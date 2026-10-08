"""Punctual attendance API. FastAPI + MySQL + JWT (cookie).
Run:  ADMIN_PASSWORD=<8+ chars> python main.py   (ADMIN_PASSWORD is only needed on the very first start)"""
import base64, hashlib, hmac, json, math, os, re, secrets, threading, time, urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import cv2
import jwt
import numpy as np
import pymysql
import uvicorn
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).parent
env = os.environ.get
MAX_ACCURACY = float(env("MAX_ACCURACY", 150))            # metres; a worse GPS fix is rejected
PHOTO_RETENTION_DAYS = int(env("PHOTO_RETENTION_DAYS", 90))  # attendance selfies older than this are deleted
GEOCODE = env("GEOCODE", "on") != "off"                  # "off" stops sending coordinates to OpenStreetMap
DB = dict(host=env("MYSQL_HOST", "127.0.0.1"), port=int(env("MYSQL_PORT", 3306)), user=env("MYSQL_USER", "root"),
          password=env("MYSQL_PASSWORD", ""), database=env("MYSQL_DB", "attendance"), charset="utf8mb4",
          cursorclass=pymysql.cursors.DictCursor, autocommit=True)

# ---------------------------------------------------------------- database (one connection per thread)
_local = threading.local()


def conn():
    c = getattr(_local, "c", None)
    if c is None:
        c = _local.c = pymysql.connect(**DB)
    else:
        try:
            c.ping(reconnect=False)
        except pymysql.err.Error:
            c.connect()
    return c


def q(sql, *a):
    with conn().cursor() as c:
        c.execute(sql, a)
        return list(c.fetchall())


def one(sql, *a):
    r = q(sql, *a)
    return r[0] if r else None


def run(sql, *a):
    """Runs a write and returns how many rows it changed."""
    with conn().cursor() as c:
        return c.execute(sql, a)


def setting(k, d=None):
    r = one("select v from settings where k=%s", k)
    return r["v"] if r else d


# ---------------------------------------------------------------- passwords
def _scrypt(p, salt, n):
    return hashlib.scrypt(p.encode(), salt=salt.encode(), n=n, r=8, p=1, maxmem=128 * 1024 * 1024).hex()


def hpw(p, salt=None):
    salt = salt or secrets.token_hex(16)
    return f"s15${salt}${_scrypt(p, salt, 2**15)}"


def verify(p, stored):
    if stored.startswith("s15$"):
        _, salt, h = stored.split("$")
        return hmac.compare_digest(_scrypt(p, salt, 2**15), h)
    salt, h = stored.split(":")  # older hashes (cost 2**14), upgraded at the next login
    return hmac.compare_digest(_scrypt(p, salt, 2**14), h)


# ---------------------------------------------------------------- startup
ADMIN_NEEDED = ("First start: set ADMIN_PASSWORD (at least 8 characters) to create the admin account, for example:\n"
                '  $env:ADMIN_PASSWORD="choose-a-strong-password"; python main.py')
SECRET = None
fernet = None
PHOTOS = HERE / "photos"
MODELS = HERE / "models"


def init():
    global SECRET, fernet
    try:
        q("select face_status, face_emb, face_photo from users limit 1")
        q("select in_photo, in_face, out_photo, out_face from attendance limit 1")
    except pymysql.err.Error:
        raise SystemExit("Your database is missing the face columns. Run the 'Upgrade an existing database' SQL at the bottom of schema.sql, then start again.")
    run("""create table if not exists attempts(id bigint auto_increment primary key, kind varchar(10) not null, ip varchar(64) not null,
           username varchar(64) null, t timestamp not null default current_timestamp, key ix_attempts (kind, ip, t))""")
    PHOTOS.mkdir(exist_ok=True)
    MODELS.mkdir(exist_ok=True)
    SECRET = setting("secret")
    if not SECRET:
        SECRET = secrets.token_hex(32)
        run("insert into settings values('secret',%s)", SECRET)
    key = setting("photo_key")
    if not key:
        key = Fernet.generate_key().decode()
        run("insert into settings values('photo_key',%s)", key)
    fernet = Fernet(key.encode())
    if not one("select 1 from users where role='admin'"):
        pw = env("ADMIN_PASSWORD", "")
        if len(pw) < 8:
            raise SystemExit(ADMIN_NEEDED)
        run("insert into users(name,username,pw,role,status) values('Admin','admin',%s,'admin','active')", hpw(pw))
        print('Created admin user "admin".')
    load_face_models()
    purge()
    threading.Thread(target=daily_cleanup, daemon=True).start()


def daily_cleanup():
    while True:
        time.sleep(86400)
        purge()


@asynccontextmanager
async def lifespan(_):
    init()
    yield


app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")  # PWA icons + manifests


@app.middleware("http")
async def guard(request: Request, call_next):
    """Blocks cross-site writes (CSRF) and adds basic security headers."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return JSONResponse({"error": "Cross-site request blocked"}, 403)
    r = await call_next(request)
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"  # map tiles (OpenStreetMap) are refused without a Referer
    return r


@app.get("/sw.js")
def service_worker():
    return FileResponse(HERE / "static" / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.exception_handler(HTTPException)
async def _err(_, e):
    return JSONResponse({"error": e.detail}, e.status_code)


# ---------------------------------------------------------------- auth helpers
def auth(req: Request):
    try:
        uid = int(jwt.decode(req.cookies.get("token", ""), SECRET, algorithms=["HS256"])["sub"])
    except Exception:
        raise HTTPException(401, "Please log in")
    u = one("select * from users where id=%s and status='active'", uid)
    if not u:
        raise HTTPException(401, "Please log in")
    return u


def admin(u=Depends(auth)):
    if u["role"] != "admin":
        raise HTTPException(403, "Admins only")
    return u


def client_ip(req):
    """Real client address. Behind a tunnel or proxy on this machine every request comes from 127.0.0.1,
    so only then do we trust the proxy's forwarded header."""
    host = req.client.host if req.client else "?"
    if host in ("127.0.0.1", "::1"):
        return req.headers.get("cf-connecting-ip") or req.headers.get("x-forwarded-for", "").split(",")[0].strip() or host
    return host


def is_https(req):
    return req.url.scheme == "https" or req.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def attempts(kind, ip, user=None, secs=900):
    sql, args = "select count(*) c from attempts where kind=%s and ip=%s and t > now() - interval %s second", [kind, ip, secs]
    if user is not None:
        sql, args = sql + " and username=%s", args + [user]
    return one(sql, *args)["c"]


def note(kind, ip, user=None):
    run("insert into attempts(kind,ip,username) values(%s,%s,%s)", kind, ip, user)


# ---------------------------------------------------------------- time, place, lateness
def day():
    return datetime.now().strftime("%Y-%m-%d")  # server-local date


def ms(d):
    return int(d.timestamp() * 1000) if d else None


def fix(r):
    """DB stores real DATETIMEs (check_in / check_out); the pages get them as epoch milliseconds (in_t / out_t)."""
    if r:
        r["in_t"], r["out_t"] = ms(r.pop("check_in", None)), ms(r.pop("check_out", None))
    return r


def today(uid):
    return fix(one("select * from attendance where user_id=%s and `day`=%s", uid, day()))


def dist(a, b, c, d):
    r = math.radians
    h = math.sin(r(c - a) / 2) ** 2 + math.cos(r(a)) * math.cos(r(c)) * math.sin(r(d - b) / 2) ** 2
    return 12742000 * math.asin(math.sqrt(h))


def classify(shift, now, grace):
    """('early' | 'on_time' | 'late', minutes after shift start)."""
    h, mi = map(int, shift.split(":"))
    mins = now.hour * 60 + now.minute - (h * 60 + mi)
    return ("early" if mins < 0 else "on_time" if mins <= grace else "late"), mins


def pos(b):
    try:
        lat, lng = float(b["lat"]), float(b["lng"])
        assert abs(lat) <= 90 and abs(lng) <= 180
    except Exception:
        raise HTTPException(400, "Bad location")
    try:
        acc = float(b.get("acc"))
        assert math.isfinite(acc) and acc >= 0
    except Exception:
        raise HTTPException(400, "Your phone did not report how accurate the location is. Try again.")
    return lat, lng, acc


def check_place(b):
    """Used for check-in AND check-out: needs an office, a decent GPS fix, and a position inside the circle."""
    lat, lng, acc = pos(b)
    if setting("office_lat") is None:
        raise HTTPException(403, "Your admin has not set the office location yet, so nobody can check in or out.")
    if acc > MAX_ACCURACY:
        raise HTTPException(400, f"Your GPS signal is too weak (about {round(acc)} m). Move near a window or outside and try again.")
    rad = float(setting("radius", 200))
    m = round(dist(float(setting("office_lat")), float(setting("office_lng")), lat, lng))
    if m > rad:
        place = setting("office_name") or setting("office_address") or "the office"
        raise HTTPException(403, f"You are {m} m from {place} (allowed: {int(rad)} m). Move closer and try again.")
    return lat, lng, acc


def workdays():
    return {int(x) for x in setting("workdays", "0,1,2,3,4").split(",") if x != ""}


def is_day_off(d):
    """d is 'YYYY-MM-DD'. Day off = not a working weekday, or listed as a holiday."""
    return datetime.strptime(d, "%Y-%m-%d").weekday() not in workdays() or d in {x.strip() for x in setting("holidays", "").split(",")}


# ---------------------------------------------------------------- address lookup (OpenStreetMap, light use)
_geo = {}
_geo_lock = threading.Lock()
_geo_last = 0.0


def address(lat, lng):
    global _geo_last
    if not GEOCODE:
        return None
    k = (round(lat, 4), round(lng, 4))
    if k in _geo:
        return _geo[k]
    with _geo_lock:  # Nominatim allows 1 request a second
        time.sleep(max(0, 1.1 - (time.time() - _geo_last)))
        _geo_last = time.time()
        try:
            ua = "Punctual-attendance/1.0 " + env("NOMINATIM_CONTACT", "(self-hosted)")
            req = urllib.request.Request(f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lng}", headers={"User-Agent": ua})
            res = json.load(urllib.request.urlopen(req, timeout=4)).get("display_name")
        except Exception:
            return None
    if len(_geo) > 2000:
        _geo.clear()
    _geo[k] = res
    return res


cond = threading.Condition()
version = 0


def notify():
    """Wake every open admin page (server-sent events) so it refreshes itself."""
    global version
    with cond:
        version += 1
        cond.notify_all()


def fill_address(col, rid, lat, lng):
    """Look the address up after replying, so check-in isn't held up by the OpenStreetMap request."""
    def job():
        run(f"update attendance set {col}=%s where id=%s", address(lat, lng), rid)
        notify()
    threading.Thread(target=job, daemon=True).start()


# ---------------------------------------------------------------- selfie + face match (OpenCV YuNet + SFace)
ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models/"
MODEL_FILES = {"face_detection_yunet_2023mar.onnx": (ZOO + "face_detection_yunet/face_detection_yunet_2023mar.onnx",
                                                     "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
               "face_recognition_sface_2021dec.onnx": (ZOO + "face_recognition_sface/face_recognition_sface_2021dec.onnx",
                                                       "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79")}
MATCH_MIN = 0.363  # cosine similarity the SFace authors recommend for "same person"
face_lock = threading.Lock()
detector = recognizer = None


def load_face_models():
    global detector, recognizer
    try:
        MODELS.mkdir(exist_ok=True)
        for name, (url, sha) in MODEL_FILES.items():
            f = MODELS / name
            if not f.exists():
                print(f"Downloading {name} (one time)...")
                urllib.request.urlretrieve(url, f.with_suffix(".part"))
                f.with_suffix(".part").replace(f)
            if hashlib.sha256(f.read_bytes()).hexdigest() != sha:
                f.unlink()
                raise RuntimeError(f"{name} does not match its expected checksum and was deleted")
        detector = cv2.FaceDetectorYN.create(str(MODELS / "face_detection_yunet_2023mar.onnx"), "", (320, 320), 0.8, 0.3, 5000)
        recognizer = cv2.FaceRecognizerSF.create(str(MODELS / "face_recognition_sface_2021dec.onnx"), "")
    except Exception as e:  # fail closed: check-in is refused while the models are missing
        print("WARNING: face models unavailable:", e)


def cosine(a, b):
    a, b = np.asarray(a, dtype=np.float32).ravel(), np.asarray(b, dtype=np.float32).ravel()
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def photo_bytes(b):
    try:
        raw = base64.b64decode(str(b.get("photo", "")).split(",", 1)[-1], validate=True)
    except Exception:
        raw = b""
    if not raw or len(raw) > 600_000:
        raise HTTPException(400, "A selfie is required")
    return raw


def face_embed(raw):
    if detector is None:
        raise HTTPException(503, "Face check is unavailable right now. Ask your admin to check the server.")
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "That photo could not be read. Try again.")
    with face_lock:
        detector.setInputSize((img.shape[1], img.shape[0]))
        _, faces = detector.detect(img)
        if faces is None or len(faces) == 0:
            raise HTTPException(400, "No face found. Look at the camera in good light and try again.")
        face = max(faces, key=lambda f: f[2] * f[3])  # largest face in frame
        return recognizer.feature(recognizer.alignCrop(img, face))


def save_photo(raw):
    """Selfies are encrypted on disk (key kept in the database, so a copied photos folder alone is unreadable)."""
    name = secrets.token_hex(12) + ".jpg"
    (PHOTOS / name).write_bytes(fernet.encrypt(raw))
    return name


def read_photo(name):
    data = (PHOTOS / name).read_bytes()
    try:
        return fernet.decrypt(data)
    except InvalidToken:
        return data  # saved before encryption was added


def drop_photo(name):
    if name:
        (PHOTOS / name).unlink(missing_ok=True)


def verify_face(u, b):
    """Employees need an admin-approved face. Returns (saved photo name, match score) or raises."""
    if u["face_status"] == "none":
        raise HTTPException(403, "Register your face first.")
    if u["face_status"] == "pending":
        raise HTTPException(403, "Your face is waiting for admin approval.")
    raw = photo_bytes(b)
    score = cosine(json.loads(u["face_emb"]), face_embed(raw))
    if score < MATCH_MIN:
        raise HTTPException(403, "That doesn't look like you. Try again in better light, facing the camera.")
    return save_photo(raw), score


def purge():
    """Retention: delete old attendance selfies, selfies nothing refers to any more, and old rate-limit rows."""
    try:
        for r in q("select id, in_photo, out_photo from attendance where `day` < curdate() - interval %s day and (in_photo is not null or out_photo is not null)", PHOTO_RETENTION_DAYS):
            drop_photo(r["in_photo"]); drop_photo(r["out_photo"])
            run("update attendance set in_photo=null, out_photo=null where id=%s", r["id"])
        used = {r["f"] for r in q("select face_photo f from users union select in_photo from attendance union select out_photo from attendance") if r["f"]}
        for f in PHOTOS.glob("*.jpg"):
            if f.name not in used and time.time() - f.stat().st_mtime > 3600:
                f.unlink()
        run("delete from attempts where t < now() - interval 1 day")
    except Exception as e:
        print("cleanup skipped:", e)


def face_dups():
    """For faces waiting for review: is the same face already registered to another account?"""
    rows = q("select id, name, face_emb, face_status from users where role='employee' and face_emb is not null")
    embs = {r["id"]: json.loads(r["face_emb"]) for r in rows}
    out = {}
    for r in rows:
        if r["face_status"] != "pending":
            continue
        best = max(((cosine(embs[r["id"]], embs[o["id"]]), o["name"]) for o in rows if o["id"] != r["id"]), default=None)
        if best and best[0] >= MATCH_MIN:
            out[r["id"]] = {"name": best[1], "score": round(best[0], 2)}
    return out


# ---------------------------------------------------------------- pages
NOCACHE = {"Cache-Control": "no-store"}  # always serve the latest page after edits


@app.get("/", response_class=HTMLResponse)
def employee_page():
    return HTMLResponse((HERE / "employee.html").read_text(encoding="utf-8"), headers=NOCACHE)


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse((HERE / "admin.html").read_text(encoding="utf-8"), headers=NOCACHE)


# ---------------------------------------------------------------- accounts
@app.post("/api/register")
def register(req: Request, b: dict = Body(...)):
    ip = client_ip(req)
    if attempts("register", ip, secs=3600) >= 20:
        raise HTTPException(429, "Too many sign-ups from this network. Try again in an hour.")
    note("register", ip)
    if one("select count(*) c from users where status='pending'")["c"] >= 200:
        raise HTTPException(429, "Sign-ups are paused until the admin reviews the waiting accounts.")
    name, un, pw = str(b.get("name", "")).strip()[:60], str(b.get("username", "")).strip().lower(), str(b.get("password", ""))
    if not name or not re.fullmatch(r"[a-z0-9._@+-]{3,64}", un) or len(pw) < 8:
        raise HTTPException(400, "Enter your name, a username or email (letters, numbers and . _ @ + - only) and a password of 8+ characters")
    if one("select 1 from users where username=%s", un):
        raise HTTPException(409, "Username already taken")
    raw = photo_bytes(b)  # sign-up includes a selfie; it is checked for a face before the account is created
    emb = face_embed(raw)
    try:
        run("insert into users(name,username,pw,face_emb,face_photo,face_status) values(%s,%s,%s,%s,%s,'pending')",
            name, un, hpw(pw), json.dumps(emb.flatten().tolist()), save_photo(raw))
    except pymysql.err.IntegrityError:
        raise HTTPException(409, "Username already taken")
    notify()
    return {"msg": "Registered! Your admin will review your account and photo. You can log in once they approve."}


@app.post("/api/login")
def login(req: Request, b: dict = Body(...)):
    ip, un = client_ip(req), str(b.get("username", "")).strip().lower()
    if attempts("login", ip, un) >= 10 or attempts("login", ip) >= 40:
        raise HTTPException(429, "Too many attempts, try again in 15 minutes")
    u = one("select * from users where username=%s", un)
    if not u or not verify(str(b.get("password", "")), u["pw"]):
        note("login", ip, un)
        raise HTTPException(401, "Wrong username or password")
    if u["status"] == "pending":
        raise HTTPException(403, "Your account is waiting for admin approval")
    if u["status"] == "disabled":
        raise HTTPException(403, "Your account is disabled")
    if not u["pw"].startswith("s15$"):
        run("update users set pw=%s where id=%s", hpw(str(b.get("password", ""))), u["id"])
    token = jwt.encode({"sub": str(u["id"]), "exp": datetime.now(timezone.utc) + timedelta(days=7)}, SECRET, algorithm="HS256")
    r = JSONResponse({"role": u["role"]})
    r.set_cookie("token", token, httponly=True, samesite="lax", secure=is_https(req), max_age=7 * 86400)
    return r


@app.post("/api/logout")
def logout():
    r = JSONResponse({"ok": 1})
    r.delete_cookie("token")
    return r


@app.get("/api/me")
def me(u=Depends(auth)):
    return {"name": u["name"], "role": u["role"], "shift": u["shift"], "grace": float(setting("grace", 10)), "office": setting("office_name", ""),
            "office_address": setting("office_address", ""), "face": u["face_status"], "today": today(u["id"]),
            "recent": [fix(r) for r in q("select `day`,check_in,check_out,status,late_min from attendance where user_id=%s order by `day` desc limit 10", u["id"])]}


@app.post("/api/face/enroll")
def face_enroll(b: dict = Body(...), u=Depends(auth)):
    if u["role"] != "employee":
        raise HTTPException(403, "Only employees register a face.")
    if u["face_status"] == "approved":
        raise HTTPException(409, "Your face is already registered. Ask your admin to reset it if you need to change it.")
    raw = photo_bytes(b)
    emb = face_embed(raw)
    drop_photo(u["face_photo"])
    run("update users set face_emb=%s, face_photo=%s, face_status='pending' where id=%s",
        json.dumps(emb.flatten().tolist()), save_photo(raw), u["id"])
    notify()
    return {"msg": "Face saved. Your admin needs to approve it before you can check in."}


# ---------------------------------------------------------------- attendance
@app.post("/api/checkin")
def checkin(b: dict = Body(...), u=Depends(auth)):
    if u["role"] != "employee":
        raise HTTPException(403, "Admin accounts don't mark attendance. Log in with an employee account.")
    if today(u["id"]):
        raise HTTPException(409, "You have already checked in today")
    lat, lng, acc = check_place(b)
    photo, score = verify_face(u, b)
    n = datetime.now()
    status, mins = classify(u["shift"], n, float(setting("grace", 10)))
    try:
        run("insert into attendance(user_id,`day`,check_in,in_lat,in_lng,in_acc,in_addr,status,late_min,in_photo,in_face) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            u["id"], day(), n, lat, lng, acc, None, status, max(mins, 0), photo, score)
    except pymysql.err.IntegrityError:  # double tap: the first request already won
        drop_photo(photo)
        raise HTTPException(409, "You have already checked in today")
    t = today(u["id"])
    fill_address("in_addr", t["id"], lat, lng)
    notify()
    msg = {"early": f"Attendance done for today. You're early by {-mins} min, good job!",
           "on_time": "Attendance done for today. You are on time, well done!",
           "late": f"Attendance done for today. You're {mins} min late, try better tomorrow!"}[status]
    return {"today": t, "msg": msg}


@app.post("/api/checkout")
def checkout(b: dict = Body(...), u=Depends(auth)):
    if u["role"] != "employee":
        raise HTTPException(403, "Admin accounts don't mark attendance. Log in with an employee account.")
    r = today(u["id"])
    if not r:
        raise HTTPException(400, "Check in first")
    if r["out_t"]:
        raise HTTPException(409, "You have already checked out today")
    lat, lng, _ = check_place(b)
    photo, score = verify_face(u, b)
    now = datetime.now()
    if not run("update attendance set check_out=%s,out_lat=%s,out_lng=%s,out_photo=%s,out_face=%s where id=%s and check_out is null",
               now, lat, lng, photo, score, r["id"]):
        drop_photo(photo)
        raise HTTPException(409, "You have already checked out today")
    fill_address("out_addr", r["id"], lat, lng)
    notify()
    m = round((ms(now) - r["in_t"]) / 60000)
    return {"today": today(u["id"]), "msg": f"Checked out. You worked {m // 60}h {m % 60}m today. See you tomorrow!"}


# ---------------------------------------------------------------- admin
@app.get("/api/admin/data")
def admin_data(date: str = "", _=Depends(admin)):
    d = date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else day()
    try:
        off = is_day_off(d)
    except ValueError:
        d, off = day(), is_day_off(day())
    dups = face_dups()
    emps = q("select id,name,username,status,shift,face_status,face_photo from users where role='employee' order by name")
    for e in emps:
        e["face_dup"] = dups.get(e["id"])
    return {"date": d, "today": day(), "dayoff": off, "emps": emps,
            "rows": [fix(r) for r in q("select a.*,u.name,u.shift from attendance a join users u on u.id=a.user_id where a.`day`=%s and u.role='employee' order by a.check_in", d)],
            "month": q("select user_id, cast(coalesce(sum(status='late'),0) as signed) late, count(*) days from attendance where date_format(`day`,'%%Y-%%m')=%s group by user_id", d[:7]),
            "settings": {"name": setting("office_name", ""), "address": setting("office_address", ""), "lat": setting("office_lat", ""), "lng": setting("office_lng", ""),
                         "radius": float(setting("radius", 200)), "grace": float(setting("grace", 10)),
                         "workdays": sorted(workdays()), "holidays": setting("holidays", ""), "max_accuracy": MAX_ACCURACY}}


@app.get("/api/admin/geocode")
def admin_geocode(lat: float, lng: float, _=Depends(admin)):
    return {"address": address(lat, lng)}  # server-side so the browser isn't blocked by CORS / rate limits


@app.post("/api/admin/employee")
def admin_employee(b: dict = Body(...), _=Depends(admin)):
    shift, act, i = b.get("shift"), b.get("action"), b.get("id")
    if shift is not None and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", shift):
        raise HTTPException(400, "Bad shift time")
    if act in ("approve", "enable"):
        run("update users set status='active' where id=%s and role='employee'", i)
    if act == "disable":
        run("update users set status='disabled' where id=%s and role='employee'", i)
    if act == "face_approve":
        run("update users set face_status='approved' where id=%s and role='employee' and face_emb is not null", i)
    if act == "face_reset":  # also the way to delete someone's face data
        row = one("select face_photo from users where id=%s and role='employee'", i)
        if row:
            drop_photo(row["face_photo"])
        run("update users set face_emb=null, face_photo=null, face_status='none' where id=%s and role='employee'", i)
    if shift:
        run("update users set shift=%s where id=%s and role='employee'", shift, i)
    notify()
    return {"ok": 1}


@app.get("/api/admin/events")
def admin_events(_=Depends(admin)):
    """Server-sent events: 'update' whenever something changes, 'ping' every 5 s so the page can tell the stream arrives."""
    def stream():
        seen = version
        yield "retry: 3000\n\n"
        while True:
            with cond:
                changed = cond.wait_for(lambda: version != seen, timeout=5)
                seen = version
            yield "data: update\n\n" if changed else "data: ping\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@app.get("/api/admin/photo/{name}")
def admin_photo(name: str, _=Depends(admin)):
    if not re.fullmatch(r"[a-f0-9]{24}\.jpg", name) or not (PHOTOS / name).exists():
        raise HTTPException(404, "Not found")
    return Response(read_photo(name), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/admin/settings")
def admin_settings(b: dict = Body(...), _=Depends(admin)):
    def put(k, v):
        run("insert into settings values(%s,%s) on duplicate key update v=values(v)", k, str(v))

    def num(k, lo, default):
        try:
            return max(lo, float(b.get(k)))
        except (TypeError, ValueError):
            return default

    if b.get("lat") in ("", None):
        run("delete from settings where k in ('office_lat','office_lng','office_name','office_address')")
    else:
        try:
            a, o = float(b["lat"]), float(b["lng"])
            assert abs(a) <= 90 and abs(o) <= 180
        except Exception:
            raise HTTPException(400, "Bad coordinates")
        put("office_lat", a)
        put("office_lng", o)
        put("office_name", str(b.get("name", "")).strip()[:250])
        put("office_address", str(b.get("address", "")).strip()[:250])
    put("radius", num("radius", 20, 200))
    put("grace", num("grace", 0, 10))
    if "workdays" in b:
        days = sorted({int(x) for x in b["workdays"] if int(x) in range(7)})
        put("workdays", ",".join(map(str, days)) or "-1")
    if "holidays" in b:
        hol = [x.strip() for x in str(b["holidays"]).split(",") if x.strip()]
        for x in hol:
            try:
                datetime.strptime(x, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(400, f"'{x}' is not a date. Use YYYY-MM-DD, separated by commas.")
        put("holidays", ",".join(hol)) if hol else run("delete from settings where k='holidays'")
    return {"ok": 1}


if __name__ == "__main__":
    # checked before the server starts so the message is short and clear
    try:
        no_admin_yet = not one("select 1 from users where role='admin'")
    except pymysql.err.Error as e:
        raise SystemExit(f"Cannot use the database ({e}). Is MySQL running, and are MYSQL_USER / MYSQL_PASSWORD / MYSQL_DB right? Did you run schema.sql?")
    if no_admin_yet and len(env("ADMIN_PASSWORD", "")) < 8:
        raise SystemExit(ADMIN_NEEDED)
    uvicorn.run(app, host="0.0.0.0", port=int(env("PORT", 3000)), timeout_graceful_shutdown=2)
