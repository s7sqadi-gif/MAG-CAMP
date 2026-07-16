
from flask import Flask, render_template, request, redirect, url_for, session, abort, flash
from pathlib import Path
from datetime import datetime, timedelta, date
import sqlite3, hashlib, hmac, os

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "mhoms.db"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "mag-camp-change-this-secret")

ROLE_AR = {
    "services_manager":"مدير الخدمات المساندة والأصول",
    "housing_manager":"مدير السكن",
    "housing_supervisor":"مشرف السكن",
    "housing_monitor":"مراقب السكن",
    "maintenance_manager":"مدير الصيانة",
    "maintenance_supervisor":"مشرف الصيانة",
}

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def verify_password(stored, password):
    try:
        salt_hex, expected = stored.split(":", 1)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), 150000
        ).hex()
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    conn.close()
    return user

def week_start(day=None):
    day = day or date.today()
    return day - timedelta(days=(day.weekday() - 5) % 7)

def room_ranges(user_id):
    conn = db()
    rows = conn.execute(
        "SELECT room_start,room_end FROM user_room_ranges WHERE user_id=? ORDER BY room_start",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows

def room_allowed(user, room_no):
    if user["role"] in (
        "services_manager","housing_manager","housing_monitor",
        "maintenance_manager","maintenance_supervisor"
    ):
        return True
    if user["role"] != "housing_supervisor":
        return False
    try:
        n = int(room_no)
    except (TypeError, ValueError):
        return False
    return any(r["room_start"] <= n <= r["room_end"] for r in room_ranges(user["id"]))

@app.context_processor
def inject_globals():
    return {"me": current_user(), "ROLE_AR": ROLE_AR}

@app.before_request
def require_login():
    if request.endpoint in ("login", "static"):
        return None
    if not current_user():
        return redirect(url_for("login"))
    return None

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        conn = db()
        user = conn.execute(
            "SELECT * FROM users WHERE username=? AND active=1", (username,)
        ).fetchone()
        if not user or not verify_password(user["password_hash"], password):
            conn.close()
            flash("بيانات الدخول غير صحيحة", "danger")
            return render_template("login.html")
        conn.execute(
            "UPDATE users SET last_login=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), user["id"]),
        )
        conn.commit()
        conn.close()
        session.clear()
        session["uid"] = user["id"]
        return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
def dashboard():
    user = current_user()
    conn = db()
    workers = conn.execute("SELECT COUNT(*) c FROM workers WHERE archived=0").fetchone()["c"]
    rooms = conn.execute("SELECT COUNT(*) c FROM rooms").fetchone()["c"]
    week = str(week_start())

    if user["role"] == "housing_supervisor":
        supervisors = [user]
    else:
        supervisors = conn.execute(
            "SELECT * FROM users WHERE role='housing_supervisor' AND active=1 ORDER BY display_name"
        ).fetchall()

    progress = []
    for supervisor in supervisors:
        ranges = conn.execute(
            "SELECT room_start,room_end FROM user_room_ranges WHERE user_id=?",
            (supervisor["id"],),
        ).fetchall()
        total = 0
        for item in ranges:
            total += conn.execute("""
                SELECT COUNT(*) c FROM rooms
                WHERE CAST(room_no AS INTEGER) BETWEEN ? AND ?
            """, (item["room_start"], item["room_end"])).fetchone()["c"]

        done = conn.execute("""
            SELECT COUNT(DISTINCT room_no) c
            FROM weekly_room_inspections
            WHERE inspector_id=? AND week_start=?
        """, (supervisor["id"], week)).fetchone()["c"]

        progress.append({
            "name": supervisor["display_name"],
            "done": done,
            "total": total,
            "remaining": max(total-done, 0),
        })

    conn.close()
    return render_template(
        "dashboard.html",
        workers=workers,
        rooms=rooms,
        progress=progress,
        week_start=week,
    )

@app.route("/rooms")
def rooms():
    user = current_user()
    q = request.args.get("q","").strip()
    conn = db()
    sql = """
        SELECT r.*, COUNT(w.id) occ
        FROM rooms r
        LEFT JOIN workers w ON w.room_no=r.room_no AND w.archived=0
        WHERE 1=1
    """
    args = []

    if q:
        sql += " AND r.room_no LIKE ?"
        args.append(f"%{q}%")

    if user["role"] == "housing_supervisor":
        ranges = room_ranges(user["id"])
        if not ranges:
            sql += " AND 1=0"
        else:
            sql += " AND (" + " OR ".join(
                ["CAST(r.room_no AS INTEGER) BETWEEN ? AND ?" for _ in ranges]
            ) + ")"
            for item in ranges:
                args.extend([item["room_start"], item["room_end"]])

    sql += " GROUP BY r.id ORDER BY CAST(r.room_no AS INTEGER)"
    room_rows = conn.execute(sql, args).fetchall()

    inspected = set()
    if user["role"] == "housing_supervisor":
        inspected = {
            row["room_no"] for row in conn.execute("""
                SELECT room_no FROM weekly_room_inspections
                WHERE inspector_id=? AND week_start=?
            """, (user["id"], str(week_start()))).fetchall()
        }

    conn.close()
    return render_template("rooms.html", rooms=room_rows, inspected=inspected, q=q)

@app.route("/room/<room_no>")
def room_detail(room_no):
    user = current_user()
    if not room_allowed(user, room_no):
        abort(403)

    conn = db()
    room = conn.execute("SELECT * FROM rooms WHERE room_no=?", (room_no,)).fetchone()
    workers = conn.execute("""
        SELECT * FROM workers
        WHERE room_no=? AND archived=0
        ORDER BY full_name
    """, (room_no,)).fetchall()
    conn.close()

    return render_template("room_detail.html", room=room, workers=workers)

@app.route("/room/<room_no>/inspect", methods=["GET","POST"])
def inspect_room(room_no):
    user = current_user()
    if user["role"] != "housing_supervisor" or not room_allowed(user, room_no):
        abort(403)

    conn = db()
    registered = conn.execute(
        "SELECT COUNT(*) c FROM workers WHERE room_no=? AND archived=0",
        (room_no,),
    ).fetchone()["c"]

    if request.method == "POST":
        actual = int(request.form.get("actual_count") or 0)
        conn.execute("""
            INSERT INTO weekly_room_inspections(
                week_start,room_no,inspector_id,registered_count,actual_count,
                cleanliness,extra_employee_nos,notes,inspected_at
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(week_start,room_no,inspector_id) DO UPDATE SET
                registered_count=excluded.registered_count,
                actual_count=excluded.actual_count,
                cleanliness=excluded.cleanliness,
                extra_employee_nos=excluded.extra_employee_nos,
                notes=excluded.notes,
                inspected_at=excluded.inspected_at
        """, (
            str(week_start()), room_no, user["id"], registered, actual,
            request.form.get("cleanliness"),
            request.form.get("extra_employee_nos",""),
            request.form.get("notes",""),
            datetime.now().isoformat(timespec="seconds"),
        ))
        conn.commit()
        conn.close()
        flash("تم حفظ الجولة الأسبوعية للغرفة", "success")
        return redirect(url_for("rooms"))

    conn.close()
    return render_template("inspect_room.html", room_no=room_no, registered=registered)

@app.errorhandler(403)
def forbidden(_):
    return render_template("error.html", message="غير مصرح لك بالدخول إلى هذه الصفحة"), 403

@app.errorhandler(500)
def server_error(_):
    return render_template("error.html", message="حدث خطأ داخلي، راجع سجل Render"), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","10000")))
