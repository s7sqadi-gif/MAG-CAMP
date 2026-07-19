import hashlib, hmac, json, os, sqlite3
from contextlib import closing
from datetime import date, datetime
from functools import wraps
from flask import Flask, abort, redirect, render_template_string, request, session, url_for

ROOT=os.path.dirname(os.path.abspath(__file__)); DATA_DIR=os.path.join(ROOT,'data'); os.makedirs(DATA_DIR,exist_ok=True)
DB=os.environ.get('DATABASE_PATH',os.path.join(DATA_DIR,'mhoms.db'))
app=Flask(__name__); app.secret_key=os.environ.get('SECRET_KEY','local-development-secret-change-me')
app.config.update(SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',SESSION_COOKIE_SECURE=os.environ.get('RENDER','').lower()=='true')
ROLE_AR={'services_manager':'مدير الخدمات المساندة','housing_manager':'مدير السكن','housing_supervisor':'مشرف السكن','housing_monitor':'مراقب السكن','maintenance_manager':'مدير الصيانة','maintenance_supervisor':'مشرف الصيانة'}
REQ_AR={'transfer':'نقل عامل','final_exit':'خروج نهائي','outside_temp':'سكن خارجي مؤقت','outside_perm':'سكن خارجي دائم','add_worker':'إضافة عامل جديد','delete_worker':'حذف/أرشفة عامل'}
STATUS_AR={'pending':'بانتظار الاعتماد','approved':'معتمد','rejected':'مرفوض','new':'جديد','in_progress':'قيد التنفيذ','completed':'مكتمل','verified':'تم التحقق','closed':'مغلق'}
ROOM_USAGE_AR={'residential':'سكن عمال','warehouse':'مستودع','security':'حراسات الأمن الداخلي','contractor':'مقاول','administration':'إدارة','maintenance':'صيانة','laundry':'مغسلة','closed':'مغلق','out_of_service':'خارج الخدمة','other':'أخرى'}

def conn():
 c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA foreign_keys=ON'); return c

def now(): return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
def make_hash(p):
 s=os.urandom(16); d=hashlib.pbkdf2_hmac('sha256',p.encode(),s,150000).hex(); return f'{s.hex()}:{d}'
def verify(stored,p):
 try:
  s,d=stored.split(':',1); v=hashlib.pbkdf2_hmac('sha256',p.encode(),bytes.fromhex(s),150000).hex(); return hmac.compare_digest(v,d)
 except Exception:return False

def column_names(c,table): return {r[1] for r in c.execute(f'PRAGMA table_info({table})')}
def ensure_schema():
 with closing(conn()) as c:
  # additive-only migration: never drops or recreates phase-1 tables
  c.executescript('''
  CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT,request_no TEXT UNIQUE,request_type TEXT,worker_id INTEGER,payload_json TEXT,requested_by INTEGER,approver_id INTEGER,status TEXT DEFAULT 'pending',decision_reason TEXT,created_at TEXT,decided_at TEXT);
  CREATE TABLE IF NOT EXISTS inspections(id INTEGER PRIMARY KEY AUTOINCREMENT,inspection_type TEXT NOT NULL,location_id TEXT NOT NULL,zone_name TEXT,inspector_id INTEGER NOT NULL,registered_count INTEGER,actual_count INTEGER,cleanliness TEXT,notes TEXT,status TEXT DEFAULT 'completed',created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS inspection_people(id INTEGER PRIMARY KEY AUTOINCREMENT,inspection_id INTEGER NOT NULL,employee_no TEXT,worker_id INTEGER,registered_room TEXT,discrepancy_type TEXT);
  CREATE TABLE IF NOT EXISTS bathrooms(id INTEGER PRIMARY KEY AUTOINCREMENT,bathroom_no TEXT NOT NULL,zone_name TEXT,active INTEGER NOT NULL DEFAULT 1);
  CREATE TABLE IF NOT EXISTS maintenance_tickets(id INTEGER PRIMARY KEY AUTOINCREMENT,ticket_no TEXT NOT NULL UNIQUE,location_type TEXT NOT NULL,location_id TEXT NOT NULL,zone_name TEXT,category TEXT,description TEXT,priority TEXT DEFAULT 'normal',status TEXT DEFAULT 'new',reported_by INTEGER NOT NULL,verification_by INTEGER,assigned_to INTEGER,technician_name TEXT,part_name TEXT,completion_notes TEXT,before_photo TEXT,after_photo TEXT,created_at TEXT NOT NULL,started_at TEXT,completed_at TEXT,verified_at TEXT,closed_at TEXT);
  CREATE TABLE IF NOT EXISTS ticket_updates(id INTEGER PRIMARY KEY AUTOINCREMENT,ticket_id INTEGER NOT NULL,user_id INTEGER NOT NULL,action TEXT NOT NULL,notes TEXT,photo_path TEXT,created_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,username TEXT,action TEXT,entity_type TEXT,entity_id INTEGER,details_json TEXT,created_at TEXT);
  CREATE TABLE IF NOT EXISTS room_usage_history(id INTEGER PRIMARY KEY AUTOINCREMENT,room_id INTEGER NOT NULL,old_usage TEXT,new_usage TEXT NOT NULL,reason TEXT,changed_by INTEGER,changed_at TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS import_batches(id INTEGER PRIMARY KEY AUTOINCREMENT,file_name TEXT,rows_total INTEGER,workers_imported INTEGER,special_rooms INTEGER,created_by INTEGER,created_at TEXT);
  CREATE TABLE IF NOT EXISTS bathroom_reports(id INTEGER PRIMARY KEY AUTOINCREMENT,report_no TEXT NOT NULL UNIQUE,bathroom_no TEXT NOT NULL,zone_name TEXT,issue_type TEXT NOT NULL,description TEXT,priority TEXT DEFAULT 'normal',status TEXT DEFAULT 'new',reported_by INTEGER NOT NULL,maintenance_ticket_id INTEGER,created_at TEXT NOT NULL,closed_at TEXT);
  CREATE TABLE IF NOT EXISTS worker_change_requests(id INTEGER PRIMARY KEY AUTOINCREMENT,request_no TEXT NOT NULL UNIQUE,change_type TEXT NOT NULL,worker_id INTEGER,employee_no TEXT,iqama_no TEXT,full_name TEXT,nationality TEXT,profession TEXT,zone TEXT,room_no TEXT,reason TEXT,requested_by INTEGER NOT NULL,status TEXT DEFAULT 'pending',decided_by INTEGER,decision_reason TEXT,created_at TEXT NOT NULL,decided_at TEXT);
  CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
  CREATE INDEX IF NOT EXISTS idx_inspections_location ON inspections(inspection_type,location_id,created_at);
  CREATE INDEX IF NOT EXISTS idx_tickets_status ON maintenance_tickets(status);
  CREATE INDEX IF NOT EXISTS idx_bathroom_reports_reporter ON bathroom_reports(reported_by,status);
  CREATE INDEX IF NOT EXISTS idx_worker_change_status ON worker_change_requests(status,requested_by);
  ''')
  # future-safe additive columns
  for table,defs in {
   'requests':{'supervisor_id':'INTEGER','supervisor_decision_at':'TEXT'},
   'inspections':{'week_key':'TEXT'},
   'rooms':{'usage_type':"TEXT DEFAULT 'residential'",'length_m':'REAL','width_m':'REAL','area_m2':'REAL','status':"TEXT DEFAULT 'active'",'notes':'TEXT','updated_at':'TEXT'},
   'workers':{'source_row':'INTEGER','import_batch_id':'INTEGER'}
  }.items():
   cols=column_names(c,table)
   for name,typ in defs.items():
    if name not in cols:c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {typ}')
  c.commit()
ensure_schema()

def current_user():
 uid=session.get('uid')
 if not uid:return None
 with closing(conn()) as c:return c.execute('SELECT * FROM users WHERE id=? AND active=1',(uid,)).fetchone()
def login_required(fn):
 @wraps(fn)
 def inner(*a,**k):
  u=current_user()
  if not u:return redirect(url_for('login'))
  if u['must_change_password'] and request.endpoint not in {'change_password','logout','static'}:return redirect(url_for('change_password'))
  return fn(*a,**k)
 return inner
def is_admin(u):return u['role'] in ('services_manager','housing_manager')
def can_maintenance(u):return u['role'] in ('services_manager','housing_manager','maintenance_manager','maintenance_supervisor')
def assigned_clause(u,alias='r'):
 if u['role']!='housing_supervisor':return '1=1',[]
 with closing(conn()) as c:rr=c.execute('SELECT room_start,room_end FROM assignments WHERE user_id=?',(u['id'],)).fetchall()
 valid=[sorted((int(x['room_start']),int(x['room_end']))) for x in rr if x['room_start'] is not None and x['room_end'] is not None]
 if not valid:return '1=0',[]
 return '('+' OR '.join([f'CAST({alias}.room_no AS INTEGER) BETWEEN ? AND ?' for _ in valid])+')',[v for pair in valid for v in pair]
def supervisor_for_room(room_no):
 try:n=int(room_no)
 except:return None
 with closing(conn()) as c:return c.execute('''SELECT u.* FROM users u JOIN assignments a ON a.user_id=u.id WHERE u.active=1 AND u.role='housing_supervisor' AND ? BETWEEN MIN(a.room_start,a.room_end) AND MAX(a.room_start,a.room_end) ORDER BY a.id LIMIT 1''',(n,)).fetchone()
def audit(c,u,action,etype,eid,details=None):
 c.execute('INSERT INTO audit_logs(user_id,username,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?,?)',(u['id'] if u else None,u['employee_no'] if u else None,action,etype,eid,json.dumps(details or {},ensure_ascii=False),now()))

BASE='''<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>{{title}}</title><style>
:root{--green:#123b32;--green2:#1e5a4b;--bg:#f4f6f8;--red:#a52222;--amber:#a66a00}
*{box-sizing:border-box}body{font-family:Tahoma,Arial;background:var(--bg);margin:0;color:#24302d}.top{background:var(--green);color:#fff;padding:12px 18px;display:flex;justify-content:space-between;align-items:center;gap:12px;position:sticky;top:0;z-index:20}.brand{display:flex;align-items:center;gap:10px}.brand img{width:46px;height:46px;object-fit:contain;background:#fff;border-radius:9px;padding:4px}.wrap{display:flex;min-height:calc(100vh - 70px)}aside{width:235px;background:#fff;padding:14px;box-shadow:0 0 8px #ccc;flex-shrink:0}aside a{display:block;padding:11px;color:var(--green);text-decoration:none;border-bottom:1px solid #eee;border-radius:7px}aside a:hover{background:#edf4f1}main{flex:1;padding:22px;min-width:0}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px}.card{background:#fff;border-radius:13px;padding:17px;box-shadow:0 2px 8px #d8dddd;margin-bottom:14px}.num{font-size:27px;font-weight:bold;margin-top:5px}.muted{color:#68736f;font-size:13px}.tbl-wrap{overflow:auto;background:#fff;border-radius:12px}.tbl{width:100%;border-collapse:collapse;min-width:720px}.tbl th,.tbl td{padding:10px;border-bottom:1px solid #eee;text-align:right;font-size:14px}.tbl th{background:#edf4f1;position:sticky;top:0}.search,input,select,textarea{padding:11px;border:1px solid #ccd4d1;border-radius:8px;max-width:100%;font:inherit}.search{width:min(420px,100%);margin-bottom:10px}.btn{display:inline-block;background:var(--green);color:#fff;border:0;border-radius:8px;padding:10px 16px;text-decoration:none;cursor:pointer}.btn2{background:#6a7c76}.danger{background:var(--red)}.login{max-width:390px;margin:9vh auto;background:#fff;padding:28px;border-radius:14px;box-shadow:0 3px 18px #bbb}.err{color:#b00020}.ok{color:#0a6b3c}.badge{padding:4px 8px;border-radius:10px;background:#e5eee9;white-space:nowrap}.badge.red{background:#fde7e7;color:#8d1616}.badge.amber{background:#fff1d6;color:#805000}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}.field label{display:block;margin:5px 0}.field input,.field select,.field textarea{width:100%}.mobile-nav{display:none}
@media(max-width:800px){.top{align-items:flex-start}.top .userline{font-size:12px;text-align:left}.wrap{display:block}.desktop-nav{display:none}.mobile-nav{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;background:#fff;padding:8px;position:sticky;top:70px;z-index:15;box-shadow:0 2px 7px #ddd}.mobile-nav a{text-align:center;text-decoration:none;color:var(--green);font-size:12px;padding:9px 3px;border-radius:8px;background:#f1f5f3}main{padding:12px}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.card{padding:14px}.num{font-size:23px}.brand small{font-size:10px}.brand img{width:40px;height:40px}.tbl{min-width:650px}}
</style></head><body><div class="top"><div class="brand"><img src="{{url_for('static',filename='mag_logo.png')}}" alt="MAG"><div><b>MAG CAMP</b><br><small>نظام إدارة سكن ولي العهد — المرحلة الرابعة</small></div></div>{% if u %}<div class="userline">{{u['display_name']}}<br><small>{{roles.get(u['role'],u['role'])}} | <a style="color:white" href="{{url_for('logout')}}">خروج</a></small></div>{% endif %}</div>{% if u %}<nav class="mobile-nav"><a href="{{url_for('dashboard')}}">الرئيسية</a><a href="{{url_for('rooms')}}">الغرف</a><a href="{{url_for('workers')}}">العمال</a><a href="{{url_for('inspections')}}">الجولات</a><a href="{{url_for('tickets')}}">الصيانة</a><a href="{{url_for('requests_list')}}">الطلبات</a></nav><div class="wrap"><aside class="desktop-nav"><a href="{{url_for('dashboard')}}">الرئيسية</a><a href="{{url_for('workers')}}">العمال</a><a href="{{url_for('rooms')}}">الغرف</a><a href="{{url_for('inspections')}}">الجولة الأسبوعية</a><a href="{{url_for('bathroom_reports')}}">بلاغات دورات المياه</a><a href="{{url_for('requests_list')}}">طلبات العمال</a><a href="{{url_for('worker_change_requests')}}">إضافة/حذف عامل</a><a href="{{url_for('tickets')}}">بلاغات الصيانة</a>{% if admin %}<a href="{{url_for('users')}}">المستخدمون</a><a href="{{url_for('audit_logs')}}">سجل العمليات</a>{% endif %}<a href="{{url_for('change_password')}}">تغيير كلمة المرور</a></aside><main>{{body|safe}}</main></div>{% else %}{{body|safe}}{% endif %}</body></html>'''
def page(body,title='MAG CAMP',user=None,**ctx):return render_template_string(BASE,title=title,body=render_template_string(body,**ctx),u=user,roles=ROLE_AR,admin=bool(user and is_admin(user)))

@app.get('/health')
def health():
 try:
  with closing(conn()) as c:
   c.execute('SELECT 1'); counts={t:c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('users','rooms','workers','requests','inspections','maintenance_tickets')}
  return {'status':'ok','database':'ok','phase':4,'counts':counts},200
 except Exception as e:return {'status':'error','database':'unavailable','message':str(e)},503
@app.route('/login',methods=['GET','POST'])
def login():
 if current_user():return redirect(url_for('dashboard'))
 error=''
 if request.method=='POST':
  eno=request.form.get('employee_no','').strip(); pw=request.form.get('password','')
  with closing(conn()) as c:
   u=c.execute('SELECT * FROM users WHERE employee_no=? AND active=1',(eno,)).fetchone()
   if u and verify(u['password_hash'],pw):
    session.clear();session['uid']=u['id'];c.execute("UPDATE users SET last_login=datetime('now') WHERE id=?",(u['id'],));c.commit();return redirect(url_for('change_password') if u['must_change_password'] else url_for('dashboard'))
  error='الرقم الوظيفي أو كلمة المرور غير صحيحة'
 return page('''<div class="login"><h2>تسجيل الدخول</h2>{% if error %}<p class="err">{{error}}</p>{% endif %}<form method="post"><input class="search" name="employee_no" placeholder="الرقم الوظيفي" required><input class="search" type="password" name="password" placeholder="كلمة المرور" required><button class="btn">دخول</button></form></div>''','تسجيل الدخول',error=error)
@app.get('/logout')
def logout():session.clear();return redirect(url_for('login'))
@app.route('/change-password',methods=['GET','POST'])
@login_required
def change_password():
 u=current_user();err='';ok=''
 if request.method=='POST':
  cur=request.form.get('current_password','');new=request.form.get('new_password','');conf=request.form.get('confirm_password','')
  if not verify(u['password_hash'],cur):err='كلمة المرور الحالية غير صحيحة'
  elif len(new)<6:err='كلمة المرور الجديدة يجب ألا تقل عن 6 أحرف أو أرقام'
  elif new!=conf:err='تأكيد كلمة المرور غير مطابق'
  else:
   with closing(conn()) as c:c.execute('UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?',(make_hash(new),u['id']));audit(c,u,'change_password','user',u['id']);c.commit();ok='تم تغيير كلمة المرور بنجاح'
 return page('''<div class="card"><h2>تغيير كلمة المرور</h2>{% if err %}<p class="err">{{err}}</p>{% endif %}{% if ok %}<p class="ok">{{ok}}</p><a class="btn" href="{{url_for('dashboard')}}">الرئيسية</a>{% else %}<form method="post"><input class="search" type="password" name="current_password" placeholder="الحالية" required><input class="search" type="password" name="new_password" placeholder="الجديدة" required><input class="search" type="password" name="confirm_password" placeholder="التأكيد" required><button class="btn">حفظ</button></form>{% endif %}</div>''','تغيير كلمة المرور',u,err=err,ok=ok)

@app.get('/')
@login_required
def dashboard():
 u=current_user();cl,args=assigned_clause(u,'r');week=date.today().strftime('%Y-W%W')
 with closing(conn()) as c:
  rooms=c.execute(f'SELECT COUNT(*) FROM rooms r WHERE {cl}',args).fetchone()[0]
  residential=c.execute(f"SELECT COUNT(*) FROM rooms r WHERE {cl} AND COALESCE(r.usage_type,'residential')='residential'",args).fetchone()[0]
  workers=c.execute(f'''SELECT COUNT(*) FROM workers w WHERE archived=0 AND EXISTS(SELECT 1 FROM rooms r WHERE r.room_no=w.room_no AND {cl})''',args).fetchone()[0]
  capacity=c.execute(f"SELECT COALESCE(SUM(r.capacity),0) FROM rooms r WHERE {cl} AND COALESCE(r.usage_type,'residential')='residential'",args).fetchone()[0]
  overcrowded=c.execute(f'''SELECT COUNT(*) FROM (SELECT r.id FROM rooms r LEFT JOIN workers w ON w.room_no=r.room_no AND w.archived=0 WHERE {cl} AND COALESCE(r.usage_type,'residential')='residential' GROUP BY r.id HAVING COUNT(w.id)>r.capacity)''',args).fetchone()[0]
  vacant_rooms=c.execute(f'''SELECT COUNT(*) FROM (SELECT r.id FROM rooms r LEFT JOIN workers w ON w.room_no=r.room_no AND w.archived=0 WHERE {cl} AND COALESCE(r.usage_type,'residential')='residential' GROUP BY r.id HAVING COUNT(w.id)=0)''',args).fetchone()[0]
  done=c.execute("SELECT COUNT(DISTINCT location_id) FROM inspections WHERE inspection_type='room' AND inspector_id=? AND week_key=?",(u['id'],week)).fetchone()[0] if u['role']=='housing_supervisor' else c.execute("SELECT COUNT(*) FROM inspections WHERE inspection_type='room' AND week_key=?",(week,)).fetchone()[0]
  if u['role']=='housing_supervisor':
   pending=c.execute("SELECT COUNT(*) FROM worker_change_requests WHERE status='pending' AND requested_by=?",(u['id'],)).fetchone()[0]+c.execute("SELECT COUNT(*) FROM requests WHERE status='pending' AND supervisor_id=?",(u['id'],)).fetchone()[0]
   open_t=c.execute("SELECT COUNT(*) FROM maintenance_tickets WHERE status NOT IN ('closed','verified') AND reported_by=?",(u['id'],)).fetchone()[0]
  elif u['role']=='housing_monitor':
   pending=c.execute("SELECT COUNT(*) FROM requests WHERE status='pending' AND requested_by=?",(u['id'],)).fetchone()[0]
   open_t=c.execute("SELECT COUNT(*) FROM maintenance_tickets WHERE status NOT IN ('closed','verified') AND reported_by=?",(u['id'],)).fetchone()[0]
  else:
   pending=c.execute("SELECT COUNT(*) FROM requests WHERE status='pending'").fetchone()[0]+c.execute("SELECT COUNT(*) FROM worker_change_requests WHERE status='pending'").fetchone()[0]
   open_t=c.execute("SELECT COUNT(*) FROM maintenance_tickets WHERE status NOT IN ('closed','verified')").fetchone()[0]
  usages=c.execute(f'''SELECT COALESCE(r.usage_type,'residential') usage,COUNT(*) total FROM rooms r WHERE {cl} GROUP BY COALESCE(r.usage_type,'residential') ORDER BY total DESC''',args).fetchall()
  occupancy=round((workers/capacity*100),1) if capacity else 0
  vacant_beds=max(capacity-workers,0)
 return page('''<h2>لوحة التحكم التنفيذية</h2><p class="muted">ملخص مباشر لحالة السكن حسب صلاحياتك.</p><div class="cards">
 <div class="card"><div>إجمالي العمال</div><div class="num">{{workers}}</div></div>
 <div class="card"><div>الغرف الظاهرة لك</div><div class="num">{{rooms}}</div><div class="muted">السكنية {{residential}}</div></div>
 <div class="card"><div>نسبة الإشغال</div><div class="num">{{occupancy}}%</div><div class="muted">السعة {{capacity}}</div></div>
 <div class="card"><div>الأسرة الشاغرة</div><div class="num">{{vacant_beds}}</div></div>
 <div class="card"><div>غرف متكدسة</div><div class="num">{{overcrowded}}</div></div>
 <div class="card"><div>غرف سكنية فارغة</div><div class="num">{{vacant_rooms}}</div></div>
 <div class="card"><div>جولات هذا الأسبوع</div><div class="num">{{done}}</div></div>
 <div class="card"><div>طلبات معلقة</div><div class="num">{{pending}}</div></div>
 <div class="card"><div>صيانة مفتوحة</div><div class="num">{{open_t}}</div></div></div>
 <div class="card"><h3>استخدامات الغرف</h3><div class="grid">{% for x in usages %}<div><span class="badge">{{usage_ar.get(x.usage,x.usage)}}</span> <b>{{x.total}}</b></div>{% endfor %}</div></div>''','الرئيسية',u,rooms=rooms,residential=residential,workers=workers,capacity=capacity,occupancy=occupancy,vacant_beds=vacant_beds,overcrowded=overcrowded,vacant_rooms=vacant_rooms,done=done,pending=pending,open_t=open_t,usages=usages,usage_ar=ROOM_USAGE_AR)
@app.get('/workers')
@login_required
def workers():
 u=current_user();q=request.args.get('q','').strip();cl,args=assigned_clause(u,'r');sql=f'SELECT w.* FROM workers w JOIN rooms r ON r.room_no=w.room_no WHERE w.archived=0 AND {cl}';p=list(args)
 if q:sql+=' AND (w.employee_no LIKE ? OR w.full_name LIKE ? OR w.room_no LIKE ?)';p += [f'%{q}%']*3
 sql+=' ORDER BY CAST(w.room_no AS INTEGER),w.full_name LIMIT 300'
 with closing(conn()) as c:rows=c.execute(sql,p).fetchall()
 return page('''<h2>العمال</h2><form><input class="search" name="q" value="{{q}}" placeholder="بحث"><button class="btn">بحث</button></form><table class="tbl"><tr><th>الرقم</th><th>الاسم</th><th>الجنسية</th><th>الزون</th><th>الغرفة</th></tr>{% for x in rows %}<tr><td>{{x.employee_no}}</td><td>{{x.full_name}}</td><td>{{x.nationality}}</td><td>{{x.zone}}</td><td>{{x.room_no}}</td></tr>{% endfor %}</table>''','العمال',u,rows=rows,q=q)
@app.get('/rooms')
@login_required
def rooms():
 u=current_user();q=request.args.get('q','').strip();usage=request.args.get('usage','').strip();cl,args=assigned_clause(u,'r');sql=f'''SELECT r.*,COUNT(w.id) occupied FROM rooms r LEFT JOIN workers w ON w.room_no=r.room_no AND w.archived=0 WHERE {cl}''';p=list(args)
 if q:sql+=' AND r.room_no LIKE ?';p.append(f'%{q}%')
 if usage:sql+=" AND COALESCE(r.usage_type,'residential')=?";p.append(usage)
 sql+=' GROUP BY r.id ORDER BY CAST(r.room_no AS INTEGER),r.room_no LIMIT 1000'
 with closing(conn()) as c:rows=c.execute(sql,p).fetchall()
 return page('''<h2>إدارة الغرف</h2><form class="card"><div class="grid"><div class="field"><label>رقم الغرفة</label><input name="q" value="{{q}}" placeholder="بحث"></div><div class="field"><label>الاستخدام</label><select name="usage"><option value="">الكل</option>{% for k,v in usage_ar.items() %}<option value="{{k}}" {% if usage==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select></div></div><button class="btn">تصفية</button></form><div class="tbl-wrap"><table class="tbl"><tr><th>الزون</th><th>الغرفة</th><th>الاستخدام</th><th>المساحة</th><th>السعة</th><th>المشغول</th><th>الشاغر</th><th>الحالة</th><th></th></tr>{% for x in rows %}{% set free=x.capacity-x.occupied %}<tr><td>{{x.zone}}</td><td><b>{{x.room_no}}</b></td><td>{{usage_ar.get(x.usage_type or 'residential',x.usage_type)}}</td><td>{{'%.2f'|format(x.area_m2) if x.area_m2 else '-'}}</td><td>{{x.capacity}}</td><td>{{x.occupied}}</td><td>{{free}}</td><td>{% if x.occupied>x.capacity %}<span class="badge red">متكدسة</span>{% elif x.occupied==0 and (x.usage_type or 'residential')=='residential' %}<span class="badge amber">فارغة</span>{% else %}<span class="badge">طبيعية</span>{% endif %}</td><td><a href="{{url_for('room_detail',room_no=x.room_no)}}">فتح</a></td></tr>{% endfor %}</table></div>''','الغرف',u,rows=rows,q=q,usage=usage,usage_ar=ROOM_USAGE_AR)

@app.route('/rooms/<room_no>',methods=['GET','POST'])
@login_required
def room_detail(room_no):
 u=current_user();cl,args=assigned_clause(u,'r')
 with closing(conn()) as c:
  r=c.execute(f'SELECT r.* FROM rooms r WHERE r.room_no=? AND {cl}',[room_no]+list(args)).fetchone()
  if not r:abort(404)
  if request.method=='POST':
   if not is_admin(u):abort(403)
   old=r['usage_type'] or 'residential';new=request.form.get('usage_type','residential')
   length=float(request.form['length_m']) if request.form.get('length_m') else None;width=float(request.form['width_m']) if request.form.get('width_m') else None
   area=round(length*width,2) if length and width else None
   capacity=int(request.form.get('capacity') or (max(1,int(area//4)) if area else r['capacity']))
   c.execute('UPDATE rooms SET usage_type=?,length_m=?,width_m=?,area_m2=?,capacity=?,status=?,notes=?,updated_at=? WHERE id=?',(new,length,width,area,capacity,request.form.get('status','active'),request.form.get('notes',''),now(),r['id']))
   if old!=new:c.execute('INSERT INTO room_usage_history(room_id,old_usage,new_usage,reason,changed_by,changed_at) VALUES(?,?,?,?,?,?)',(r['id'],old,new,request.form.get('reason',''),u['id'],now()))
   audit(c,u,'update','room',r['id'],{'room_no':room_no,'usage':new,'capacity':capacity});c.commit()
   return redirect(url_for('room_detail',room_no=room_no))
  workers=c.execute('SELECT * FROM workers WHERE room_no=? AND archived=0 ORDER BY full_name',(room_no,)).fetchall()
  history=c.execute('''SELECT h.*,u.display_name FROM room_usage_history h LEFT JOIN users u ON u.id=h.changed_by WHERE h.room_id=? ORDER BY h.id DESC''',(r['id'],)).fetchall()
 return page('''<div class="card"><h2>الغرفة {{r.room_no}}</h2><div class="cards"><div><b>الزون</b><div class="num">{{r.zone}}</div></div><div><b>السعة</b><div class="num">{{r.capacity}}</div></div><div><b>المقيمون</b><div class="num">{{workers|length}}</div></div><div><b>الشاغر</b><div class="num">{{r.capacity-(workers|length)}}</div></div></div></div>
 {% if admin %}<div class="card"><h3>بيانات واستخدام الغرفة</h3><form method="post"><div class="grid"><div class="field"><label>الاستخدام</label><select name="usage_type">{% for k,v in usage_ar.items() %}<option value="{{k}}" {% if (r.usage_type or 'residential')==k %}selected{% endif %}>{{v}}</option>{% endfor %}</select></div><div class="field"><label>الطول بالمتر</label><input type="number" step="0.01" name="length_m" value="{{r.length_m or ''}}"></div><div class="field"><label>العرض بالمتر</label><input type="number" step="0.01" name="width_m" value="{{r.width_m or ''}}"></div><div class="field"><label>السعة المعتمدة</label><input type="number" min="0" name="capacity" value="{{r.capacity}}"></div><div class="field"><label>الحالة</label><select name="status"><option value="active">نشطة</option><option value="closed">مغلقة</option><option value="out_of_service">خارج الخدمة</option></select></div><div class="field"><label>سبب تغيير الاستخدام</label><input name="reason"></div></div><div class="field"><label>ملاحظات</label><textarea name="notes">{{r.notes or ''}}</textarea></div><button class="btn">حفظ التعديلات</button></form></div>{% endif %}
 <div class="card"><h3>المقيمون</h3><div class="tbl-wrap"><table class="tbl"><tr><th>الرقم</th><th>الاسم</th><th>الجنسية</th><th>المهنة</th></tr>{% for w in workers %}<tr><td>{{w.employee_no}}</td><td>{{w.full_name}}</td><td>{{w.nationality}}</td><td>{{w.profession}}</td></tr>{% endfor %}</table></div></div>
 <div class="card"><a class="btn" href="{{url_for('new_inspection',room_no=r.room_no)}}">تنفيذ الجولة الأسبوعية</a></div>
 {% if history %}<div class="card"><h3>سجل تغيير الاستخدام</h3><div class="tbl-wrap"><table class="tbl"><tr><th>التاريخ</th><th>السابق</th><th>الجديد</th><th>السبب</th><th>بواسطة</th></tr>{% for h in history %}<tr><td>{{h.changed_at}}</td><td>{{usage_ar.get(h.old_usage,h.old_usage)}}</td><td>{{usage_ar.get(h.new_usage,h.new_usage)}}</td><td>{{h.reason}}</td><td>{{h.display_name}}</td></tr>{% endfor %}</table></div></div>{% endif %}''','الغرفة '+room_no,u,r=r,workers=workers,history=history,usage_ar=ROOM_USAGE_AR,admin=is_admin(u))
@app.get('/inspections')
@login_required
def inspections():
 u=current_user();where='';p=[]
 if u['role']=='housing_supervisor':where='WHERE i.inspector_id=?';p=[u['id']]
 with closing(conn()) as c:rows=c.execute(f'''SELECT i.*,u.display_name FROM inspections i JOIN users u ON u.id=i.inspector_id {where} AND i.inspection_type='room' ORDER BY i.id DESC LIMIT 300''' if where else '''SELECT i.*,u.display_name FROM inspections i JOIN users u ON u.id=i.inspector_id WHERE i.inspection_type='room' ORDER BY i.id DESC LIMIT 300''',p).fetchall()
 return page('''<h2>الجولة الأسبوعية للغرف</h2><a class="btn" href="{{url_for('new_inspection')}}">جولة غرفة</a><table class="tbl"><tr><th>التاريخ</th><th>الغرفة</th><th>المسجل</th><th>الفعلي</th><th>النظافة</th><th>المشرف</th><th>ملاحظات</th></tr>{% for x in rows %}<tr><td>{{x.created_at}}</td><td>{{x.location_id}}</td><td>{{x.registered_count}}</td><td>{{x.actual_count}}</td><td>{{x.cleanliness}}</td><td>{{x.display_name}}</td><td>{{x.notes}}</td></tr>{% endfor %}</table>''','الجرد الأسبوعي',u,rows=rows)
@app.route('/inspections/new',methods=['GET','POST'])
@login_required
def new_inspection():
 u=current_user();room=request.args.get('room_no','');err=''
 if request.method=='POST':
  room=request.form.get('room_no','').strip();clean=request.form.get('cleanliness');actual=request.form.get('actual_count');notes=request.form.get('notes','');sup=supervisor_for_room(room)
  if u['role']=='housing_supervisor' and (not sup or sup['id']!=u['id']):err='هذه الغرفة ليست ضمن نطاقك'
  else:
   with closing(conn()) as c:
    r=c.execute('SELECT * FROM rooms WHERE room_no=?',(room,)).fetchone()
    if not r:err='رقم الغرفة غير موجود'
    else:
     reg=c.execute('SELECT COUNT(*) FROM workers WHERE room_no=? AND archived=0',(room,)).fetchone()[0];wk=date.today().strftime('%Y-W%W')
     cur=c.execute('INSERT INTO inspections(inspection_type,location_id,zone_name,inspector_id,registered_count,actual_count,cleanliness,notes,status,created_at,week_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)',('room',room,str(r['zone']),u['id'],reg,int(actual or 0),clean,notes,'completed',now(),wk));audit(c,u,'create','inspection',cur.lastrowid,{'room_no':room});c.commit();return redirect(url_for('inspections'))
 return page('''<div class="card"><h2>جولة غرفة</h2>{% if err %}<p class="err">{{err}}</p>{% endif %}<form method="post"><div class="grid"><div class="field"><label>رقم الغرفة</label><input name="room_no" value="{{room}}" required></div><div class="field"><label>العدد الفعلي</label><input type="number" min="0" name="actual_count" required></div><div class="field"><label>النظافة</label><select name="cleanliness"><option>ممتاز</option><option>جيد</option><option>يحتاج متابعة</option></select></div></div><div class="field"><label>الملاحظات</label><textarea name="notes"></textarea></div><button class="btn">حفظ الجولة</button></form></div>''','جولة غرفة',u,room=room,err=err)

@app.get('/bathroom-inspections')
@login_required
def bathroom_inspections():
 return redirect(url_for('bathroom_reports'))

@app.get('/bathroom-reports')
@login_required
def bathroom_reports():
 u=current_user();where='';params=[]
 if u['role'] in ('housing_supervisor','housing_monitor'):where='WHERE b.reported_by=?';params=[u['id']]
 with closing(conn()) as c:
  rows=c.execute(f'''SELECT b.*,u.display_name,t.ticket_no,t.status ticket_status FROM bathroom_reports b LEFT JOIN users u ON u.id=b.reported_by LEFT JOIN maintenance_tickets t ON t.id=b.maintenance_ticket_id {where} ORDER BY b.id DESC LIMIT 300''',params).fetchall()
 return page('''<h2>بلاغات دورات المياه</h2><p class="muted">تم استبدال جرد الدورات بنظام بلاغات؛ كل بلاغ ينشئ تذكرة صيانة مرتبطة به.</p><a class="btn" href="{{url_for('new_bathroom_report')}}">إضافة بلاغ دورة مياه</a><div class="tbl-wrap"><table class="tbl"><tr><th>رقم البلاغ</th><th>الدورة</th><th>الزون</th><th>نوع الملاحظة</th><th>الأولوية</th><th>الحالة</th><th>المبلّغ</th><th>التاريخ</th><th></th></tr>{% for x in rows %}<tr><td>{{x.report_no}}</td><td>{{x.bathroom_no}}</td><td>{{x.zone_name or '-'}}</td><td>{{x.issue_type}}</td><td>{{x.priority}}</td><td>{{status_ar.get(x.ticket_status or x.status,x.ticket_status or x.status)}}</td><td>{{x.display_name}}</td><td>{{x.created_at}}</td><td>{% if x.maintenance_ticket_id %}<a href="{{url_for('ticket_detail',tid=x.maintenance_ticket_id)}}">فتح البلاغ</a>{% endif %}</td></tr>{% endfor %}</table></div>''','بلاغات دورات المياه',u,rows=rows,status_ar=STATUS_AR)

@app.route('/bathroom-inspections/new',methods=['GET','POST'])
@login_required
def new_bathroom_inspection():
 return redirect(url_for('new_bathroom_report'))

@app.route('/bathroom-reports/new',methods=['GET','POST'])
@login_required
def new_bathroom_report():
 u=current_user();err=''
 if request.method=='POST':
  no=request.form.get('bathroom_no','').strip();zone=request.form.get('zone_name','').strip();issue=request.form.get('issue_type','').strip();desc=request.form.get('description','').strip();priority=request.form.get('priority','normal')
  if not no or not issue:err='رقم دورة المياه ونوع الملاحظة مطلوبان'
  else:
   report_no='BTH-'+datetime.utcnow().strftime('%Y%m%d%H%M%S%f')[-16:]
   ticket_no='MNT-'+datetime.utcnow().strftime('%Y%m%d%H%M%S%f')[-16:]
   with closing(conn()) as c:
    t=c.execute('INSERT INTO maintenance_tickets(ticket_no,location_type,location_id,zone_name,category,description,priority,status,reported_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(ticket_no,'bathroom',no,zone,issue,desc,priority,'new',u['id'],now()))
    c.execute('INSERT INTO ticket_updates(ticket_id,user_id,action,notes,created_at) VALUES(?,?,?,?,?)',(t.lastrowid,u['id'],'created',desc,now()))
    b=c.execute('INSERT INTO bathroom_reports(report_no,bathroom_no,zone_name,issue_type,description,priority,status,reported_by,maintenance_ticket_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(report_no,no,zone,issue,desc,priority,'new',u['id'],t.lastrowid,now()))
    audit(c,u,'create','bathroom_report',b.lastrowid,{'bathroom_no':no,'ticket_no':ticket_no});c.commit()
   return redirect(url_for('bathroom_reports'))
 return page('''<div class="card"><h2>بلاغ دورة مياه جديد</h2>{% if err %}<p class="err">{{err}}</p>{% endif %}<form method="post"><div class="grid"><div class="field"><label>رقم دورة المياه</label><input name="bathroom_no" required></div><div class="field"><label>الزون</label><select name="zone_name"><option value="">غير محدد</option><option value="1">زون 1</option><option value="2">زون 2</option><option value="3">زون 3</option><option value="4">زون 4</option></select></div><div class="field"><label>نوع الملاحظة</label><select name="issue_type"><option>محبس أرضي</option><option>محبس ترويش</option><option>باب الحمام</option><option>مروش</option><option>محابس الغسيل</option><option>المغاسل</option><option>تسريب</option><option>انسداد</option><option>كهرباء</option><option>نظافة</option><option>أخرى</option></select></div><div class="field"><label>الأولوية</label><select name="priority"><option value="normal">عادي</option><option value="urgent">عاجل</option><option value="critical">طارئ</option></select></div></div><div class="field"><label>وصف البلاغ</label><textarea name="description" required></textarea></div><button class="btn">إرسال البلاغ</button></form></div>''','بلاغ دورة مياه',u,err=err)

@app.get('/requests')
@login_required
def requests_list():
 u=current_user();where='';p=[]
 if u['role']=='housing_monitor':where='WHERE q.requested_by=?';p=[u['id']]
 elif u['role']=='housing_supervisor':where='WHERE q.supervisor_id=?';p=[u['id']]
 with closing(conn()) as c:rows=c.execute(f'''SELECT q.*,w.employee_no,w.full_name,ru.display_name requester,au.display_name approver FROM requests q LEFT JOIN workers w ON w.id=q.worker_id LEFT JOIN users ru ON ru.id=q.requested_by LEFT JOIN users au ON au.id=q.approver_id {where} ORDER BY q.id DESC LIMIT 300''',p).fetchall()
 return page('''<h2>طلبات العمال</h2>{% if can_create %}<a class="btn" href="{{url_for('new_request')}}">إنشاء طلب</a>{% endif %}<table class="tbl"><tr><th>الرقم</th><th>النوع</th><th>العامل</th><th>مقدم الطلب</th><th>المشرف</th><th>الحالة</th><th>التاريخ</th><th></th></tr>{% for x in rows %}<tr><td>{{x.request_no}}</td><td>{{req_ar.get(x.request_type,x.request_type)}}</td><td>{{x.employee_no}} - {{x.full_name}}</td><td>{{x.requester}}</td><td>{{x.approver or '-'}}</td><td><span class="badge">{{status_ar.get(x.status,x.status)}}</span></td><td>{{x.created_at}}</td><td><a href="{{url_for('request_detail',rid=x.id)}}">فتح</a></td></tr>{% endfor %}</table>''','طلبات العمال',u,rows=rows,req_ar=REQ_AR,status_ar=STATUS_AR,can_create=u['role'] in ('housing_monitor','housing_manager','services_manager'))
@app.route('/requests/new',methods=['GET','POST'])
@login_required
def new_request():
 u=current_user()
 if u['role'] not in ('housing_monitor','housing_manager','services_manager'):abort(403)
 err='';worker=None
 if request.method=='POST':
  eno=request.form['employee_no'].strip();typ=request.form['request_type'];payload={k:request.form.get(k,'') for k in ('new_room','start_date','end_date','outside_location','reason')}
  with closing(conn()) as c:
   worker=c.execute('SELECT * FROM workers WHERE employee_no=? AND archived=0',(eno,)).fetchone()
   if not worker:err='العامل غير موجود أو مؤرشف'
   else:
    sup=supervisor_for_room(worker['room_no']);reqno='REQ-'+datetime.utcnow().strftime('%Y%m%d%H%M%S%f')[-16:]
    cur=c.execute('INSERT INTO requests(request_no,request_type,worker_id,payload_json,requested_by,approver_id,supervisor_id,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(reqno,typ,worker['id'],json.dumps(payload,ensure_ascii=False),u['id'],sup['id'] if sup else None,sup['id'] if sup else None,'pending',now()));audit(c,u,'create','request',cur.lastrowid,{'type':typ,'worker':eno});c.commit();return redirect(url_for('request_detail',rid=cur.lastrowid))
 return page('''<div class="card"><h2>إنشاء طلب عامل</h2>{% if err %}<p class="err">{{err}}</p>{% endif %}<form method="post"><div class="grid"><div class="field"><label>الرقم الوظيفي للعامل</label><input name="employee_no" required></div><div class="field"><label>نوع الطلب</label><select name="request_type"><option value="transfer">نقل عامل</option><option value="final_exit">خروج نهائي</option><option value="outside_temp">سكن خارجي مؤقت</option><option value="outside_perm">سكن خارجي دائم</option></select></div><div class="field"><label>الغرفة الجديدة (للنقل)</label><input name="new_room"></div><div class="field"><label>تاريخ البداية</label><input type="date" name="start_date"></div><div class="field"><label>تاريخ النهاية</label><input type="date" name="end_date"></div><div class="field"><label>موقع السكن الخارجي</label><input name="outside_location"></div></div><div class="field"><label>السبب</label><textarea name="reason"></textarea></div><button class="btn">إرسال الطلب</button></form></div>''','إنشاء طلب',u,err=err)
@app.route('/requests/<int:rid>',methods=['GET','POST'])
@login_required
def request_detail(rid):
 u=current_user()
 with closing(conn()) as c:
  q=c.execute('''SELECT q.*,w.employee_no,w.full_name,w.room_no,u.display_name approver FROM requests q LEFT JOIN workers w ON w.id=q.worker_id LEFT JOIN users u ON u.id=q.supervisor_id WHERE q.id=?''',(rid,)).fetchone()
  if not q:abort(404)
  if u['role']=='housing_monitor' and q['requested_by']!=u['id']:abort(403)
  if u['role']=='housing_supervisor' and q['supervisor_id']!=u['id']:abort(403)
  if request.method=='POST':
   if not (is_admin(u) or (u['role']=='housing_supervisor' and q['supervisor_id']==u['id'])):abort(403)
   decision=request.form['decision'];reason=request.form.get('decision_reason','');payload=json.loads(q['payload_json'] or '{}')
   if q['status']!='pending':return redirect(url_for('request_detail',rid=rid))
   if decision=='approved':
    if q['request_type']=='transfer':
     new_room=payload.get('new_room','').strip();room=c.execute('SELECT * FROM rooms WHERE room_no=?',(new_room,)).fetchone()
     if not room:return page('<div class="card"><p class="err">الغرفة الجديدة غير موجودة.</p></div>','خطأ',u),400
     occupied=c.execute('SELECT COUNT(*) FROM workers WHERE room_no=? AND archived=0',(new_room,)).fetchone()[0]
     if occupied>=room['capacity']:return page('<div class="card"><p class="err">الغرفة الجديدة ممتلئة.</p></div>','خطأ',u),400
     c.execute("UPDATE workers SET room_no=?,zone=?,status='active',updated_at=? WHERE id=?",(new_room,room['zone'],now(),q['worker_id']))
    elif q['request_type']=='final_exit':c.execute("UPDATE workers SET archived=1,status='final_exit',updated_at=? WHERE id=?",(now(),q['worker_id']))
    elif q['request_type']=='outside_temp':c.execute("UPDATE workers SET status='outside_temp',updated_at=? WHERE id=?",(now(),q['worker_id']))
    elif q['request_type']=='outside_perm':c.execute("UPDATE workers SET status='outside_perm',updated_at=? WHERE id=?",(now(),q['worker_id']))
   c.execute('UPDATE requests SET status=?,approver_id=?,decision_reason=?,decided_at=?,supervisor_decision_at=? WHERE id=?',(decision,u['id'],reason,now(),now(),rid));audit(c,u,decision,'request',rid,{'reason':reason});c.commit();return redirect(url_for('request_detail',rid=rid))
  payload=json.loads(q['payload_json'] or '{}')
 return page('''<div class="card"><h2>طلب {{q.request_no}}</h2><p><b>النوع:</b> {{req_ar.get(q.request_type)}}</p><p><b>العامل:</b> {{q.employee_no}} - {{q.full_name}}</p><p><b>الغرفة الحالية:</b> {{q.room_no}}</p><p><b>المشرف المسؤول:</b> {{q.approver or '-'}}</p><p><b>الحالة:</b> {{status_ar.get(q.status,q.status)}}</p><p><b>البيانات:</b> {{payload}}</p>{% if can_decide and q.status=='pending' %}<form method="post"><textarea name="decision_reason" placeholder="ملاحظة القرار"></textarea><br><button class="btn" name="decision" value="approved">اعتماد وتنفيذ</button> <button class="btn danger" name="decision" value="rejected">رفض</button></form>{% endif %}</div>''','تفاصيل الطلب',u,q=q,payload=payload,req_ar=REQ_AR,status_ar=STATUS_AR,can_decide=is_admin(u) or (u['role']=='housing_supervisor' and q['supervisor_id']==u['id']))

@app.get('/worker-change-requests')
@login_required
def worker_change_requests():
 u=current_user();where='';params=[]
 if u['role']=='housing_supervisor':where='WHERE q.requested_by=?';params=[u['id']]
 elif u['role'] not in ('services_manager','housing_manager'):abort(403)
 with closing(conn()) as c:
  rows=c.execute(f'''SELECT q.*,ru.display_name requester,du.display_name decider FROM worker_change_requests q LEFT JOIN users ru ON ru.id=q.requested_by LEFT JOIN users du ON du.id=q.decided_by {where} ORDER BY q.id DESC LIMIT 300''',params).fetchall()
 return page('''<h2>طلبات إضافة وحذف العمال</h2>{% if u.role=='housing_supervisor' %}<a class="btn" href="{{url_for('new_worker_change_request')}}">طلب جديد</a>{% endif %}<div class="tbl-wrap"><table class="tbl"><tr><th>الطلب</th><th>النوع</th><th>العامل</th><th>الغرفة</th><th>مقدم الطلب</th><th>الحالة</th><th>التاريخ</th><th></th></tr>{% for x in rows %}<tr><td>{{x.request_no}}</td><td>{{'إضافة عامل' if x.change_type=='add' else 'حذف/أرشفة عامل'}}</td><td>{{x.employee_no or '-'}} - {{x.full_name or '-'}}</td><td>{{x.room_no or '-'}}</td><td>{{x.requester}}</td><td>{{status_ar.get(x.status,x.status)}}</td><td>{{x.created_at}}</td><td><a href="{{url_for('worker_change_request_detail',qid=x.id)}}">فتح</a></td></tr>{% endfor %}</table></div>''','إضافة/حذف عامل',u,rows=rows,status_ar=STATUS_AR,u=u)

@app.route('/worker-change-requests/new',methods=['GET','POST'])
@login_required
def new_worker_change_request():
 u=current_user()
 if u['role']!='housing_supervisor':abort(403)
 err='';typ=request.form.get('change_type','add')
 if request.method=='POST':
  reason=request.form.get('reason','').strip();room_no=request.form.get('room_no','').strip();eno=request.form.get('employee_no','').strip()
  with closing(conn()) as c:
   if typ=='add':
    room=c.execute('SELECT * FROM rooms WHERE room_no=?',(room_no,)).fetchone();sup=supervisor_for_room(room_no)
    if not room:err='الغرفة غير موجودة'
    elif not sup or sup['id']!=u['id']:err='الغرفة ليست ضمن نطاقك'
    elif c.execute('SELECT 1 FROM workers WHERE employee_no=? AND archived=0',(eno,)).fetchone():err='الرقم الوظيفي موجود مسبقًا'
    else:
     req='WCR-'+datetime.utcnow().strftime('%Y%m%d%H%M%S%f')[-16:]
     cur=c.execute('''INSERT INTO worker_change_requests(request_no,change_type,employee_no,iqama_no,full_name,nationality,profession,zone,room_no,reason,requested_by,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(req,'add',eno,request.form.get('iqama_no','').strip(),request.form.get('full_name','').strip(),request.form.get('nationality','').strip(),request.form.get('profession','').strip(),str(room['zone']),room_no,reason,u['id'],'pending',now()))
     audit(c,u,'create','worker_change_request',cur.lastrowid,{'type':'add','employee_no':eno});c.commit();return redirect(url_for('worker_change_request_detail',qid=cur.lastrowid))
   else:
    worker=c.execute('SELECT * FROM workers WHERE employee_no=? AND archived=0',(eno,)).fetchone()
    if not worker:err='العامل غير موجود'
    else:
     sup=supervisor_for_room(worker['room_no'])
     if not sup or sup['id']!=u['id']:err='العامل ليس ضمن الغرف التابعة لك'
     else:
      req='WCR-'+datetime.utcnow().strftime('%Y%m%d%H%M%S%f')[-16:]
      cur=c.execute('''INSERT INTO worker_change_requests(request_no,change_type,worker_id,employee_no,iqama_no,full_name,nationality,profession,zone,room_no,reason,requested_by,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(req,'delete',worker['id'],worker['employee_no'],worker['iqama_no'],worker['full_name'],worker['nationality'],worker['profession'],worker['zone'],worker['room_no'],reason,u['id'],'pending',now()))
      audit(c,u,'create','worker_change_request',cur.lastrowid,{'type':'delete','worker_id':worker['id']});c.commit();return redirect(url_for('worker_change_request_detail',qid=cur.lastrowid))
 return page('''<div class="card"><h2>طلب إضافة أو حذف عامل</h2>{% if err %}<p class="err">{{err}}</p>{% endif %}<form method="post"><div class="grid"><div class="field"><label>نوع الطلب</label><select name="change_type"><option value="add" {% if typ=='add' %}selected{% endif %}>إضافة عامل جديد</option><option value="delete" {% if typ=='delete' %}selected{% endif %}>حذف/أرشفة عامل</option></select></div><div class="field"><label>الرقم الوظيفي</label><input name="employee_no" required></div><div class="field"><label>رقم الإقامة (للإضافة)</label><input name="iqama_no"></div><div class="field"><label>اسم العامل (للإضافة)</label><input name="full_name"></div><div class="field"><label>الجنسية (للإضافة)</label><input name="nationality"></div><div class="field"><label>المهنة (للإضافة)</label><input name="profession"></div><div class="field"><label>رقم الغرفة (للإضافة)</label><input name="room_no"></div></div><div class="field"><label>سبب الطلب</label><textarea name="reason" required></textarea></div><button class="btn">إرسال لمدير السكن</button></form></div>''','طلب إضافة/حذف عامل',u,err=err,typ=typ)

@app.route('/worker-change-requests/<int:qid>',methods=['GET','POST'])
@login_required
def worker_change_request_detail(qid):
 u=current_user()
 with closing(conn()) as c:
  q=c.execute('''SELECT q.*,ru.display_name requester,du.display_name decider FROM worker_change_requests q LEFT JOIN users ru ON ru.id=q.requested_by LEFT JOIN users du ON du.id=q.decided_by WHERE q.id=?''',(qid,)).fetchone()
  if not q:abort(404)
  if u['role']=='housing_supervisor' and q['requested_by']!=u['id']:abort(403)
  if u['role'] not in ('housing_supervisor','services_manager','housing_manager'):abort(403)
  if request.method=='POST':
   if u['role'] not in ('housing_manager','services_manager'):abort(403)
   if q['status']!='pending':return redirect(url_for('worker_change_request_detail',qid=qid))
   decision=request.form.get('decision');reason=request.form.get('decision_reason','').strip()
   if decision=='approved':
    if q['change_type']=='add':
     if c.execute('SELECT 1 FROM workers WHERE employee_no=? AND archived=0',(q['employee_no'],)).fetchone():return page('<div class="card"><p class="err">العامل موجود مسبقًا.</p></div>','خطأ',u),400
     room=c.execute('SELECT * FROM rooms WHERE room_no=?',(q['room_no'],)).fetchone()
     if not room:return page('<div class="card"><p class="err">الغرفة غير موجودة.</p></div>','خطأ',u),400
     occupied=c.execute('SELECT COUNT(*) FROM workers WHERE room_no=? AND archived=0',(q['room_no'],)).fetchone()[0]
     if occupied>=room['capacity']:return page('<div class="card"><p class="err">الغرفة ممتلئة.</p></div>','خطأ',u),400
     cur=c.execute('''INSERT INTO workers(employee_no,iqama_no,full_name,nationality,profession,zone,room_no,status,archived,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(q['employee_no'],q['iqama_no'],q['full_name'],q['nationality'],q['profession'],q['zone'],q['room_no'],'active',0,now(),now()))
     c.execute('UPDATE worker_change_requests SET worker_id=? WHERE id=?',(cur.lastrowid,qid))
    else:
     c.execute("UPDATE workers SET archived=1,status='archived',updated_at=? WHERE id=?",(now(),q['worker_id']))
   c.execute('UPDATE worker_change_requests SET status=?,decided_by=?,decision_reason=?,decided_at=? WHERE id=?',(decision,u['id'],reason,now(),qid));audit(c,u,decision,'worker_change_request',qid,{'reason':reason});c.commit();return redirect(url_for('worker_change_request_detail',qid=qid))
 return page('''<div class="card"><h2>طلب {{q.request_no}}</h2><p><b>النوع:</b> {{'إضافة عامل جديد' if q.change_type=='add' else 'حذف/أرشفة عامل'}}</p><p><b>العامل:</b> {{q.employee_no}} - {{q.full_name}}</p><p><b>الإقامة:</b> {{q.iqama_no or '-'}}</p><p><b>الغرفة:</b> {{q.room_no}}</p><p><b>مقدم الطلب:</b> {{q.requester}}</p><p><b>السبب:</b> {{q.reason}}</p><p><b>الحالة:</b> {{status_ar.get(q.status,q.status)}}</p>{% if can_decide and q.status=='pending' %}<form method="post"><textarea name="decision_reason" placeholder="ملاحظة القرار"></textarea><br><button class="btn" name="decision" value="approved">اعتماد وتنفيذ</button> <button class="btn danger" name="decision" value="rejected">رفض</button></form>{% endif %}</div>''','تفاصيل الطلب',u,q=q,status_ar=STATUS_AR,can_decide=u['role'] in ('housing_manager','services_manager'))

@app.get('/tickets')
@login_required
def tickets():
 u=current_user();where='';params=[]
 if u['role'] in ('housing_supervisor','housing_monitor'):where='WHERE t.reported_by=?';params=[u['id']]
 with closing(conn()) as c:rows=c.execute(f'''SELECT t.*,u.display_name reporter FROM maintenance_tickets t LEFT JOIN users u ON u.id=t.reported_by {where} ORDER BY t.id DESC LIMIT 300''',params).fetchall()
 return page('''<h2>بلاغات الصيانة</h2><a class="btn" href="{{url_for('new_ticket')}}">بلاغ جديد</a><table class="tbl"><tr><th>البلاغ</th><th>الموقع</th><th>التصنيف</th><th>الأولوية</th><th>الحالة</th><th>المبلغ</th><th></th></tr>{% for x in rows %}<tr><td>{{x.ticket_no}}</td><td>{{x.location_type}} {{x.location_id}}</td><td>{{x.category}}</td><td>{{x.priority}}</td><td>{{status_ar.get(x.status,x.status)}}</td><td>{{x.reporter}}</td><td><a href="{{url_for('ticket_detail',tid=x.id)}}">فتح</a></td></tr>{% endfor %}</table>''','بلاغات الصيانة',u,rows=rows,status_ar=STATUS_AR)
@app.route('/tickets/new',methods=['GET','POST'])
@login_required
def new_ticket():
 u=current_user()
 if request.method=='POST':
  no='MNT-'+datetime.utcnow().strftime('%Y%m%d%H%M%S%f')[-16:]
  with closing(conn()) as c:
   cur=c.execute('INSERT INTO maintenance_tickets(ticket_no,location_type,location_id,zone_name,category,description,priority,status,reported_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(no,request.form['location_type'],request.form['location_id'],request.form.get('zone_name',''),request.form['category'],request.form.get('description',''),request.form['priority'],'new',u['id'],now()));c.execute('INSERT INTO ticket_updates(ticket_id,user_id,action,notes,created_at) VALUES(?,?,?,?,?)',(cur.lastrowid,u['id'],'created',request.form.get('description',''),now()));audit(c,u,'create','maintenance_ticket',cur.lastrowid);c.commit();return redirect(url_for('ticket_detail',tid=cur.lastrowid))
 return page('''<div class="card"><h2>بلاغ صيانة جديد</h2><form method="post"><div class="grid"><div class="field"><label>نوع الموقع</label><select name="location_type"><option value="room">غرفة</option><option value="bathroom">دورة مياه</option><option value="corridor">ممر</option><option value="kitchen">مطبخ</option><option value="other">أخرى</option></select></div><div class="field"><label>رقم/اسم الموقع</label><input name="location_id" required></div><div class="field"><label>الزون</label><input name="zone_name"></div><div class="field"><label>التصنيف</label><select name="category"><option>كهرباء</option><option>سباكة</option><option>تكييف</option><option>نجارة</option><option>مولدات</option><option>أخرى</option></select></div><div class="field"><label>الأولوية</label><select name="priority"><option value="normal">عادي</option><option value="urgent">عاجل</option><option value="critical">طارئ</option></select></div></div><div class="field"><label>الوصف</label><textarea name="description" required></textarea></div><button class="btn">إرسال البلاغ</button></form></div>''','بلاغ جديد',u)
@app.route('/tickets/<int:tid>',methods=['GET','POST'])
@login_required
def ticket_detail(tid):
 u=current_user()
 with closing(conn()) as c:
  t=c.execute('SELECT * FROM maintenance_tickets WHERE id=?',(tid,)).fetchone()
  if not t:abort(404)
  if u['role'] in ('housing_supervisor','housing_monitor') and t['reported_by']!=u['id']:abort(403)
  if request.method=='POST':
   if not can_maintenance(u):abort(403)
   status=request.form['status'];notes=request.form.get('notes','');tech=request.form.get('technician_name','');fields={'new':'','in_progress':'started_at','completed':'completed_at','verified':'verified_at','closed':'closed_at'};timecol=fields.get(status,'')
   sql='UPDATE maintenance_tickets SET status=?,technician_name=?,completion_notes=?';params=[status,tech,notes]
   if timecol:sql+=f',{timecol}=?';params.append(now())
   sql+=' WHERE id=?';params.append(tid);c.execute(sql,params);c.execute('INSERT INTO ticket_updates(ticket_id,user_id,action,notes,created_at) VALUES(?,?,?,?,?)',(tid,u['id'],status,notes,now()));audit(c,u,status,'maintenance_ticket',tid);c.commit();return redirect(url_for('ticket_detail',tid=tid))
  updates=c.execute('''SELECT x.*,u.display_name FROM ticket_updates x LEFT JOIN users u ON u.id=x.user_id WHERE x.ticket_id=? ORDER BY x.id DESC''',(tid,)).fetchall()
 return page('''<div class="card"><h2>بلاغ {{t.ticket_no}}</h2><p><b>الموقع:</b> {{t.location_type}} {{t.location_id}}</p><p><b>التصنيف:</b> {{t.category}}</p><p><b>الوصف:</b> {{t.description}}</p><p><b>الحالة:</b> {{status_ar.get(t.status,t.status)}}</p>{% if can_update %}<form method="post"><select name="status"><option value="in_progress">قيد التنفيذ</option><option value="completed">مكتمل</option><option value="verified">تم التحقق</option><option value="closed">مغلق</option></select><input name="technician_name" placeholder="اسم الفني"><textarea name="notes" placeholder="ملاحظات التنفيذ"></textarea><button class="btn">تحديث</button></form>{% endif %}</div><div class="card"><h3>سجل التحديثات</h3><table class="tbl">{% for x in updates %}<tr><td>{{x.created_at}}</td><td>{{x.display_name}}</td><td>{{status_ar.get(x.action,x.action)}}</td><td>{{x.notes}}</td></tr>{% endfor %}</table></div>''','تفاصيل البلاغ',u,t=t,updates=updates,status_ar=STATUS_AR,can_update=can_maintenance(u))

@app.get('/users')
@login_required
def users():
 u=current_user()
 if not is_admin(u):abort(403)
 with closing(conn()) as c:rows=c.execute("SELECT u.*,GROUP_CONCAT(a.room_text,'، ') room_text FROM users u LEFT JOIN assignments a ON a.user_id=u.id GROUP BY u.id ORDER BY u.active DESC,u.role,u.display_name").fetchall()
 return page('''<h2>المستخدمون والصلاحيات</h2><table class="tbl"><tr><th>الرقم</th><th>الاسم</th><th>الدور</th><th>النطاق</th><th>الحالة</th></tr>{% for x in rows %}<tr><td>{{x.employee_no}}</td><td>{{x.display_name}}</td><td>{{roles.get(x.role,x.role)}}</td><td>{{x.room_text or '-'}}</td><td>{{'نشط' if x.active else 'موقوف'}}</td></tr>{% endfor %}</table>''','المستخدمون',u,rows=rows,roles=ROLE_AR)
@app.get('/audit-logs')
@login_required
def audit_logs():
 u=current_user()
 if not is_admin(u):abort(403)
 with closing(conn()) as c:rows=c.execute('SELECT * FROM audit_logs ORDER BY id DESC LIMIT 500').fetchall()
 return page('''<h2>سجل العمليات</h2><table class="tbl"><tr><th>التاريخ</th><th>المستخدم</th><th>العملية</th><th>النوع</th><th>الرقم</th><th>التفاصيل</th></tr>{% for x in rows %}<tr><td>{{x.created_at}}</td><td>{{x.username}}</td><td>{{x.action}}</td><td>{{x.entity_type}}</td><td>{{x.entity_id}}</td><td>{{x.details_json}}</td></tr>{% endfor %}</table>''','سجل العمليات',u,rows=rows)
@app.errorhandler(403)
def forbidden(e):return page('<div class="card"><h2>غير مصرح</h2><p>ليس لديك صلاحية لفتح هذه الصفحة.</p></div>','غير مصرح',current_user()),403
@app.errorhandler(404)
def notfound(e):return page('<div class="card"><h2>غير موجود</h2></div>','غير موجود',current_user()),404
if __name__=='__main__':app.run(host='0.0.0.0',port=int(os.environ.get('PORT','10000')))
