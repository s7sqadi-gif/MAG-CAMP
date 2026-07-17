import hashlib
import hmac
import os
import sqlite3
from functools import wraps
from contextlib import closing

from flask import Flask, abort, redirect, render_template_string, request, session, url_for

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB = os.environ.get("DATABASE_PATH", os.path.join(DATA_DIR, "mhoms.db"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "local-development-secret-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("RENDER", "").lower() == "true",
)

ROLE_AR = {
    "services_manager": "مدير الخدمات المساندة",
    "housing_manager": "مدير السكن",
    "housing_supervisor": "مشرف السكن",
    "housing_monitor": "مراقب السكن",
    "maintenance_manager": "مدير الصيانة",
    "maintenance_supervisor": "مشرف الصيانة",
}


def conn():
    connection = sqlite3.connect(DB, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def verify(stored, password):
    try:
        salt, digest = stored.split(":", 1)
        value = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), 150000
        ).hex()
        return hmac.compare_digest(value, digest)
    except (AttributeError, TypeError, ValueError):
        return False


def make_hash(password):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 150000
    ).hex()
    return f"{salt.hex()}:{digest}"


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    with closing(conn()) as c:
        return c.execute(
            "SELECT * FROM users WHERE id=? AND active=1", (uid,)
        ).fetchone()


def login_required(fn):
    @wraps(fn)
    def inner(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        if user["must_change_password"] and request.endpoint not in {
            "change_password",
            "logout",
            "static",
        }:
            return redirect(url_for("change_password"))
        return fn(*args, **kwargs)

    return inner


def is_admin(user):
    return user["role"] in ("services_manager", "housing_manager")


def assigned_clause(user, alias="r"):
    """Return SQL and parameters restricting a supervisor to all assigned ranges."""
    if user["role"] != "housing_supervisor":
        return "1=1", []
    with closing(conn()) as c:
        ranges = c.execute(
            "SELECT room_start, room_end FROM assignments WHERE user_id=? ORDER BY id",
            (user["id"],),
        ).fetchall()
    valid = [(int(r["room_start"]), int(r["room_end"])) for r in ranges if r["room_start"] is not None and r["room_end"] is not None]
    if not valid:
        return "1=0", []
    clauses = []
    params = []
    for start, end in valid:
        low, high = sorted((start, end))
        clauses.append(f"CAST({alias}.room_no AS INTEGER) BETWEEN ? AND ?")
        params.extend([low, high])
    return "(" + " OR ".join(clauses) + ")", params


BASE = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}}</title><link rel="stylesheet" href="{{url_for('static',filename='style.css')}}"><style>
body{font-family:Tahoma,Arial;background:#f4f6f8;margin:0}.top{background:#123b32;color:white;padding:14px 24px;display:flex;justify-content:space-between;align-items:center}.wrap{display:flex;min-height:calc(100vh - 70px)}aside{width:220px;background:white;padding:18px;box-shadow:0 0 8px #ccc}aside a{display:block;padding:10px;color:#123b32;text-decoration:none;border-bottom:1px solid #eee}main{flex:1;padding:24px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}.card{background:white;border-radius:12px;padding:18px;box-shadow:0 2px 8px #ddd}.num{font-size:28px;font-weight:bold}.tbl{width:100%;border-collapse:collapse;background:white}.tbl th,.tbl td{padding:10px;border-bottom:1px solid #eee;text-align:right}.search{padding:10px;width:min(420px,90%);margin-bottom:14px}.btn{background:#123b32;color:white;border:0;border-radius:7px;padding:10px 16px}.login{max-width:390px;margin:10vh auto;background:white;padding:28px;border-radius:14px;box-shadow:0 3px 18px #bbb}.err{color:#b00020}.ok{color:#0a6b3c}
</style></head><body><div class="top"><div><b>MAG CAMP</b><br><small>نظام إدارة سكن ولي العهد</small></div>{% if u %}<div>{{u['display_name']}} — {{roles.get(u['role'],u['role'])}} | <a style="color:white" href="{{url_for('logout')}}">خروج</a></div>{% endif %}</div>{% if u %}<div class="wrap"><aside><a href="{{url_for('dashboard')}}">الرئيسية</a><a href="{{url_for('workers')}}">العمال</a><a href="{{url_for('rooms')}}">الغرف</a>{% if admin %}<a href="{{url_for('users')}}">المستخدمون</a>{% endif %}<a href="{{url_for('change_password')}}">تغيير كلمة المرور</a></aside><main>{{body|safe}}</main></div>{% else %}{{body|safe}}{% endif %}</body></html>"""


def page(body, title="MAG CAMP", user=None, **context):
    return render_template_string(
        BASE,
        title=title,
        body=render_template_string(body, **context),
        u=user,
        roles=ROLE_AR,
        admin=bool(user and is_admin(user)),
    )


@app.get("/health")
def health():
    try:
        with closing(conn()) as c:
            c.execute("SELECT 1").fetchone()
            users = c.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
        return {"status": "ok", "database": "ok", "active_users": users}, 200
    except sqlite3.Error:
        return {"status": "error", "database": "unavailable"}, 503


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))
    error = ""
    if request.method == "POST":
        employee_no = request.form.get("employee_no", "").strip()
        password = request.form.get("password", "")
        with closing(conn()) as c:
            user = c.execute(
                "SELECT * FROM users WHERE employee_no=? AND active=1",
                (employee_no,),
            ).fetchone()
            if user and verify(user["password_hash"], password):
                session.clear()
                session["uid"] = user["id"]
                c.execute(
                    "UPDATE users SET last_login=datetime('now') WHERE id=?", (user["id"],)
                )
                c.commit()
                return redirect(
                    url_for("change_password")
                    if user["must_change_password"]
                    else url_for("dashboard")
                )
        error = "الرقم الوظيفي أو كلمة المرور غير صحيحة"
    body = """<div class="login"><h2>تسجيل الدخول</h2>{% if error %}<p class="err">{{error}}</p>{% endif %}<form method="post"><label>الرقم الوظيفي</label><br><input class="search" name="employee_no" required><br><label>كلمة المرور</label><br><input class="search" type="password" name="password" required><br><button class="btn">دخول</button></form></div>"""
    return page(body, "تسجيل الدخول", error=error)


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    user = current_user()
    error = ""
    success = ""
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not verify(user["password_hash"], current):
            error = "كلمة المرور الحالية غير صحيحة"
        elif len(new) < 6:
            error = "كلمة المرور الجديدة يجب ألا تقل عن 6 أحرف أو أرقام"
        elif new != confirm:
            error = "تأكيد كلمة المرور غير مطابق"
        else:
            with closing(conn()) as c:
                c.execute(
                    "UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                    (make_hash(new), user["id"]),
                )
                c.commit()
            success = "تم تغيير كلمة المرور بنجاح"
    body = """<div class="card"><h2>تغيير كلمة المرور</h2>{% if required %}<p>يجب تغيير كلمة المرور الافتراضية قبل متابعة استخدام النظام.</p>{% endif %}{% if error %}<p class="err">{{error}}</p>{% endif %}{% if success %}<p class="ok">{{success}}</p><p><a class="btn" href="{{url_for('dashboard')}}">الانتقال للوحة التحكم</a></p>{% else %}<form method="post"><label>كلمة المرور الحالية</label><br><input class="search" type="password" name="current_password" required><br><label>كلمة المرور الجديدة</label><br><input class="search" type="password" name="new_password" required><br><label>تأكيد كلمة المرور الجديدة</label><br><input class="search" type="password" name="confirm_password" required><br><button class="btn">حفظ</button></form>{% endif %}</div>"""
    return page(body, "تغيير كلمة المرور", user, error=error, success=success, required=bool(user["must_change_password"]))


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def dashboard():
    user = current_user()
    clause, args = assigned_clause(user, "r")
    with closing(conn()) as c:
        rooms_count = c.execute(
            f"SELECT COUNT(*) FROM rooms r WHERE {clause}", args
        ).fetchone()[0]
        workers_count = c.execute(
            f"""SELECT COUNT(*) FROM workers w WHERE archived=0 AND EXISTS(
                SELECT 1 FROM rooms r WHERE r.room_no=w.room_no AND {clause}
            )""",
            args,
        ).fetchone()[0]
        total_users = c.execute(
            "SELECT COUNT(*) FROM users WHERE active=1"
        ).fetchone()[0]
    body = """<h2>لوحة التحكم</h2><div class="cards"><div class="card"><div>الغرف المتاحة لك</div><div class="num">{{rooms}}</div></div><div class="card"><div>العمال الظاهرون لك</div><div class="num">{{workers}}</div></div>{% if admin %}<div class="card"><div>المستخدمون النشطون</div><div class="num">{{users}}</div></div>{% endif %}</div>"""
    return page(
        body,
        "الرئيسية",
        user,
        rooms=rooms_count,
        workers=workers_count,
        users=total_users,
        admin=is_admin(user),
    )


@app.get("/workers")
@login_required
def workers():
    user = current_user()
    query = request.args.get("q", "").strip()
    clause, args = assigned_clause(user, "r")
    sql = f"""SELECT w.* FROM workers w JOIN rooms r ON r.room_no=w.room_no
              WHERE w.archived=0 AND {clause}"""
    params = list(args)
    if query:
        sql += " AND (w.employee_no LIKE ? OR w.full_name LIKE ? OR w.room_no LIKE ?)"
        params += [f"%{query}%"] * 3
    sql += " ORDER BY CAST(w.room_no AS INTEGER), w.full_name LIMIT 300"
    with closing(conn()) as c:
        rows = c.execute(sql, params).fetchall()
    body = """<h2>العمال</h2><form><input class="search" name="q" value="{{q}}" placeholder="بحث بالرقم أو الاسم أو الغرفة"><button class="btn">بحث</button></form><table class="tbl"><tr><th>الرقم الوظيفي</th><th>الاسم</th><th>الجنسية</th><th>المنطقة</th><th>الغرفة</th></tr>{% for x in rows %}<tr><td>{{x['employee_no']}}</td><td>{{x['full_name']}}</td><td>{{x['nationality']}}</td><td>{{x['zone']}}</td><td>{{x['room_no']}}</td></tr>{% endfor %}</table><p>المعروض: {{rows|length}}</p>"""
    return page(body, "العمال", user, rows=rows, q=query)


@app.get("/rooms")
@login_required
def rooms():
    user = current_user()
    query = request.args.get("q", "").strip()
    clause, args = assigned_clause(user, "r")
    sql = f"""SELECT r.*, COUNT(w.id) occupied FROM rooms r
              LEFT JOIN workers w ON w.room_no=r.room_no AND w.archived=0
              WHERE {clause}"""
    params = list(args)
    if query:
        sql += " AND r.room_no LIKE ?"
        params.append(f"%{query}%")
    sql += " GROUP BY r.id ORDER BY CAST(r.room_no AS INTEGER) LIMIT 500"
    with closing(conn()) as c:
        rows = c.execute(sql, params).fetchall()
    body = """<h2>الغرف</h2><form><input class="search" name="q" value="{{q}}" placeholder="رقم الغرفة"><button class="btn">بحث</button></form><table class="tbl"><tr><th>المنطقة</th><th>رقم الغرفة</th><th>السعة</th><th>المشغول</th><th>الشاغر</th></tr>{% for x in rows %}<tr><td>{{x['zone']}}</td><td>{{x['room_no']}}</td><td>{{x['capacity']}}</td><td>{{x['occupied']}}</td><td>{{x['capacity']-x['occupied']}}</td></tr>{% endfor %}</table>"""
    return page(body, "الغرف", user, rows=rows, q=query)


@app.get("/users")
@login_required
def users():
    user = current_user()
    if not is_admin(user):
        abort(403)
    with closing(conn()) as c:
        rows = c.execute(
            """SELECT u.*, GROUP_CONCAT(a.room_text, '، ') room_text
               FROM users u LEFT JOIN assignments a ON a.user_id=u.id
               GROUP BY u.id ORDER BY u.active DESC, u.role, u.display_name"""
        ).fetchall()
    body = """<h2>المستخدمون والصلاحيات</h2><table class="tbl"><tr><th>الرقم</th><th>الاسم</th><th>الدور</th><th>نطاق الغرف</th><th>الحالة</th></tr>{% for x in rows %}<tr><td>{{x['employee_no']}}</td><td>{{x['display_name']}}</td><td>{{roles.get(x['role'],x['role'])}}</td><td>{{x['room_text'] or '-'}}</td><td>{{'نشط' if x['active'] else 'موقوف'}}</td></tr>{% endfor %}</table>"""
    return page(body, "المستخدمون", user, rows=rows, roles=ROLE_AR)


@app.errorhandler(403)
def forbidden(_error):
    user = current_user()
    return page(
        '<div class="card"><h2>غير مصرح</h2><p>ليس لديك صلاحية لفتح هذه الصفحة.</p></div>',
        "غير مصرح",
        user,
    ), 403


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
