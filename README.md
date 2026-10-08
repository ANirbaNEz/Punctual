# Punctual

Location-verified employee attendance. Employees check in and out from their phone or PC; the app confirms they are at the office, records where and when, and tells them whether they are early, on time or late. Admins see everything live on a dashboard.

Both screens are installable as apps (PWA) with their own icons: **Punctual** for employees, **Punctual Admin** for admins.

| Employee (phone) | Employee, checked in | Admin dashboard |
|---|---|---|
| ![Employee home](docs/employee-home.png) | ![Checked in](docs/employee-checked-in.png) | ![Admin](docs/admin-dashboard.png) |

> Screenshots use made-up demo data.

## Features

**Employee**
- Sign up, log in, then check in and out with one tap on a watch-face dial
- Live location fix (waits for a good GPS reading), reverse-geocoded to a street address with OpenStreetMap
- Instant feedback: "You're early by 8 min, good job!" / "You're 24 min late, try better tomorrow!"
- Today's card (time and address for check-in and check-out) and the last 10 days
- Blocked from checking in when outside the office circle
- **Face check:** a selfie is taken at sign-up and approved by the admin, then a selfie on every check-in and check-out that must match the registered face

**Admin**
- Daily attendance table: who, when, where, early/on time/late, with a date picker
- Stat cards: checked in, on time or early, late, not checked in, awaiting approval
- Approve or disable employees, set each person's shift start time, see late count and days present for the month
- Office setup on a map: drag the pin or use your current location, resize the attendance circle (20 m to 1 km), set the late grace period

## Tech stack

| Layer | Choice |
|---|---|
| Backend | Python, FastAPI, REST API |
| Database | MySQL (`schema.sql`) via PyMySQL |
| Auth | JWT in an HttpOnly cookie, role-based access (admin / employee), scrypt-hashed passwords, login throttling |
| Frontend | Plain HTML, CSS and JavaScript, Leaflet maps (no build step) |
| Location | Browser Geolocation API, OpenStreetMap Nominatim for addresses |
| Face check | OpenCV (YuNet face detector + SFace recogniser, small ONNX models), camera via `getUserMedia` |
| Install | PWA: web manifests, service worker, separate icons for the two apps |

## How it works

- **Geofence:** the server measures the distance between the employee's position and the office point (haversine formula) and rejects check-in outside the admin's radius.
- **Early, on time, late:** compared with the employee's own shift start plus the admin's grace period, in the server's local time.
- **One record per employee per day:** enforced by a unique key in the database, and check-in and check-out are stored as real `DATETIME` values.
- **Approvals:** self-registered accounts stay pending until an admin approves them. Admins cannot mark attendance.
- **Face check:** the sign-up selfie is stored as a 128-number face signature and waits for admin approval. After that every check-in and check-out selfie is compared with it (cosine similarity, threshold 0.363) and rejected if it does not match. The score and the selfie are saved so the admin can review them.
- **Addresses** are looked up on the server after the response is sent, so check-in is not slowed down by the geocoder.

## Run it locally

Requires Python 3.12+ and MySQL (or MariaDB). The first start downloads the two face models (about 39 MB) into `models/`.

```bash
# 1. create the database and tables
mysql -u root -p < schema.sql          # or run schema.sql in HeidiSQL / phpMyAdmin

# 2. install dependencies
pip install -r requirements.txt

# 3. start the app (PowerShell shown; use export on macOS/Linux)
$env:MYSQL_USER="root"; $env:MYSQL_PASSWORD="your-mysql-password"; $env:ADMIN_PASSWORD="choose-a-password"; python main.py
```

Open `http://localhost:3000/admin` and log in as `admin` with your `ADMIN_PASSWORD`. Employees use `http://localhost:3000`.

| Variable | Default | Purpose |
|---|---|---|
| `MYSQL_HOST` / `MYSQL_PORT` | `127.0.0.1` / `3306` | Database server |
| `MYSQL_USER` / `MYSQL_PASSWORD` | `root` / empty | Database login |
| `MYSQL_DB` | `attendance` | Database name |
| `ADMIN_PASSWORD` | `admin123` | Password for the `admin` account, used on first start only |
| `PORT` | `3000` | Web port |

### Try it on a phone

Phones only allow location access on HTTPS. For a quick demo, expose your local app with a free tunnel:

```bash
cloudflared tunnel --protocol http2 --url http://localhost:3000
```

Open the `https://…trycloudflare.com` address it prints on your phone, then use the browser's **Install app** option to add the icon to the home screen.

## Project structure

```
main.py            API, auth, geofence and attendance logic
schema.sql         MySQL tables
employee.html      Employee app
admin.html         Admin app
static/            PWA manifests, icons, service worker
models/            face models (downloaded on first start, not committed)
photos/            selfies (created at runtime, not committed)
docs/              README screenshots
```

## Limits and ideas

- Times are judged in the server's time zone, so this suits a single office
- No liveness check: a photo or video of the right person held up to the camera can still pass the face match
- Face data is sensitive: the app stores a face signature and selfies, asks for consent at registration, and lets the admin delete a person's face data ("Reset")
- No password reset, CSV export or absence marking yet
- Attendance is only as trustworthy as the phone's GPS; a determined user can spoof it
- Ideas: liveness challenge (blink or head turn), per-shift geofences, monthly reports, email notifications

## License

MIT, see [LICENSE](LICENSE).
