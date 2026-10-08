"""End-to-end test against a RUNNING app and an empty database.

    python tests/e2e.py http://127.0.0.1:3000 <ADMIN_PASSWORD>

The app must be freshly started on an empty database (CI does this; see .github/workflows/ci.yml).
Faces: scikit-image's public sample photo (person A) and OpenCV's public 'lena.jpg' (person B, downloaded once).
"""
import base64, http.cookiejar, json, sys, tempfile, threading, time, urllib.error, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import cv2
from skimage import data

BASE, ADMIN_PW = sys.argv[1].rstrip("/"), sys.argv[2]
LENA_URL = "https://raw.githubusercontent.com/opencv/opencv/master/samples/data/lena.jpg"


def b64(bgr, w=480):
    bgr = cv2.resize(bgr, (w, int(bgr.shape[0] * w / bgr.shape[1])))
    return base64.b64encode(cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])[1]).decode()


lena = Path(tempfile.gettempdir()) / "punctual-lena.jpg"
if not lena.exists():
    urllib.request.urlretrieve(LENA_URL, lena)
PERSON_A = b64(cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR))
PERSON_B = b64(cv2.imread(str(lena)))
NO_FACE = b64(cv2.cvtColor(data.coffee(), cv2.COLOR_RGB2BGR))


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, raw=False, headers=None):
        req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            r = self.op.open(req, timeout=60)
        except urllib.error.HTTPError as e:
            r = e
        payload = r.read()
        if raw:
            return r.status, payload, r.headers
        try:
            return r.status, json.loads(payload or b"{}")
        except Exception:
            return r.status, payload


results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))


anon, admin, emp = Client(), Client(), Client()
INSIDE, FAR = {"lat": 22.5727, "lng": 88.3640, "acc": 12}, {"lat": 22.60, "lng": 88.40, "acc": 12}

# ---- pages, static, auth gates, security headers
for path in ("/", "/admin", "/sw.js", "/static/manifest-employee.json", "/static/manifest-admin.json", "/static/icon-employee-192.png"):
    check(f"page {path} loads", anon.call("GET", path, raw=True)[0] == 200)
_, _, h = anon.call("GET", "/", raw=True)
check("html is no-store and has security headers", "no-store" in (h.get("Cache-Control") or "") and h.get("X-Content-Type-Options") == "nosniff")
check("unauthenticated /api/me -> 401", anon.call("GET", "/api/me")[0] == 401)
check("unauthenticated admin data -> 401", anon.call("GET", "/api/admin/data")[0] == 401)
check("cross-site POST is blocked (CSRF)", anon.call("POST", "/api/login", {"username": "a", "password": "b"}, headers={"Origin": "https://evil.example"})[0] == 403)

# ---- sign-up with selfie
good = {"name": "Early Eddie", "username": "eddie@example.com", "password": "secret123"}
check("sign-up without selfie rejected", anon.call("POST", "/api/register", good)[0] == 400)
check("sign-up with a photo that has no face rejected", anon.call("POST", "/api/register", {**good, "photo": NO_FACE})[0] == 400)
check("sign-up with a 7-character password rejected", anon.call("POST", "/api/register", {**good, "password": "1234567", "photo": PERSON_A})[0] == 400)
s, j = anon.call("POST", "/api/register", {**good, "photo": PERSON_A})
check("sign-up with selfie succeeds", s == 200, j)
check("duplicate username rejected", anon.call("POST", "/api/register", {**good, "photo": PERSON_A})[0] == 409)
check("login before approval blocked", emp.call("POST", "/api/login", {"username": good["username"], "password": good["password"]})[0] == 403)

# ---- admin
check("admin wrong password -> 401", Client().call("POST", "/api/login", {"username": "admin", "password": "nope"})[0] == 401)
s, j = admin.call("POST", "/api/login", {"username": "admin", "password": ADMIN_PW})
check("admin login", s == 200 and j.get("role") == "admin", j)
s, _, h = Client().call("POST", "/api/login", {"username": "admin", "password": ADMIN_PW}, raw=True, headers={"X-Forwarded-Proto": "https"})
check("session cookie is Secure behind an https tunnel", "secure" in (h.get("Set-Cookie") or "").lower())
s, _, h = Client().call("POST", "/api/login", {"username": "admin", "password": ADMIN_PW}, raw=True)
check("...and not marked Secure on plain http (so local use works)", "secure" not in (h.get("Set-Cookie") or "").lower())
s, d = admin.call("GET", "/api/admin/data")
e = next((x for x in d["emps"] if x["username"] == good["username"]), None)
check("admin sees the new employee, pending, face to review", e and e["status"] == "pending" and e["face_status"] == "pending" and e["face_photo"], e)

events = []
def listen():
    cj = http.cookiejar.CookieJar()
    for c in admin.jar: cj.set_cookie(c)
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    with op.open(urllib.request.Request(BASE + "/api/admin/events"), timeout=40) as r:
        for line in r:
            if line.startswith(b"data: update"): events.append(time.time())
            if len(events) >= 3: break
threading.Thread(target=listen, daemon=True).start()
time.sleep(1)

uid = e["id"]
now = datetime.now()
fmt = lambda dt: dt.strftime("%H:%M")
check("approve account", admin.call("POST", "/api/admin/employee", {"id": uid, "action": "approve", "shift": fmt(now + timedelta(minutes=60))})[0] == 200)
check("approve face", admin.call("POST", "/api/admin/employee", {"id": uid, "action": "face_approve"})[0] == 200)
check("live update arrived after an admin action", len(events) >= 1)

# ---- the geofence fails closed until an office exists
s, j = emp.call("POST", "/api/login", {"username": good["username"], "password": good["password"]})
check("employee login after approval", s == 200 and j.get("role") == "employee", j)
s, j = emp.call("POST", "/api/checkin", {**INSIDE, "photo": PERSON_A})
check("check-in is refused while no office is set", s == 403 and "office" in str(j), j)
check("admin saves the office circle", admin.call("POST", "/api/admin/settings", {"name": "Test Office", "address": "Somewhere, Kolkata", "lat": 22.5726, "lng": 88.3639, "radius": 150, "grace": 10,
      "workdays": [0, 1, 2, 3, 4, 5, 6], "holidays": ""})[0] == 200)
s, me = emp.call("GET", "/api/me")
check("me: face approved, office name shown", me.get("face") == "approved" and me.get("office") == "Test Office", me)

# ---- check-in / check-out rules
check("check-in without selfie rejected", emp.call("POST", "/api/checkin", INSIDE)[0] == 400)
check("check-in with a weak GPS fix (400 m) rejected", emp.call("POST", "/api/checkin", {**INSIDE, "acc": 400, "photo": PERSON_A})[0] == 400)
check("check-in without an accuracy value rejected", emp.call("POST", "/api/checkin", {"lat": INSIDE["lat"], "lng": INSIDE["lng"], "photo": PERSON_A})[0] == 400)
check("check-in with WRONG PERSON's face rejected", emp.call("POST", "/api/checkin", {**INSIDE, "photo": PERSON_B})[0] == 403)
s, j = emp.call("POST", "/api/checkin", {**FAR, "photo": PERSON_A})
check("check-in outside the circle rejected", s == 403 and "from" in str(j), j)
s, j = emp.call("POST", "/api/checkin", {**INSIDE, "photo": PERSON_A})
check("check-in inside the circle with the right face -> early", s == 200 and j["today"]["status"] == "early", j)
check("second check-in rejected", emp.call("POST", "/api/checkin", {**INSIDE, "photo": PERSON_A})[0] == 409)
check("check-out outside the circle is ALSO rejected", emp.call("POST", "/api/checkout", {**FAR, "photo": PERSON_A})[0] == 403)
check("check-out with a weak GPS fix rejected", emp.call("POST", "/api/checkout", {**INSIDE, "acc": 400, "photo": PERSON_A})[0] == 400)
check("check-out with WRONG face rejected", emp.call("POST", "/api/checkout", {**INSIDE, "photo": PERSON_B})[0] == 403)
s, j = emp.call("POST", "/api/checkout", {**INSIDE, "photo": PERSON_A})
check("check-out inside the circle OK", s == 200 and "worked" in str(j.get("msg", "")), j)
check("second check-out rejected", emp.call("POST", "/api/checkout", {**INSIDE, "photo": PERSON_A})[0] == 409)
check("employee cannot re-enrol while approved", emp.call("POST", "/api/face/enroll", {"photo": PERSON_A})[0] == 409)
check("employee blocked from admin data and events", emp.call("GET", "/api/admin/data")[0] == 403 and emp.call("GET", "/api/admin/events")[0] == 403)


def onboard(label, name, shift):
    c = Client()
    u = f"{label}@example.com"
    assert c.call("POST", "/api/register", {"name": name, "username": u, "password": "secret123", "photo": PERSON_A})[0] == 200
    i = next(x["id"] for x in admin.call("GET", "/api/admin/data")[1]["emps"] if x["username"] == u)
    admin.call("POST", "/api/admin/employee", {"id": i, "action": "approve", "shift": shift})
    admin.call("POST", "/api/admin/employee", {"id": i, "action": "face_approve"})
    c.call("POST", "/api/login", {"username": u, "password": "secret123"})
    return c, i


if 1 <= now.hour <= 21:  # the shift times below are built from "now", which breaks around midnight
    c2, _ = onboard("ontime", "On-Time Olivia", fmt(now - timedelta(minutes=5)))
    s, j = c2.call("POST", "/api/checkin", {**INSIDE, "photo": PERSON_A})
    check("5 min after shift start (grace 10) -> on time", s == 200 and j["today"]["status"] == "on_time", j)
    c3, _ = onboard("late", "Late Larry", fmt(now - timedelta(minutes=60)))
    s, j = c3.call("POST", "/api/checkin", {**INSIDE, "photo": PERSON_A})
    check("60 min after shift start -> late (+60)", s == 200 and j["today"]["status"] == "late" and 58 <= j["today"]["late_min"] <= 62, j)
else:
    print("SKIP  on-time/late checks (too close to midnight)")

# ---- a second account registering the same face is flagged to the admin
anon.call("POST", "/api/register", {"name": "Copycat Cleo", "username": "cleo@example.com", "password": "secret123", "photo": PERSON_A})
cleo = next(x for x in admin.call("GET", "/api/admin/data")[1]["emps"] if x["username"] == "cleo@example.com")
check("same face on a second account is flagged for the admin", cleo["face_dup"] and cleo["face_dup"]["name"], cleo)

# ---- what the admin sees
time.sleep(4)  # addresses are looked up in the background
s, d = admin.call("GET", "/api/admin/data")
rows = {r["name"]: r for r in d["rows"]}
check("admin table lists the check-ins", {"Early Eddie"} <= set(rows) and (len(rows) == 3 or not 1 <= now.hour <= 21), list(rows))
r = rows.get("Early Eddie", {})
check("row has in/out times, selfies and match scores", r.get("in_t") and r.get("out_t") and r.get("in_photo") and r.get("out_photo") and (r.get("in_face") or 0) > 0.9, {k: r.get(k) for k in ("in_t", "out_t", "in_photo", "in_face")})
s, body, h = admin.call("GET", f"/api/admin/photo/{r['in_photo']}", raw=True)
check("admin can open a selfie (decrypted JPEG)", s == 200 and h.get_content_type() == "image/jpeg" and body[:2] == b"\xff\xd8")
check("employee cannot open selfies", emp.call("GET", f"/api/admin/photo/{r['in_photo']}")[0] == 403)
check("path traversal on the photo route is blocked", admin.call("GET", "/api/admin/photo/..%2f..%2fmain.py")[0] == 404)
check("live updates were delivered for check-ins", len(events) >= 2, len(events))

# ---- working days / holidays
today = datetime.now().strftime("%Y-%m-%d")
admin.call("POST", "/api/admin/settings", {"lat": 22.5726, "lng": 88.3639, "radius": 150, "grace": 10, "name": "Test Office", "address": "", "workdays": [0, 1, 2, 3, 4, 5, 6], "holidays": today})
check("a listed holiday is a day off", admin.call("GET", "/api/admin/data")[1]["dayoff"] is True)
admin.call("POST", "/api/admin/settings", {"lat": 22.5726, "lng": 88.3639, "radius": 150, "grace": 10, "name": "Test Office", "address": "", "workdays": [0, 1, 2, 3, 4, 5, 6], "holidays": ""})
check("...and not once the holiday is removed", admin.call("GET", "/api/admin/data")[1]["dayoff"] is False)
check("a bad holiday date is rejected", admin.call("POST", "/api/admin/settings", {"lat": 22.5726, "lng": 88.3639, "holidays": "next tuesday"})[0] == 400)

# ---- admin rules, face reset, disabling
check("admin cannot check in", admin.call("POST", "/api/checkin", {**INSIDE, "photo": PERSON_A})[0] == 403)
fran, fid = onboard("fran", "Fresh Fran", "09:00")
admin.call("POST", "/api/admin/employee", {"id": fid, "action": "face_reset"})
check("after a face reset, check-in is refused until a face is registered", fran.call("POST", "/api/checkin", {**INSIDE, "photo": PERSON_A})[0] == 403)
check("employee re-enrols from the app", fran.call("POST", "/api/face/enroll", {"photo": PERSON_A})[0] == 200)
s, j = fran.call("POST", "/api/checkin", {**INSIDE, "photo": PERSON_A})
check("...and waits for approval", s == 403 and "waiting" in str(j), j)
admin.call("POST", "/api/admin/employee", {"id": uid, "action": "disable"})
check("disabled account is locked out immediately", emp.call("GET", "/api/me")[0] == 401)

# ---- brute force
bf = Client()
codes = [bf.call("POST", "/api/login", {"username": "victim@example.com", "password": f"bad{i}"})[0] for i in range(12)]
check("login throttle: 10 wrong tries then 429", codes[:10] == [401] * 10 and 429 in codes[10:], codes)
check("the throttle is per username, so it cannot lock out someone else", Client().call("POST", "/api/login", {"username": "other@example.com", "password": "x"})[0] == 401)

bad = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
if bad:
    print("FAILED:", bad)
    sys.exit(1)
