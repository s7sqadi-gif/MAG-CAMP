
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
from http.cookies import SimpleCookie
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import sqlite3, os, html, hashlib, hmac, secrets, json, re
from datetime import datetime

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = ROOT / "uploads"
DB_PATH = DATA_DIR / "mhoms.db"
DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

SESSIONS = {}

ROLE_AR = {
    "admin": "مدير النظام",
    "services_manager": "مدير الخدمات المساندة",
    "housing_manager": "مدير السكن",
    "housing_supervisor": "مشرف السكن",
    "housing_monitor": "مراقب السكن",
    "maintenance_manager": "مدير الصيانة",
    "maintenance_supervisor": "مشرف الصيانة",
}

STATUS_AR = {
    "new": "جديد",
    "in_progress": "تحت التنفيذ",
    "pending_verification": "بانتظار تحقق مشرف السكن",
    "closed": "مغلق",
    "reopened": "معاد للصيانة",
}

def now():
    return datetime.now().isoformat(timespec="seconds")

def esc(v):
    return html.escape("" if v is None else str(v))

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def table_columns(conn, table):
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()

def add_column(conn, table, definition):
    name = definition.split()[0]
    if name not in table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 150000)
    return f"{salt.hex()}:{digest.hex()}"

def verify_password(stored, password):
    try:
        salt_hex, digest_hex = stored.split(":", 1)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), 150000
        ).hex()
        return hmac.compare_digest(actual, digest_hex)
    except Exception:
        return False

def bootstrap():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_no TEXT,
        username TEXT UNIQUE,
        display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'housing_supervisor',
        active INTEGER NOT NULL DEFAULT 1,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        last_login TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS assignments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE,
        room_start INTEGER,
        room_end INTEGER,
        room_text TEXT,
        bathrooms_group TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS rooms(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_no TEXT UNIQUE NOT NULL,
        zone TEXT,
        capacity INTEGER NOT NULL DEFAULT 6,
        active INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS workers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_no TEXT UNIQUE NOT NULL,
        iqama_no TEXT,
        full_name TEXT NOT NULL,
        nationality TEXT,
        profession TEXT,
        zone TEXT,
        room_no TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        archived INTEGER NOT NULL DEFAULT 0,
        created_at TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS bathrooms(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bathroom_no TEXT NOT NULL,
        zone_name TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        UNIQUE(bathroom_no, zone_name)
    );

    CREATE TABLE IF NOT EXISTS inspections(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inspection_type TEXT NOT NULL,
        location_id TEXT NOT NULL,
        zone_name TEXT,
        inspector_id INTEGER NOT NULL,
        registered_count INTEGER,
        actual_count INTEGER,
        cleanliness TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(inspector_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS inspection_people(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inspection_id INTEGER NOT NULL,
        employee_no TEXT NOT NULL,
        worker_id INTEGER,
        registered_room TEXT,
        discrepancy_type TEXT,
        FOREIGN KEY(inspection_id) REFERENCES inspections(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS maintenance_tickets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_no TEXT UNIQUE NOT NULL,
        location_type TEXT NOT NULL,
        location_id TEXT NOT NULL,
        zone_name TEXT,
        category TEXT,
        description TEXT,
        priority TEXT NOT NULL DEFAULT 'normal',
        status TEXT NOT NULL DEFAULT 'new',
        reported_by INTEGER NOT NULL,
        assigned_to INTEGER,
        verification_by INTEGER,
        technician_name TEXT,
        part_name TEXT,
        completion_notes TEXT,
        before_photo TEXT,
        after_photo TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        verified_at TEXT,
        closed_at TEXT,
        FOREIGN KEY(reported_by) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS ticket_updates(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        notes TEXT,
        photo_path TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(ticket_id) REFERENCES maintenance_tickets(id) ON DELETE CASCADE,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_no TEXT UNIQUE NOT NULL,
        request_type TEXT NOT NULL,
        worker_id INTEGER,
        payload_json TEXT NOT NULL,
        requested_by INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        approver_id INTEGER,
        decision_reason TEXT,
        created_at TEXT NOT NULL,
        decided_at TEXT,
        FOREIGN KEY(requested_by) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS audit_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        action TEXT,
        entity_type TEXT,
        entity_id TEXT,
        details_json TEXT,
        created_at TEXT NOT NULL
    );
    """)

    # ترقية قواعد البيانات القديمة دون حذف البيانات
    for definition in [
        "employee_no TEXT", "username TEXT", "display_name TEXT",
        "password_hash TEXT", "role TEXT DEFAULT 'housing_supervisor'",
        "active INTEGER DEFAULT 1", "must_change_password INTEGER DEFAULT 0",
        "last_login TEXT", "created_at TEXT"
    ]:
        add_column(conn, "users", definition)

    # حساب المدير العام وحساب حسام
    defaults = [
        ("ADMIN", "admin", "مدير النظام", "admin"),
        ("109753", "109753", "حسام عبدالرحمن قاضي", "housing_manager"),
    ]
    for emp, username, name, role in defaults:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            conn.execute(
                """INSERT INTO users(employee_no,username,display_name,password_hash,role,
                   active,must_change_password,created_at)
                   VALUES(?,?,?,?,?,1,0,?)""",
                (emp, username, name, password_hash("123456"), role, now()),
            )
        else:
            # يضمن أن الحساب يعمل حتى لو كانت النسخة القديمة ناقصة
            conn.execute(
                """UPDATE users SET employee_no=COALESCE(NULLIF(employee_no,''),?),
                   display_name=COALESCE(NULLIF(display_name,''),?),
                   role=COALESCE(NULLIF(role,''),?),
                   active=1 WHERE username=?""",
                (emp, name, role, username),
            )

    # غرف افتراضية عند أول تشغيل فقط
    if conn.execute("SELECT COUNT(*) c FROM rooms").fetchone()["c"] == 0:
        conn.executemany(
            "INSERT INTO rooms(room_no,zone,capacity) VALUES(?,?,6)",
            [(str(i), "غير محدد") for i in range(1, 973)]
        )

    conn.commit()
    conn.close()

bootstrap()

def is_admin(user):
    return user["role"] in ("admin", "services_manager", "housing_manager")

def assignment(user_id):
    conn = db()
    row = conn.execute("SELECT * FROM assignments WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row

def room_allowed(user, room_no):
    if user["role"] in ("admin","services_manager","housing_manager","housing_monitor",
                        "maintenance_manager","maintenance_supervisor"):
        return True
    if user["role"] != "housing_supervisor":
        return False
    a = assignment(user["id"])
    try:
        n = int(room_no)
    except Exception:
        return False
    return bool(a and a["room_start"] is not None and a["room_end"] is not None
                and a["room_start"] <= n <= a["room_end"])

def bathroom_allowed(user, zone):
    if user["role"] in ("admin","services_manager","housing_manager","housing_monitor",
                        "maintenance_manager","maintenance_supervisor"):
        return True
    if user["role"] != "housing_supervisor":
        return False
    a = assignment(user["id"])
    return bool(a and zone and zone in (a["bathrooms_group"] or ""))

def responsible_supervisor(location_type, location_id, zone=""):
    conn = db()
    if location_type == "room":
        try:
            n = int(location_id)
        except Exception:
            n = -1
        row = conn.execute("""
            SELECT u.id FROM users u
            JOIN assignments a ON a.user_id=u.id
            WHERE u.role='housing_supervisor'
              AND a.room_start IS NOT NULL
              AND ? BETWEEN a.room_start AND a.room_end
            LIMIT 1
        """, (n,)).fetchone()
    else:
        row = conn.execute("""
            SELECT u.id FROM users u
            JOIN assignments a ON a.user_id=u.id
            WHERE u.role='housing_supervisor'
              AND a.bathrooms_group LIKE ?
            LIMIT 1
        """, (f"%{zone}%",)).fetchone()
    conn.close()
    return row["id"] if row else None

def request_no(prefix):
    return f"{prefix}-{datetime.now().strftime('%Y%m%d')}-{secrets.randbelow(90000)+10000}"

def save_image(file_tuple, prefix):
    if not file_tuple or not file_tuple[2]:
        return None
    filename, ctype, data = file_tuple
    ext = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"
    }.get(ctype, Path(filename).suffix.lower() or ".jpg")
    safe = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}{ext}"
    (UPLOAD_DIR / safe).write_bytes(data)
    return safe

CSS = """
*{box-sizing:border-box}body{margin:0;font-family:Arial,Tahoma,sans-serif;background:#f3f7fb;color:#1e293b}
a{text-decoration:none;color:inherit}header{height:72px;background:#fff;display:flex;align-items:center;
justify-content:space-between;padding:12px 22px;border-bottom:1px solid #dbe5ef;position:sticky;top:0;z-index:5}
.brand{display:flex;align-items:center;gap:12px}.logo{font-weight:900;font-size:28px;color:#f05a28}
.layout{display:grid;grid-template-columns:230px 1fr;min-height:calc(100vh - 72px)}
aside{background:#0d4770;padding:18px}aside a{display:block;color:#fff;padding:12px;border-radius:9px;margin:4px 0}
aside a:hover{background:#16628f}main{padding:24px}.panel,.card{background:#fff;border-radius:14px;padding:20px;
box-shadow:0 5px 18px rgba(15,23,42,.07);margin-bottom:18px}.cards{display:grid;grid-template-columns:repeat(5,1fr);
gap:14px}.value{font-size:32px;font-weight:800;margin-top:8px;color:#0d6fa6}.filters,.actions,.statline{display:flex;gap:10px;
flex-wrap:wrap;align-items:center}input,select,textarea{width:100%;padding:11px;border:1px solid #cbd5e1;border-radius:9px;
font:inherit}textarea{min-height:95px}.filters input{max-width:350px}.btn{border:0;border-radius:9px;padding:10px 15px;
cursor:pointer;display:inline-block;background:#e2e8f0}.primary{background:#0d8ecf;color:white}.danger{background:#dc3545;color:white}
.small{padding:6px 10px;font-size:13px}.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse}
th,td{padding:11px;border-bottom:1px solid #e5edf5;text-align:right}.roomgrid{display:grid;
grid-template-columns:repeat(5,1fr);gap:12px}.roomcard{background:#fff;padding:15px;border-radius:12px;
box-shadow:0 3px 12px rgba(15,23,42,.06);display:flex;flex-direction:column;gap:8px}.over{border:2px solid #dc3545}
.login{min-height:100vh;display:grid;place-items:center;background:linear-gradient(135deg,#0e74a8,#0b4368)}
.loginbox{width:min(420px,92vw);background:#fff;padding:30px;border-radius:18px;text-align:center}
.notice{background:#fee2e2;color:#991b1b;padding:10px;border-radius:9px;margin:12px 0}.muted{color:#64748b;font-size:13px}
.ticketimg,.timelineitem img{max-width:320px;max-height:260px;object-fit:cover;border-radius:10px}
.timelineitem{border-right:4px solid #0d8ecf;padding:12px;margin:10px 0;background:#f8fafc}
.narrow{max-width:650px;margin-inline:auto}@media(max-width:1000px){.cards{grid-template-columns:repeat(2,1fr)}
.roomgrid{grid-template-columns:repeat(2,1fr)}}@media(max-width:700px){.layout{grid-template-columns:1fr}
aside{display:flex;overflow:auto;gap:5px;padding:8px}aside a{white-space:nowrap}.cards,.roomgrid{grid-template-columns:1fr}
main{padding:12px}header{font-size:13px}}
"""

def nav(user):
    links = [("الرئيسية","/"),("العمال","/workers"),("الغرف","/rooms")]
    if user["role"] in ("admin","services_manager","housing_manager","housing_supervisor","housing_monitor"):
        links += [("دورات المياه","/bathrooms"),("الجولات","/rounds")]
    links += [("بلاغات الصيانة","/maintenance"),("طلبات التسكين","/requests")]
    if user["role"] in ("admin","services_manager","housing_manager","housing_supervisor"):
        links.append(("الاعتمادات","/approvals"))
    if is_admin(user):
        links += [("المستخدمون","/users"),("سجل النشاط","/audit")]
    return "".join(f"<a href='{url}'>{title}</a>" for title,url in links)

def layout(body, user, title="MAG CAMP"):
    return f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title>
    <style>{CSS}</style></head><body><header><div class="brand"><div class="logo">MAG</div>
    <div><b>MAG CAMP</b><br><small>نظام إدارة وتشغيل السكن</small></div></div>
    <div><b>{esc(user["display_name"])}</b><br><small>{esc(ROLE_AR.get(user["role"],user["role"]))}</small>
    | <a href="/logout">خروج</a></div></header><div class="layout"><aside>{nav(user)}</aside>
    <main>{body}</main></div></body></html>"""

class App(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def send_html(self, text, code=200):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, path):
        self.send_response(302)
        self.send_header("Location", path)
        self.end_headers()

    def form(self):
        n = int(self.headers.get("Content-Length","0") or 0)
        return {k:v[0] for k,v in parse_qs(self.rfile.read(n).decode("utf-8","replace")).items()}

    def multipart(self):
        n = int(self.headers.get("Content-Length","0") or 0)
        body = self.rfile.read(n)
        ct = self.headers.get("Content-Type","")
        msg = BytesParser(policy=default).parsebytes(
            (f"Content-Type: {ct}\r\nMIME-Version: 1.0\r\n\r\n").encode()+body
        )
        fields, files = {}, {}
        if msg.is_multipart():
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                filename = part.get_filename()
                data = part.get_payload(decode=True) or b""
                if filename:
                    files[name] = (filename, part.get_content_type(), data)
                elif name:
                    fields[name] = data.decode("utf-8","replace")
        return fields, files

    def session_user(self):
        cookie = SimpleCookie(self.headers.get("Cookie"))
        token = cookie.get("magcamp")
        session = SESSIONS.get(token.value) if token else None
        return session["user"] if session else None

    def require_user(self):
        user = self.session_user()
        if not user:
            self.redirect("/login")
            return None
        return user

    def do_GET(self):
        p = urlparse(self.path)
        path = p.path

        if path.startswith("/uploads/"):
            filename = os.path.basename(path)
            f = UPLOAD_DIR / filename
            if not f.is_file():
                return self.send_html("غير موجود",404)
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type","image/jpeg")
            self.send_header("Content-Length",str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/login":
            return self.login()
        if path == "/logout":
            self.send_response(302)
            self.send_header("Set-Cookie","magcamp=; Max-Age=0; Path=/")
            self.send_header("Location","/login")
            self.end_headers()
            return

        user = self.require_user()
        if not user:
            return

        try:
            if path == "/": return self.dashboard(user)
            if path == "/workers": return self.workers(user, parse_qs(p.query))
            if path == "/rooms": return self.rooms(user, parse_qs(p.query))
            if re.fullmatch(r"/room/[^/]+", path): return self.room(user, path.split("/")[2])
            if re.fullmatch(r"/room/[^/]+/inspect", path): return self.room_inspect_form(user,path.split("/")[2])
            if path == "/bathrooms": return self.bathrooms(user, parse_qs(p.query))
            if re.fullmatch(r"/bathroom/\d+/inspect", path): return self.bath_inspect_form(user,int(path.split("/")[2]))
            if path == "/rounds": return self.rounds(user)
            if path == "/maintenance": return self.maintenance(user, parse_qs(p.query))
            if path == "/ticket/new": return self.ticket_form(user, parse_qs(p.query))
            if re.fullmatch(r"/ticket/\d+", path): return self.ticket(user,int(path.split("/")[2]))
            if path == "/requests": return self.requests(user)
            if path == "/requests/new": return self.request_form(user)
            if path == "/approvals": return self.approvals(user)
            if path == "/users": return self.users(user)
            if path == "/audit": return self.audit(user)
            return self.send_html("الصفحة غير موجودة",404)
        except Exception as ex:
            print("GET ERROR:", repr(ex))
            return self.send_html(layout(f"<div class='panel'><h2>حدث خطأ</h2><p>{esc(ex)}</p></div>",user),500)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            return self.login_post()

        user = self.require_user()
        if not user:
            return

        try:
            if re.fullmatch(r"/room/[^/]+/inspect", path): return self.room_inspect_post(user,path.split("/")[2])
            if re.fullmatch(r"/bathroom/\d+/inspect", path): return self.bath_inspect_post(user,int(path.split("/")[2]))
            if path == "/ticket/new": return self.ticket_post(user)
            if re.fullmatch(r"/ticket/\d+/update", path): return self.ticket_update(user,int(path.split("/")[2]))
            if path == "/requests/new": return self.request_post(user)
            if re.fullmatch(r"/approvals/\d+", path): return self.approval_decide(user,int(path.split("/")[2]))
            if path == "/users/new": return self.user_create(user)
            return self.send_html("الصفحة غير موجودة",404)
        except Exception as ex:
            print("POST ERROR:", repr(ex))
            return self.send_html(layout(f"<div class='panel'><h2>حدث خطأ</h2><p>{esc(ex)}</p></div>",user),500)

    def login(self, message=""):
        note = f"<div class='notice'>{esc(message)}</div>" if message else ""
        self.send_html(f"""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head>
        <body class="login"><form class="loginbox" method="post"><div class="logo">MAG</div>
        <h1>MAG CAMP</h1><p>نظام إدارة وتشغيل السكن</p>{note}
        <input name="username" placeholder="الرقم الوظيفي أو admin" required><br><br>
        <input type="password" name="password" placeholder="كلمة المرور" required><br><br>
        <button class="btn primary" style="width:100%">تسجيل الدخول</button>
        <p class="muted">كلمة المرور الافتراضية: 123456</p></form></body></html>""")

    def login_post(self):
        f = self.form()
        conn = db()
        user = conn.execute(
            "SELECT * FROM users WHERE username=? AND COALESCE(active,1)=1",
            (f.get("username","").strip(),)
        ).fetchone()

        if not user or not verify_password(user["password_hash"] or "", f.get("password","")):
            conn.close()
            return self.login("بيانات الدخول غير صحيحة")

        # تحديث آمن: العمود موجود بسبب bootstrap، ومع ذلك لا نعتمد عليه لتسجيل الدخول
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (now(),user["id"]))
        conn.commit()
        conn.close()

        token = secrets.token_urlsafe(32)
        SESSIONS[token] = {"user": dict(user)}
        self.send_response(302)
        self.send_header("Set-Cookie",f"magcamp={token}; Path=/; HttpOnly; SameSite=Lax")
        self.send_header("Location","/")
        self.end_headers()

    def dashboard(self,user):
        conn=db()
        total_workers=conn.execute("SELECT COUNT(*) c FROM workers WHERE COALESCE(archived,0)=0").fetchone()["c"]
        total_rooms=conn.execute("SELECT COUNT(*) c FROM rooms WHERE COALESCE(active,1)=1").fetchone()["c"]
        open_tickets=conn.execute("SELECT COUNT(*) c FROM maintenance_tickets WHERE status<>'closed'").fetchone()["c"]
        verify=conn.execute("SELECT COUNT(*) c FROM maintenance_tickets WHERE status='pending_verification'").fetchone()["c"]
        pending=conn.execute("SELECT COUNT(*) c FROM requests WHERE status='pending'").fetchone()["c"]
        conn.close()
        body=f"""<div class="cards">
        <div class="card">إجمالي العمال<div class="value">{total_workers}</div></div>
        <div class="card">إجمالي الغرف<div class="value">{total_rooms}</div></div>
        <div class="card">بلاغات مفتوحة<div class="value">{open_tickets}</div></div>
        <div class="card">بانتظار التحقق<div class="value">{verify}</div></div>
        <div class="card">طلبات معلقة<div class="value">{pending}</div></div></div>
        <div class="panel"><h2>مرحبًا {esc(user["display_name"])}</h2>
        <p>النظام يعمل بنجاح. استخدم القائمة للوصول إلى الجولات والصيانة والتسكين.</p></div>"""
        self.send_html(layout(body,user))

    def workers(self,user,q):
        term=(q.get("q") or [""])[0]
        conn=db()
        sql="SELECT * FROM workers WHERE COALESCE(archived,0)=0"
        args=[]
        if term:
            sql+=" AND (employee_no LIKE ? OR full_name LIKE ? OR iqama_no LIKE ? OR room_no LIKE ?)"
            x=f"%{term}%"; args=[x,x,x,x]
        sql+=" ORDER BY full_name LIMIT 1000"
        rows=conn.execute(sql,args).fetchall()
        conn.close()
        html_rows="".join(f"<tr><td>{esc(r['employee_no'])}</td><td>{esc(r['full_name'])}</td>"
                          f"<td>{esc(r['room_no'])}</td><td>{esc(r['zone'])}</td></tr>" for r in rows)
        body=f"""<div class="panel"><form class="filters"><input name="q" value="{esc(term)}"
        placeholder="الاسم أو الرقم الوظيفي أو الغرفة"><button class="btn primary">بحث</button></form></div>
        <div class="panel tablewrap"><table><tr><th>الرقم الوظيفي</th><th>الاسم</th><th>الغرفة</th><th>الزون</th></tr>
        {html_rows}</table></div>"""
        self.send_html(layout(body,user))

    def rooms(self,user,q):
        term=(q.get("q") or [""])[0]
        conn=db()
        sql="""SELECT r.room_no,r.zone,r.capacity,COUNT(w.id) occ
               FROM rooms r LEFT JOIN workers w
               ON w.room_no=r.room_no AND COALESCE(w.archived,0)=0
               WHERE COALESCE(r.active,1)=1"""
        args=[]
        if term:
            sql+=" AND r.room_no LIKE ?"; args.append(f"%{term}%")
        if user["role"]=="housing_supervisor":
            a=assignment(user["id"])
            if a and a["room_start"] is not None:
                sql+=" AND CAST(r.room_no AS INTEGER) BETWEEN ? AND ?"
                args += [a["room_start"],a["room_end"]]
            else:
                sql+=" AND 1=0"
        sql+=" GROUP BY r.id ORDER BY CAST(r.room_no AS INTEGER) LIMIT 1000"
        rows=conn.execute(sql,args).fetchall()
        conn.close()
        cards="".join(f"<a class='roomcard {'over' if r['occ']>r['capacity'] else ''}' href='/room/{quote(str(r['room_no']))}'>"
                      f"<b>غرفة {esc(r['room_no'])}</b><span>المسجل: {r['occ']} / السعة: {r['capacity']}</span>"
                      f"<small>{esc(r['zone'])}</small></a>" for r in rows)
        body=f"""<div class="panel"><form class="filters"><input name="q" value="{esc(term)}"
        placeholder="رقم الغرفة"><button class="btn primary">بحث</button></form></div>
        <div class="roomgrid">{cards}</div>"""
        self.send_html(layout(body,user))

    def room(self,user,no):
        if not room_allowed(user,no): return self.send_html("غير مصرح",403)
        conn=db()
        room=conn.execute("SELECT * FROM rooms WHERE room_no=?",(no,)).fetchone()
        workers=conn.execute("SELECT * FROM workers WHERE room_no=? AND COALESCE(archived,0)=0 ORDER BY full_name",(no,)).fetchall()
        conn.close()
        rows="".join(f"<tr><td>{esc(w['employee_no'])}</td><td>{esc(w['full_name'])}</td></tr>" for w in workers)
        body=f"""<div class="panel"><h2>الغرفة {esc(no)}</h2>
        <div class="statline"><span>السعة: {room['capacity'] if room else '-'}</span><span>المسجلون: {len(workers)}</span></div>
        <div class="actions"><a class="btn primary" href="/room/{quote(no)}/inspect">بدء جولة الغرفة</a>
        <a class="btn danger" href="/ticket/new?type=room&id={quote(no)}&zone={quote(room['zone'] if room else '')}">بلاغ صيانة</a></div></div>
        <div class="panel tablewrap"><table><tr><th>الرقم الوظيفي</th><th>الاسم</th></tr>{rows}</table></div>"""
        self.send_html(layout(body,user))

    def room_inspect_form(self,user,no):
        if not room_allowed(user,no): return self.send_html("غير مصرح",403)
        conn=db()
        registered=conn.execute("SELECT COUNT(*) c FROM workers WHERE room_no=? AND COALESCE(archived,0)=0",(no,)).fetchone()["c"]
        conn.close()
        body=f"""<div class="panel narrow"><h2>جولة الغرفة {esc(no)}</h2><form method="post">
        <label>العدد المسجل</label><input value="{registered}" readonly><br><br>
        <label>العدد الفعلي</label><input type="number" min="0" name="actual_count" required><br><br>
        <label>النظافة</label><select name="cleanliness"><option>ممتازة</option><option>مقبولة</option><option>سيئة</option></select><br><br>
        <label>الأرقام الوظيفية للأشخاص الزائدين</label><input name="extra_employee_nos" placeholder="12345, 67890"><br><br>
        <label>ملاحظات</label><textarea name="notes"></textarea><br><br>
        <button class="btn primary">حفظ الجولة</button></form></div>"""
        self.send_html(layout(body,user))

    def room_inspect_post(self,user,no):
        if not room_allowed(user,no): return self.send_html("غير مصرح",403)
        f=self.form(); conn=db()
        registered=conn.execute("SELECT COUNT(*) c FROM workers WHERE room_no=? AND COALESCE(archived,0)=0",(no,)).fetchone()["c"]
        conn.execute("""INSERT INTO inspections(inspection_type,location_id,inspector_id,
                      registered_count,actual_count,cleanliness,notes,created_at)
                      VALUES('room',?,?,?,?,?,?,?)""",
                     (no,user["id"],registered,int(f.get("actual_count") or 0),
                      f.get("cleanliness"),f.get("notes",""),now()))
        iid=conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        for emp in re.split(r"[,،\s]+",f.get("extra_employee_nos","").strip()):
            if not emp: continue
            w=conn.execute("SELECT id,room_no FROM workers WHERE employee_no=?",(emp,)).fetchone()
            conn.execute("""INSERT INTO inspection_people(inspection_id,employee_no,worker_id,
                          registered_room,discrepancy_type) VALUES(?,?,?,?,?)""",
                         (iid,emp,w["id"] if w else None,w["room_no"] if w else None,"extra"))
        conn.commit(); conn.close()
        self.redirect(f"/room/{quote(no)}")

    def bathrooms(self,user,q):
        term=(q.get("q") or [""])[0]
        conn=db()
        sql="SELECT * FROM bathrooms WHERE COALESCE(active,1)=1"; args=[]
        if user["role"]=="housing_supervisor":
            a=assignment(user["id"]); group=(a["bathrooms_group"] or "") if a else ""
            if group: sql+=" AND zone_name LIKE ?"; args.append(f"%{group}%")
            else: sql+=" AND 1=0"
        if term:
            sql+=" AND (bathroom_no LIKE ? OR zone_name LIKE ?)"
            args += [f"%{term}%",f"%{term}%"]
        sql+=" ORDER BY CAST(bathroom_no AS INTEGER)"
        rows=conn.execute(sql,args).fetchall(); conn.close()
        cards="".join(f"<div class='roomcard'><b>دورة مياه {esc(r['bathroom_no'])}</b><span>{esc(r['zone_name'])}</span>"
                      f"<div class='actions'><a class='btn small' href='/bathroom/{r['id']}/inspect'>تسجيل جولة</a>"
                      f"<a class='btn danger small' href='/ticket/new?type=bathroom&id={quote(str(r['bathroom_no']))}&zone={quote(r['zone_name'])}'>بلاغ</a></div></div>"
                      for r in rows)
        body=f"""<div class="panel"><form class="filters"><input name="q" value="{esc(term)}"
        placeholder="رقم دورة المياه أو الزون"><button class="btn primary">بحث</button></form></div>
        <div class="roomgrid">{cards or '<div class=panel>لم تتم إضافة دورات مياه بعد.</div>'}</div>"""
        self.send_html(layout(body,user))

    def bath_inspect_form(self,user,bid):
        conn=db(); b=conn.execute("SELECT * FROM bathrooms WHERE id=?",(bid,)).fetchone(); conn.close()
        if not b or not bathroom_allowed(user,b["zone_name"]): return self.send_html("غير مصرح",403)
        body=f"""<div class="panel narrow"><h2>جولة دورة المياه {esc(b['bathroom_no'])}</h2>
        <p>{esc(b['zone_name'])}</p><form method="post"><label>النظافة</label>
        <select name="cleanliness"><option>ممتازة</option><option>مقبولة</option><option>سيئة</option></select><br><br>
        <label>ملاحظات</label><textarea name="notes"></textarea><br><br>
        <button class="btn primary">حفظ الجولة</button></form></div>"""
        self.send_html(layout(body,user))

    def bath_inspect_post(self,user,bid):
        f=self.form(); conn=db()
        b=conn.execute("SELECT * FROM bathrooms WHERE id=?",(bid,)).fetchone()
        if not b or not bathroom_allowed(user,b["zone_name"]):
            conn.close(); return self.send_html("غير مصرح",403)
        conn.execute("""INSERT INTO inspections(inspection_type,location_id,zone_name,inspector_id,
                      cleanliness,notes,created_at) VALUES('bathroom',?,?,?,?,?,?)""",
                     (b["bathroom_no"],b["zone_name"],user["id"],f.get("cleanliness"),f.get("notes",""),now()))
        conn.commit(); conn.close(); self.redirect("/bathrooms")

    def rounds(self,user):
        conn=db()
        if is_admin(user):
            rows=conn.execute("""SELECT i.*,u.display_name FROM inspections i JOIN users u ON u.id=i.inspector_id
                                 ORDER BY i.id DESC LIMIT 500""").fetchall()
        else:
            rows=conn.execute("""SELECT i.*,u.display_name FROM inspections i JOIN users u ON u.id=i.inspector_id
                                 WHERE i.inspector_id=? ORDER BY i.id DESC LIMIT 500""",(user["id"],)).fetchall()
        conn.close()
        tr="".join(f"<tr><td>{esc(r['created_at'])}</td><td>{esc(r['display_name'])}</td>"
                   f"<td>{'غرفة' if r['inspection_type']=='room' else 'دورة مياه'} {esc(r['location_id'])}</td>"
                   f"<td>{esc(r['cleanliness'])}</td><td>{esc(r['actual_count'])}</td><td>{esc(r['notes'])}</td></tr>"
                   for r in rows)
        self.send_html(layout(f"<div class='panel tablewrap'><table><tr><th>التاريخ</th><th>المشرف</th>"
                              f"<th>الموقع</th><th>النظافة</th><th>العدد</th><th>الملاحظات</th></tr>{tr}</table></div>",user))

    def ticket_form(self,user,q):
        typ=(q.get("type") or ["room"])[0]; lid=(q.get("id") or [""])[0]; zone=(q.get("zone") or [""])[0]
        body=f"""<div class="panel narrow"><h2>إنشاء بلاغ صيانة</h2>
        <form method="post" enctype="multipart/form-data"><label>نوع الموقع</label>
        <select name="location_type"><option value="room" {'selected' if typ=='room' else ''}>غرفة</option>
        <option value="bathroom" {'selected' if typ=='bathroom' else ''}>دورة مياه</option></select><br><br>
        <label>رقم الموقع</label><input name="location_id" value="{esc(lid)}" required><br><br>
        <label>الزون</label><input name="zone_name" value="{esc(zone)}"><br><br>
        <label>نوع العطل</label><select name="category"><option>سباكة</option><option>كهرباء</option>
        <option>تكييف</option><option>نجارة</option><option>دهانات</option><option>أخرى</option></select><br><br>
        <label>الأولوية</label><select name="priority"><option value="normal">عادي</option>
        <option value="medium">متوسط</option><option value="urgent">عاجل</option></select><br><br>
        <label>وصف العطل</label><textarea name="description" required></textarea><br><br>
        <label>صورة الملاحظة</label><input type="file" name="photo" accept="image/*" capture="environment" required><br><br>
        <button class="btn danger">إرسال البلاغ</button></form></div>"""
        self.send_html(layout(body,user))

    def ticket_post(self,user):
        f,files=self.multipart()
        typ=f.get("location_type","room"); lid=f.get("location_id",""); zone=f.get("zone_name","")
        if typ=="room" and not room_allowed(user,lid): return self.send_html("غير مصرح",403)
        if typ=="bathroom" and not bathroom_allowed(user,zone): return self.send_html("غير مصرح",403)
        photo=save_image(files.get("photo"),"before")
        conn=db(); ticket_no=request_no("MNT"); verifier=responsible_supervisor(typ,lid,zone)
        conn.execute("""INSERT INTO maintenance_tickets(ticket_no,location_type,location_id,zone_name,
                      category,description,priority,status,reported_by,verification_by,before_photo,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (ticket_no,typ,lid,zone,f.get("category"),f.get("description"),f.get("priority","normal"),
                      "new",user["id"],verifier,photo,now()))
        tid=conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        conn.execute("""INSERT INTO ticket_updates(ticket_id,user_id,action,notes,photo_path,created_at)
                      VALUES(?,?,?,?,?,?)""",(tid,user["id"],"created",f.get("description"),photo,now()))
        conn.commit(); conn.close(); self.redirect(f"/ticket/{tid}")

    def maintenance(self,user,q):
        status=(q.get("status") or [""])[0]
        conn=db()
        sql="""SELECT t.*,u.display_name reporter FROM maintenance_tickets t
               JOIN users u ON u.id=t.reported_by WHERE 1=1"""; args=[]
        if status: sql+=" AND t.status=?"; args.append(status)
        if user["role"]=="housing_supervisor":
            sql+=" AND (t.reported_by=? OR t.verification_by=?)"; args += [user["id"],user["id"]]
        elif user["role"]=="housing_monitor":
            sql+=" AND t.reported_by=?"; args.append(user["id"])
        sql+=" ORDER BY t.id DESC"
        rows=conn.execute(sql,args).fetchall(); conn.close()
        tr="".join(f"<tr><td><a href='/ticket/{r['id']}'>{esc(r['ticket_no'])}</a></td>"
                   f"<td>{'غرفة' if r['location_type']=='room' else 'دورة مياه'} {esc(r['location_id'])}<br>"
                   f"<small>{esc(r['zone_name'])}</small></td><td>{esc(r['category'])}</td>"
                   f"<td>{esc(r['reporter'])}</td><td>{esc(STATUS_AR.get(r['status'],r['status']))}</td>"
                   f"<td>{esc(r['created_at'])}</td></tr>" for r in rows)
        body=f"""<div class="panel actions"><a class="btn danger" href="/ticket/new">بلاغ جديد</a>
        <a class="btn small" href="/maintenance">الكل</a><a class="btn small" href="/maintenance?status=new">جديد</a>
        <a class="btn small" href="/maintenance?status=pending_verification">بانتظار التحقق</a></div>
        <div class="panel tablewrap"><table><tr><th>البلاغ</th><th>الموقع</th><th>النوع</th>
        <th>المبلّغ</th><th>الحالة</th><th>التاريخ</th></tr>{tr}</table></div>"""
        self.send_html(layout(body,user))

    def ticket(self,user,tid):
        conn=db()
        t=conn.execute("""SELECT t.*,u.display_name reporter FROM maintenance_tickets t
                          JOIN users u ON u.id=t.reported_by WHERE t.id=?""",(tid,)).fetchone()
        updates=conn.execute("""SELECT x.*,u.display_name FROM ticket_updates x
                                JOIN users u ON u.id=x.user_id WHERE x.ticket_id=? ORDER BY x.id""",(tid,)).fetchall()
        conn.close()
        if not t: return self.send_html("غير موجود",404)
        if user["role"]=="housing_supervisor" and user["id"] not in (t["reported_by"],t["verification_by"]):
            return self.send_html("غير مصرح",403)
        timeline=""
        for x in updates:
            img=f"<img src='/uploads/{esc(x['photo_path'])}'>" if x["photo_path"] else ""
            timeline+=f"<div class='timelineitem'><b>{esc(x['display_name'])}</b> — {esc(x['action'])}<br>"
            timeline+=f"<small>{esc(x['created_at'])}</small><p>{esc(x['notes'])}</p>{img}</div>"
        before=f"<img class='ticketimg' src='/uploads/{esc(t['before_photo'])}'>" if t["before_photo"] else ""
        after=f"<img class='ticketimg' src='/uploads/{esc(t['after_photo'])}'>" if t["after_photo"] else ""
        actions=""
        if user["role"] in ("maintenance_manager","maintenance_supervisor") and t["status"] in ("new","reopened"):
            actions=f"""<form method="post" action="/ticket/{tid}/update" enctype="multipart/form-data" class="panel">
            <h3>إجراء الصيانة</h3><input type="hidden" name="action" value="complete">
            <input name="technician_name" placeholder="اسم الفني" required><br><br>
            <input name="part_name" placeholder="القطعة المستخدمة" required><br><br>
            <textarea name="notes" placeholder="ما تم تنفيذه" required></textarea><br><br>
            <input type="file" name="photo" accept="image/*" capture="environment" required><br><br>
            <button class="btn primary">إرسال للتحقق</button></form>"""
        if t["status"]=="pending_verification" and (user["id"]==t["verification_by"] or is_admin(user)):
            actions+=f"""<form method="post" action="/ticket/{tid}/update" class="panel">
            <h3>تحقق مشرف السكن ميدانيًا</h3><textarea name="notes" placeholder="ملاحظات التحقق"></textarea>
            <div class="actions"><button name="action" value="verify" class="btn primary">إغلاق البلاغ بعد التحقق</button>
            <button name="action" value="reopen" class="btn danger">إعادته للصيانة</button></div></form>"""
        body=f"""<div class="panel"><h2>{esc(t['ticket_no'])}</h2>
        <p><b>الموقع:</b> {'غرفة' if t['location_type']=='room' else 'دورة مياه'} {esc(t['location_id'])}</p>
        <p><b>الزون:</b> {esc(t['zone_name'])}</p><p><b>المبلّغ:</b> {esc(t['reporter'])}</p>
        <p><b>الحالة:</b> {esc(STATUS_AR.get(t['status'],t['status']))}</p>
        <p><b>الوصف:</b> {esc(t['description'])}</p><div>{before}{after}</div></div>
        {actions}<div class="panel"><h3>الخط الزمني</h3>{timeline}</div>"""
        self.send_html(layout(body,user))

    def ticket_update(self,user,tid):
        ct=self.headers.get("Content-Type","")
        f,files=self.multipart() if ct.startswith("multipart/") else (self.form(),{})
        conn=db(); t=conn.execute("SELECT * FROM maintenance_tickets WHERE id=?",(tid,)).fetchone()
        if not t: conn.close(); return self.send_html("غير موجود",404)
        action=f.get("action")
        if action=="complete":
            if user["role"] not in ("maintenance_manager","maintenance_supervisor"):
                conn.close(); return self.send_html("غير مصرح",403)
            photo=save_image(files.get("photo"),"after")
            conn.execute("""UPDATE maintenance_tickets SET status='pending_verification',
                          assigned_to=?,technician_name=?,part_name=?,completion_notes=?,after_photo=?,
                          started_at=COALESCE(started_at,?),completed_at=? WHERE id=?""",
                         (user["id"],f.get("technician_name"),f.get("part_name"),f.get("notes"),
                          photo,now(),now(),tid))
            conn.execute("""INSERT INTO ticket_updates(ticket_id,user_id,action,notes,photo_path,created_at)
                          VALUES(?,?,?,?,?,?)""",(tid,user["id"],"maintenance_completed",f.get("notes"),photo,now()))
        elif action in ("verify","reopen"):
            if not (user["id"]==t["verification_by"] or is_admin(user)):
                conn.close(); return self.send_html("غير مصرح",403)
            status="closed" if action=="verify" else "reopened"
            conn.execute("""UPDATE maintenance_tickets SET status=?,verified_at=?,
                          closed_at=? WHERE id=?""",(status,now(),now() if status=="closed" else None,tid))
            conn.execute("""INSERT INTO ticket_updates(ticket_id,user_id,action,notes,created_at)
                          VALUES(?,?,?,?,?)""",(tid,user["id"],"verified_closed" if status=="closed" else "reopened",
                                              f.get("notes",""),now()))
        conn.commit(); conn.close(); self.redirect(f"/ticket/{tid}")

    def requests(self,user):
        conn=db()
        rows=conn.execute("""SELECT r.*,u.display_name requester FROM requests r
                             JOIN users u ON u.id=r.requested_by ORDER BY r.id DESC""").fetchall()
        conn.close()
        tr="".join(f"<tr><td>{esc(r['request_no'])}</td><td>{esc(r['request_type'])}</td>"
                   f"<td>{esc(r['requester'])}</td><td>{esc(r['status'])}</td><td>{esc(r['created_at'])}</td></tr>"
                   for r in rows)
        button="<a class='btn primary' href='/requests/new'>طلب تسكين جديد</a>" if user["role"]=="housing_monitor" or is_admin(user) else ""
        self.send_html(layout(f"<div class='panel'>{button}</div><div class='panel tablewrap'><table>"
                              f"<tr><th>الطلب</th><th>النوع</th><th>المقدم</th><th>الحالة</th><th>التاريخ</th></tr>{tr}</table></div>",user))

    def request_form(self,user):
        if user["role"]!="housing_monitor" and not is_admin(user): return self.send_html("غير مصرح",403)
        body="""<div class="panel narrow"><h2>طلب تسكين جديد</h2><form method="post">
        <input name="employee_no" placeholder="الرقم الوظيفي" required><br><br>
        <input name="iqama_no" placeholder="رقم الإقامة"><br><br>
        <input name="full_name" placeholder="الاسم" required><br><br>
        <input name="nationality" placeholder="الجنسية"><br><br>
        <input name="profession" placeholder="المهنة"><br><br>
        <input name="zone" placeholder="الزون" required><br><br>
        <input name="room_no" placeholder="الغرفة" required><br><br>
        <textarea name="reason" placeholder="السبب" required></textarea><br><br>
        <button class="btn primary">إرسال للاعتماد</button></form></div>"""
        self.send_html(layout(body,user))

    def request_post(self,user):
        if user["role"]!="housing_monitor" and not is_admin(user): return self.send_html("غير مصرح",403)
        f=self.form()
        payload={k:f.get(k,"") for k in ("employee_no","iqama_no","full_name","nationality","profession","zone","room_no","reason")}
        conn=db()
        conn.execute("""INSERT INTO requests(request_no,request_type,payload_json,requested_by,status,created_at)
                      VALUES(?,?,?,?,?,?)""",(request_no("ACC"),"new_housing",json.dumps(payload,ensure_ascii=False),
                                             user["id"],"pending",now()))
        conn.commit(); conn.close(); self.redirect("/requests")

    def approvals(self,user):
        if user["role"] not in ("admin","services_manager","housing_manager","housing_supervisor"):
            return self.send_html("غير مصرح",403)
        conn=db()
        rows=conn.execute("""SELECT r.*,u.display_name requester FROM requests r
                             JOIN users u ON u.id=r.requested_by WHERE r.status='pending'""").fetchall()
        arr=[]
        for r in rows:
            p=json.loads(r["payload_json"]); room=p.get("room_no","")
            if room_allowed(user,room):
                arr.append(f"""<tr><td>{esc(r['request_no'])}</td><td>{esc(r['requester'])}</td>
                <td>{esc(room)}</td><td><form method="post" action="/approvals/{r['id']}">
                <input name="reason" placeholder="سبب القرار"><button name="decision" value="approve"
                class="btn primary small">اعتماد</button> <button name="decision" value="reject"
                class="btn danger small">رفض</button></form></td></tr>""")
        conn.close()
        self.send_html(layout(f"<div class='panel tablewrap'><table><tr><th>الطلب</th><th>المقدم</th>"
                              f"<th>الغرفة</th><th>الإجراء</th></tr>{''.join(arr) or '<tr><td colspan=4>لا توجد طلبات</td></tr>'}</table></div>",user))

    def approval_decide(self,user,rid):
        f=self.form(); conn=db()
        r=conn.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone()
        if not r: conn.close(); return self.send_html("غير موجود",404)
        p=json.loads(r["payload_json"]); room=p.get("room_no","")
        if not room_allowed(user,room): conn.close(); return self.send_html("غير مصرح",403)
        status="approved" if f.get("decision")=="approve" else "rejected"
        if status=="approved":
            conn.execute("""INSERT OR REPLACE INTO workers(employee_no,iqama_no,full_name,nationality,
                          profession,zone,room_no,status,archived,created_at,updated_at)
                          VALUES(?,?,?,?,?,?,?,'active',0,?,?)""",
                         (p["employee_no"],p.get("iqama_no",""),p["full_name"],p.get("nationality",""),
                          p.get("profession",""),p["zone"],p["room_no"],now(),now()))
        conn.execute("""UPDATE requests SET status=?,approver_id=?,decision_reason=?,decided_at=?
                      WHERE id=?""",(status,user["id"],f.get("reason",""),now(),rid))
        conn.commit(); conn.close(); self.redirect("/approvals")

    def users(self,user):
        if not is_admin(user): return self.send_html("غير مصرح",403)
        conn=db()
        rows=conn.execute("""SELECT u.*,a.room_start,a.room_end,a.bathrooms_group FROM users u
                             LEFT JOIN assignments a ON a.user_id=u.id ORDER BY u.role,u.display_name""").fetchall()
        conn.close()
        tr="".join(f"<tr><td>{esc(r['employee_no'])}</td><td>{esc(r['display_name'])}</td>"
                   f"<td>{esc(r['username'])}</td><td>{esc(ROLE_AR.get(r['role'],r['role']))}</td>"
                   f"<td>{esc(r['room_start'])} - {esc(r['room_end'])} {esc(r['bathrooms_group'])}</td></tr>" for r in rows)
        form="""<div class="panel"><h3>إضافة مستخدم</h3><form method="post" action="/users/new">
        <div class="filters"><input name="employee_no" placeholder="الرقم الوظيفي" required>
        <input name="display_name" placeholder="الاسم" required><select name="role">
        <option value="housing_supervisor">مشرف سكن</option><option value="housing_monitor">مراقب سكن</option>
        <option value="maintenance_manager">مدير صيانة</option><option value="maintenance_supervisor">مشرف صيانة</option>
        <option value="services_manager">مدير الخدمات المساندة</option><option value="housing_manager">مدير السكن</option>
        </select><input name="room_start" placeholder="من غرفة"><input name="room_end" placeholder="إلى غرفة">
        <input name="bathrooms_group" placeholder="مجموعة دورات المياه"><button class="btn primary">إضافة</button></div>
        <p class="muted">اسم المستخدم هو الرقم الوظيفي، وكلمة المرور 123456.</p></form></div>"""
        self.send_html(layout(form+f"<div class='panel tablewrap'><table><tr><th>الرقم الوظيفي</th>"
                              f"<th>الاسم</th><th>المستخدم</th><th>الدور</th><th>النطاق</th></tr>{tr}</table></div>",user))

    def user_create(self,user):
        if not is_admin(user): return self.send_html("غير مصرح",403)
        f=self.form(); emp=f.get("employee_no","").strip()
        conn=db()
        try:
            conn.execute("""INSERT INTO users(employee_no,username,display_name,password_hash,role,
                          active,must_change_password,created_at) VALUES(?,?,?,?,?,1,0,?)""",
                         (emp,emp,f.get("display_name"),password_hash("123456"),f.get("role"),now()))
            uid=conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            rs=int(f["room_start"]) if f.get("room_start","").isdigit() else None
            re_=int(f["room_end"]) if f.get("room_end","").isdigit() else None
            conn.execute("""INSERT INTO assignments(user_id,room_start,room_end,room_text,bathrooms_group)
                          VALUES(?,?,?,?,?)""",(uid,rs,re_,f"{rs or ''}-{re_ or ''}",f.get("bathrooms_group","")))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
        conn.close(); self.redirect("/users")

    def audit(self,user):
        if not is_admin(user): return self.send_html("غير مصرح",403)
        conn=db(); rows=conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 1000").fetchall(); conn.close()
        tr="".join(f"<tr><td>{esc(r['created_at'])}</td><td>{esc(r['username'])}</td>"
                   f"<td>{esc(r['action'])}</td><td>{esc(r['details_json'])}</td></tr>" for r in rows)
        self.send_html(layout(f"<div class='panel tablewrap'><table><tr><th>التاريخ</th><th>المستخدم</th>"
                              f"<th>العملية</th><th>التفاصيل</th></tr>{tr}</table></div>",user))

if __name__ == "__main__":
    port = int(os.environ.get("PORT","10000"))
    print(f"MAG CAMP running on port {port}")
    ThreadingHTTPServer(("0.0.0.0",port),App).serve_forever()
