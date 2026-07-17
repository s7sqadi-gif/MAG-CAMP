
from flask import Flask, render_template, request, redirect, url_for, session, abort, flash
import sqlite3, os, hashlib, hmac
from datetime import datetime, timedelta, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "mhoms.db"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-render-mag-camp-2026")

ROLE_AR = {
    "services_manager":"مدير الخدمات المساندة والأصول",
    "housing_manager":"مدير السكن",
    "housing_supervisor":"مشرف السكن",
    "housing_monitor":"مراقب السكن",
    "maintenance_manager":"مدير الصيانة",
    "maintenance_supervisor":"مشرف الصيانة",
}

def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def verify(stored, password):
    try:
        salt_hex, expected = stored.split(":",1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 150000).hex()
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

def current_user():
    uid = session.get("uid")
    if not uid: return None
    c=db(); u=c.execute("SELECT * FROM users WHERE id=? AND active=1",(uid,)).fetchone(); c.close()
    return u

def week_start(d=None):
    d = d or date.today()
    # السبت بداية الأسبوع
    days_since_saturday = (d.weekday() - 5) % 7
    return d - timedelta(days=days_since_saturday)

def room_ranges(user):
    c=db()
    rows=c.execute("SELECT room_start,room_end FROM user_room_ranges WHERE user_id=? ORDER BY room_start",(user["id"],)).fetchall()
    c.close()
    return rows

def room_allowed(user, room_no):
    if user["role"] in ("services_manager","housing_manager","housing_monitor","maintenance_manager","maintenance_supervisor"):
        return True
    if user["role"] != "housing_supervisor": return False
    try: n=int(room_no)
    except: return False
    return any(r["room_start"] <= n <= r["room_end"] for r in room_ranges(user))

@app.context_processor
def inject():
    return {"me":current_user(), "ROLE_AR":ROLE_AR}

@app.before_request
def protect():
    if request.endpoint in ("login","static"): return
    if not current_user(): return redirect(url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        username=request.form.get("username","").strip()
        password=request.form.get("password","")
        c=db(); u=c.execute("SELECT * FROM users WHERE username=? AND active=1",(username,)).fetchone()
        if not u or not verify(u["password_hash"],password):
            c.close(); flash("بيانات الدخول غير صحيحة","danger"); return render_template("login.html")
        c.execute("UPDATE users SET last_login=? WHERE id=?",(datetime.now().isoformat(timespec="seconds"),u["id"]))
        c.commit(); c.close()
        session.clear(); session["uid"]=u["id"]
        return redirect(url_for("dashboard"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

@app.route("/")
def dashboard():
    u=current_user(); c=db()
    workers=c.execute("SELECT COUNT(*) c FROM workers WHERE archived=0").fetchone()["c"]
    rooms=c.execute("SELECT COUNT(*) c FROM rooms").fetchone()["c"]
    ws=str(week_start())
    if u["role"]=="housing_supervisor":
        total=0
        for r in room_ranges(u):
            total += c.execute("""SELECT COUNT(*) c FROM rooms WHERE CAST(room_no AS INTEGER) BETWEEN ? AND ?""",
                               (r["room_start"],r["room_end"])).fetchone()["c"]
        done=c.execute("""SELECT COUNT(DISTINCT room_no) c FROM weekly_room_inspections
                          WHERE inspector_id=? AND week_start=?""",(u["id"],ws)).fetchone()["c"]
        progress=[{"name":u["display_name"],"done":done,"total":total}]
    else:
        supervisors=c.execute("SELECT * FROM users WHERE role='housing_supervisor' AND active=1 ORDER BY display_name").fetchall()
        progress=[]
        for s in supervisors:
            ranges=c.execute("SELECT room_start,room_end FROM user_room_ranges WHERE user_id=?",(s["id"],)).fetchall()
            total=sum(c.execute("""SELECT COUNT(*) c FROM rooms WHERE CAST(room_no AS INTEGER) BETWEEN ? AND ?""",
                                (r["room_start"],r["room_end"])).fetchone()["c"] for r in ranges)
            done=c.execute("""SELECT COUNT(DISTINCT room_no) c FROM weekly_room_inspections
                              WHERE inspector_id=? AND week_start=?""",(s["id"],ws)).fetchone()["c"]
            progress.append({"name":s["display_name"],"done":done,"total":total})
    c.close()
    return render_template("dashboard.html",workers=workers,rooms=rooms,progress=progress,week_start=ws)

@app.route("/rooms")
def rooms():
    u=current_user(); q=request.args.get("q","").strip(); c=db()
    sql="""SELECT r.*,COUNT(w.id) occ FROM rooms r LEFT JOIN workers w
           ON w.room_no=r.room_no AND w.archived=0 WHERE 1=1"""
    args=[]
    if q: sql+=" AND r.room_no LIKE ?"; args.append(f"%{q}%")
    if u["role"]=="housing_supervisor":
        ranges=room_ranges(u)
        if not ranges: sql+=" AND 1=0"
        else:
            sql+=" AND ("+" OR ".join(["CAST(r.room_no AS INTEGER) BETWEEN ? AND ?" for _ in ranges])+")"
            for x in ranges: args += [x["room_start"],x["room_end"]]
    sql+=" GROUP BY r.id ORDER BY CAST(r.room_no AS INTEGER)"
    rows=c.execute(sql,args).fetchall()
    ws=str(week_start())
    inspected={r["room_no"] for r in c.execute("""SELECT room_no FROM weekly_room_inspections
                  WHERE inspector_id=? AND week_start=?""",(u["id"],ws)).fetchall()} if u["role"]=="housing_supervisor" else set()
    c.close()
    return render_template("rooms.html",rooms=rows,inspected=inspected,q=q)

@app.route("/room/<room_no>")
def room_detail(room_no):
    u=current_user()
    if not room_allowed(u,room_no): abort(403)
    c=db()
    room=c.execute("SELECT * FROM rooms WHERE room_no=?",(room_no,)).fetchone()
    workers=c.execute("SELECT * FROM workers WHERE room_no=? AND archived=0 ORDER BY full_name",(room_no,)).fetchall()
    c.close()
    return render_template("room_detail.html",room=room,workers=workers)

@app.route("/room/<room_no>/inspect", methods=["GET","POST"])
def inspect_room(room_no):
    u=current_user()
    if u["role"]!="housing_supervisor" or not room_allowed(u,room_no): abort(403)
    c=db()
    registered=c.execute("SELECT COUNT(*) c FROM workers WHERE room_no=? AND archived=0",(room_no,)).fetchone()["c"]
    if request.method=="POST":
        actual=int(request.form.get("actual_count") or 0)
        c.execute("""INSERT INTO weekly_room_inspections(week_start,room_no,inspector_id,
                   registered_count,actual_count,cleanliness,extra_employee_nos,notes,inspected_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(week_start,room_no,inspector_id) DO UPDATE SET
                   registered_count=excluded.registered_count,actual_count=excluded.actual_count,
                   cleanliness=excluded.cleanliness,extra_employee_nos=excluded.extra_employee_nos,
                   notes=excluded.notes,inspected_at=excluded.inspected_at""",
                  (str(week_start()),room_no,u["id"],registered,actual,
                   request.form.get("cleanliness"),request.form.get("extra_employee_nos",""),
                   request.form.get("notes",""),datetime.now().isoformat(timespec="seconds")))
        c.commit(); c.close()
        flash("تم حفظ الجولة الأسبوعية للغرفة","success")
        return redirect(url_for("rooms"))
    c.close()
    return render_template("inspect_room.html",room_no=room_no,registered=registered)

@app.route("/weekly-progress")
def weekly_progress():
    u=current_user()
    if u["role"] not in ("services_manager","housing_manager"): abort(403)
    return redirect(url_for("dashboard"))

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT","10000")))
