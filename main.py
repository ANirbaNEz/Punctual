"""Attendance API. FastAPI + MySQL + JWT (cookie). Run: ADMIN_PASSWORD=secret python main.py"""
import base64, hashlib, hmac, json, math, os, re, secrets, threading, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import jwt
import numpy as np
import pymysql
import uvicorn
from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).parent
lock = threading.Lock()  # ponytail: one shared connection + lock; fine for one office, use a pool if it grows
db = pymysql.connect(host=os.environ.get("MYSQL_HOST", "127.0.0.1"), port=int(os.environ.get("MYSQL_PORT", 3306)),
                     user=os.environ.get("MYSQL_USER", "root"), password=os.environ.get("MYSQL_PASSWORD", ""),
                     database=os.environ.get("MYSQL_DB", "attendance"), charset="utf8mb4",
                     cursorclass=pymysql.cursors.DictCursor, autocommit=True)


def q(sql, *a):
    with lock:
        try:
            db.ping(reconnect=False)
        except pymysql.err.Error:
            db.connect()
        with db.cursor() as c:
            c.execute(sql, a)
            return list(c.fetchall())


def one(sql, *a):
    r = q(sql, *a)
    return r[0] if r else None


def run(sql, *a):
    q(sql, *a)


def setting(k, d=None):
    r = one("select v from settings where k=%s", k)
    return r["v"] if r else d


def hpw(p, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ":" + hashlib.scrypt(p.encode(), salt=salt.encode(), n=2**14, r=8, p=1).hex()


def verify(p, h):
    return hmac.compare_digest(hpw(p, h.split(":")[0]), h)


SECRET = setting("secret")
if not SECRET:
    SECRET = secrets.token_hex(32)
    run("insert into settings values('secret',%s)", SECRET)
if not one("select 1 from users where role='admin'"):
    run("insert into users(name,username,pw,role,status) values('Admin','admin',%s,'admin','active')",
        hpw(os.environ.get("ADMIN_PASSWORD", "admin123")))
    print('Created admin user "admin"' + ("" if os.environ.get("ADMIN_PASSWORD") else " with default password admin123 - delete the admin row from the users table and restart with ADMIN_PASSWORD set to change it"))

try:
    q("select face_status, face_emb, face_photo from users limit 1")
    q("select in_photo, in_face, out_photo, out_face from attendance limit 1")
except pymysql.err.Error:
    raise SystemExit("Your database is missing the face columns. Run the 'Upgrade an existing database' SQL at the bottom of schema.sql, then start again.")

app = FastAPI(docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")  # PWA icons + manifests


@app.get("/sw.js")
def service_worker():
    return FileResponse(HERE / "static" / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.exception_handler(HTTPException)
async def _err(_, e):
    return JSONResponse({"error": e.detail}, e.status_code)


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


def day():
    return datetime.now().strftime("%Y-%m-%d")  # server-local date


def ms(d):
    return int(d.timestamp() * 1000) if d else None


def fix(r):
    """DB stores real DATETIMEs (check_in / check_out); the pages get them as epoch milliseconds (in_t / out_t)."""
    if r:
        r["in_t"], r["out_t"] = ms(r.pop("check_in", None)), ms(r.pop("check_out", None))
    return r


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


def today(uid):
    return fix(one("select * from attendance where user_id=%s and `day`=%s", uid, day()))


def dist(a, b, c, d):
    r = math.radians
    h = math.sin(r(c - a) / 2) ** 2 + math.cos(r(a)) * math.cos(r(c)) * math.sin(r(d - b) / 2) ** 2
    return 12742000 * math.asin(math.sqrt(h))


def address(lat, lng):
    try:
        req = urllib.request.Request(f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lng}",
                                     headers={"User-Agent": "attendance-app/1.0"})
        return json.load(urllib.request.urlopen(req, timeout=4)).get("display_name")
    except Exception:
        return None


def pos(b):
    try:
        lat, lng = float(b["lat"]), float(b["lng"])
        assert abs(lat) <= 90 and abs(lng) <= 180
    except Exception:
        raise HTTPException(400, "Bad location")
    return lat, lng, b.get("acc")


# ---- selfie + face match (OpenCV YuNet detector + SFace recogniser, both small ONNX models) ----
PHOTOS = HERE / "photos"
MODELS = HERE / "models"
PHOTOS.mkdir(exist_ok=True)
MODELS.mkdir(exist_ok=True)
ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models/"
MODEL_FILES = {"face_detection_yunet_2023mar.onnx": ZOO + "face_detection_yunet/face_detection_yunet_2023mar.onnx",
               "face_recognition_sface_2021dec.onnx": ZOO + "face_recognition_sface/face_recognition_sface_2021dec.onnx"}
MATCH_MIN = 0.363  # cosine similarity the SFace authors recommend for "same person"
face_lock = threading.Lock()
detector = recognizer = None


def load_face_models():
    global detector, recognizer
    try:
        for name, url in MODEL_FILES.items():
            f = MODELS / name
            if not f.exists():
                print(f"Downloading {name} (one time)...")
                urllib.request.urlretrieve(url, f.with_suffix(".part"))
                f.with_suffix(".part").replace(f)
        detector = cv2.FaceDetectorYN.create(str(MODELS / "face_detection_yunet_2023mar.onnx"), "", (320, 320), 0.8, 0.3, 5000)
        recognizer = cv2.FaceRecognizerSF.create(str(MODELS / "face_recognition_sface_2021dec.onnx"), "")
    except Exception as e:  # fail closed: check-in is refused while the models are missing
        print("WARNING: face models unavailable:", e)


load_face_models()


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
    name = secrets.token_hex(12) + ".jpg"
    (PHOTOS / name).write_bytes(raw)
    return name


def verify_face(u, b):
    """Employees need an admin-approved face. Returns (saved photo name, match score) or raises."""
    if u["face_status"] == "none":
        raise HTTPException(403, "Register your face first.")
    if u["face_status"] == "pending":
        raise HTTPException(403, "Your face is waiting for admin approval.")
    raw = photo_bytes(b)
    emb = face_embed(raw)
    stored = np.array(json.loads(u["face_emb"]), dtype=np.float32).reshape(1, -1)
    with face_lock:
        score = float(recognizer.match(stored, emb, cv2.FaceRecognizerSF_FR_COSINE))
    if score < MATCH_MIN:
        raise HTTPException(403, "That doesn't look like you. Try again in better light, facing the camera.")
    return save_photo(raw), score


NOCACHE = {"Cache-Control": "no-store"}  # always serve the latest page after edits


@app.get("/", response_class=HTMLResponse)
def employee_page():
    return HTMLResponse((HERE / "employee.html").read_text(encoding="utf-8"), headers=NOCACHE)


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse((HERE / "admin.html").read_text(encoding="utf-8"), headers=NOCACHE)


@app.post("/api/register")
def register(b: dict = Body(...)):
    name, un, pw = str(b.get("name", "")).strip()[:60], str(b.get("username", "")).strip().lower(), str(b.get("password", ""))
    if not name or not re.fullmatch(r"[a-z0-9._@+-]{3,64}", un) or len(pw) < 6:
        raise HTTPException(400, "Enter your name, a username or email (letters, numbers and . _ @ + - only) and a password of 6+ characters")
    if one("select 1 from users where username=%s", un):
        raise HTTPException(409, "Username already taken")
    raw = photo_bytes(b)  # sign-up includes a selfie; it is checked for a face before the account is created
    emb = face_embed(raw)
    run("insert into users(name,username,pw,face_emb,face_photo,face_status) values(%s,%s,%s,%s,%s,'pending')",
        name, un, hpw(pw), json.dumps(emb.flatten().tolist()), save_photo(raw))
    notify()
    return {"msg": "Registered! Your admin will review your account and photo. You can log in once they approve."}


fails = {}  # ponytail: in-memory login throttle, resets on restart


@app.post("/api/login")
def login(req: Request, b: dict = Body(...)):
    un = str(b.get("username", "")).strip().lower()
    k = f"{req.client.host}{un}"
    recent = [t for t in fails.get(k, []) if t > datetime.now().timestamp() - 900]
    if len(recent) >= 10:
        raise HTTPException(429, "Too many attempts, try again in 15 minutes")
    u = one("select * from users where username=%s", un)
    if not u or not verify(str(b.get("password", "")), u["pw"]):
        fails[k] = recent + [datetime.now().timestamp()]
        raise HTTPException(401, "Wrong username or password")
    if u["status"] == "pending":
        raise HTTPException(403, "Your account is waiting for admin approval")
    if u["status"] == "disabled":
        raise HTTPException(403, "Your account is disabled")
    token = jwt.encode({"sub": str(u["id"]), "exp": datetime.now() + timedelta(days=7)}, SECRET, algorithm="HS256")
    r = JSONResponse({"role": u["role"]})
    r.set_cookie("token", token, httponly=True, samesite="lax", max_age=7 * 86400)
    return r


@app.post("/api/logout")
def logout():
    r = JSONResponse({"ok": 1})
    r.delete_cookie("token")
    return r


@app.get("/api/me")
def me(u=Depends(auth)):
    return {"name": u["name"], "role": u["role"], "shift": u["shift"], "grace": float(setting("grace", 10)), "office": setting("office_name", ""), "office_address": setting("office_address", ""), "face": u["face_status"], "today": today(u["id"]),
            "recent": [fix(r) for r in q("select `day`,check_in,check_out,status,late_min from attendance where user_id=%s order by `day` desc limit 10", u["id"])]}


@app.post("/api/face/enroll")
def face_enroll(b: dict = Body(...), u=Depends(auth)):
    if u["role"] != "employee":
        raise HTTPException(403, "Only employees register a face.")
    if u["face_status"] == "approved":
        raise HTTPException(409, "Your face is already registered. Ask your admin to reset it if you need to change it.")
    raw = photo_bytes(b)
    emb = face_embed(raw)
    if u["face_photo"]:
        (PHOTOS / u["face_photo"]).unlink(missing_ok=True)
    run("update users set face_emb=%s, face_photo=%s, face_status='pending' where id=%s",
        json.dumps(emb.flatten().tolist()), save_photo(raw), u["id"])
    notify()
    return {"msg": "Face saved. Your admin needs to approve it before you can check in."}


@app.post("/api/checkin")
def checkin(b: dict = Body(...), u=Depends(auth)):
    if u["role"] != "employee":
        raise HTTPException(403, "Admin accounts don't mark attendance. Log in with an employee account.")
    lat, lng, acc = pos(b)
    if today(u["id"]):
        raise HTTPException(409, "You have already checked in today")
    if setting("office_lat") is not None:
        rad = float(setting("radius", 200))
        m = round(dist(float(setting("office_lat")), float(setting("office_lng")), lat, lng))
        if m > rad:
            raise HTTPException(403, f"You are {m} m from {setting("office_name") or setting("office_address") or "the office"} (allowed: {int(rad)} m). Move closer and try again.")
    photo, score = verify_face(u, b)
    h, mi = map(int, u["shift"].split(":"))
    n = datetime.now()
    mins = n.hour * 60 + n.minute - (h * 60 + mi)
    status = "early" if mins < 0 else "on_time" if mins <= float(setting("grace", 10)) else "late"
    run("insert into attendance(user_id,`day`,check_in,in_lat,in_lng,in_acc,in_addr,status,late_min,in_photo,in_face) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        u["id"], day(), n, lat, lng, acc, None, status, max(mins, 0), photo, score)
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
    lat, lng, _ = pos(b)
    r = today(u["id"])
    if not r:
        raise HTTPException(400, "Check in first")
    if r["out_t"]:
        raise HTTPException(409, "You have already checked out today")
    photo, score = verify_face(u, b)
    now = datetime.now()
    run("update attendance set check_out=%s,out_lat=%s,out_lng=%s,out_photo=%s,out_face=%s where id=%s", now, lat, lng, photo, score, r["id"])
    fill_address("out_addr", r["id"], lat, lng)
    notify()
    m = round((ms(now) - r["in_t"]) / 60000)
    return {"today": today(u["id"]), "msg": f"Checked out. You worked {m // 60}h {m % 60}m today. See you tomorrow!"}


@app.get("/api/admin/data")
def admin_data(date: str = "", _=Depends(admin)):
    d = date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else day()
    return {"date": d, "today": day(),
            "emps": q("select id,name,username,status,shift,face_status,face_photo from users where role='employee' order by name"),
            "rows": [fix(r) for r in q("select a.*,u.name,u.shift from attendance a join users u on u.id=a.user_id where a.`day`=%s and u.role='employee' order by a.check_in", d)],
            "month": q("select user_id, cast(coalesce(sum(status='late'),0) as signed) late, count(*) days from attendance where date_format(`day`,'%%Y-%%m')=%s group by user_id", d[:7]),
            "settings": {"name": setting("office_name", ""), "address": setting("office_address", ""), "lat": setting("office_lat", ""), "lng": setting("office_lng", ""),
                         "radius": float(setting("radius", 200)), "grace": float(setting("grace", 10))}}


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
        if row and row["face_photo"]:
            (PHOTOS / row["face_photo"]).unlink(missing_ok=True)
        run("update users set face_emb=null, face_photo=null, face_status='none' where id=%s and role='employee'", i)
    if shift:
        run("update users set shift=%s where id=%s and role='employee'", shift, i)
    notify()
    return {"ok": 1}


@app.get("/api/admin/events")
def admin_events(_=Depends(admin)):
    """Server-sent events: one message whenever something changes, a comment every 15 s to keep the link alive."""
    def stream():
        seen = version
        yield "retry: 3000\n\n"
        while True:
            with cond:
                changed = cond.wait_for(lambda: version != seen, timeout=5)
                seen = version
            # "ping" every 5 s lets the page tell whether the stream really arrives (Cloudflare quick tunnels hold it back)
            yield "data: update\n\n" if changed else "data: ping\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@app.get("/api/admin/photo/{name}")
def admin_photo(name: str, _=Depends(admin)):
    if not re.fullmatch(r"[a-f0-9]{24}\.jpg", name) or not (PHOTOS / name).exists():
        raise HTTPException(404, "Not found")
    return FileResponse(PHOTOS / name, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


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
    return {"ok": 1}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 3000)), timeout_graceful_shutdown=2)
