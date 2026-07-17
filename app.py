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
REQ_AR={'transfer':'نقل عامل','final_exit':'خروج نهائي','outside_temp':'سكن خارجي مؤقت','outside_perm':'سكن خارجي دائم'}
STATUS_AR={'pending':'بانتظار الاعتماد','approved':'معتمد','rejected':'مرفوض','new':'جديد','in_progress':'قيد التنفيذ','completed':'مكتمل','verified':'تم التحقق','closed':'مغلق'}

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
  CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
  CREATE INDEX IF NOT EXISTS idx_inspections_location ON inspections(inspection_type,location_id,created_at);
  CREATE INDEX IF NOT EXISTS idx_tickets_status ON maintenance_tickets(status);
  ''')
  # future-safe additive columns
  for table,defs in {'requests':{'supervisor_id':'INTEGER','supervisor_decision_at':'TEXT'},'inspections':{'week_key':'TEXT'}}.items():
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

BASE='''<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}}</title><style>
body{font-family:Tahoma,Arial;background:#f4f6f8;margin:0;color:#24302d}.top{background:#123b32;color:#fff;padding:14px 24px;display:flex;justify-content:space-between;align-items:center}.wrap{display:flex;min-height:calc(100vh - 70px)}aside{width:235px;background:#fff;padding:14px;box-shadow:0 0 8px #ccc}aside a{display:block;padding:10px;color:#123b32;text-decoration:none;border-bottom:1px solid #eee}main{flex:1;padding:24px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}.card{background:#fff;border-radius:12px;padding:18px;box-shadow:0 2px 8px #ddd;margin-bottom:15px}.num{font-size:28px;font-weight:bold}.tbl{width:100%;border-collapse:collapse;background:#fff}.tbl th,.tbl td{padding:9px;border-bottom:1px solid #eee;text-align:right;font-size:14px}.search,input,select,textarea{padding:10px;border:1px solid #ccd4d1;border-radius:7px;max-width:100%;box-sizing:border-box}.search{width:min(420px,90%);margin-bottom:10px}.btn{display:inline-block;background:#123b32;color:#fff;border:0;border-radius:7px;padding:10px 16px;text-decoration:none;cursor:pointer}.btn2{background:#6a7c76}.danger{background:#a52222}.login{max-width:390px;margin:10vh auto;background:#fff;padding:28px;border-radius:14px;box-shadow:0 3px 18px #bbb}.err{color:#b00020}.ok{color:#0a6b3c}.badge{padding:4px 8px;border-radius:10px;background:#e5eee9;white-space:nowrap}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}.field label{display:block;margin:5px 0}.field input,.field select,.field textarea{width:100%}@media(max-width:800px){aside{width:160px}.wrap{display:block}aside{width:auto}.tbl{display:block;overflow:auto}}
</style></head><body><div class="top"><div><b>MAG CAMP</b><br><small>نظام إدارة سكن ولي العهد — المرحلة الثانية</small></div>{% if u %}<div>{{u['display_name']}} — {{roles.get(u['role'],u['role'])}} | <a style="color:white" href="{{url_for('logout')}}">خروج</a></div>{% endif %}</div>{% if u %}<div class="wrap"><aside><a href="{{url_for('dashboard')}}">الرئيسية</a><a href="{{url_for('workers')}}">العمال</a><a href="{{url_for('rooms')}}">الغرف</a><a href="{{url_for('inspections')}}">الجرد الأسبوعي</a><a href="{{url_for('bathroom_inspections')}}">جرد دورات المياه</a><a href="{{url_for('requests_list')}}">طلبات العمال</a><a href="{{url_for('tickets')}}">بلاغات الصيانة</a>{% if admin %}<a href="{{url_for('users')}}">المستخدمون</a><a href="{{url_for('audit_logs')}}">سجل العمليات</a>{% endif %}<a href="{{url_for('change_password')}}">تغيير كلمة المرور</a></aside><main>{{body|safe}}</main></div>{% else %}{{body|safe}}{% endif %}</body></html>'''
def page(body,title='MAG CAMP',user=None,**ctx):return render_template_string(BASE,title=title,body=render_template_string(body,**ctx),u=user,roles=ROLE_AR,admin=bool(user and is_admin(user)))

@app.get('/health')
def health():
 try:
  with closing(conn()) as c:
   c.execute('SELECT 1'); counts={t:c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('users','rooms','workers','requests','inspections','maintenance_tickets')}
  return {'status':'ok','database':'ok','phase':2,'counts':counts},200
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
  workers=c.execute(f'''SELECT COUNT(*) FROM workers w WHERE archived=0 AND EXISTS(SELECT 1 FROM rooms r WHERE r.room_no=w.room_no AND {cl})''',args).fetchone()[0]
  done=c.execute("SELECT COUNT(DISTINCT location_id) FROM inspections WHERE inspection_type='room' AND inspector_id=? AND week_key=?",(u['id'],week)).fetchone()[0] if u['role']=='housing_supervisor' else c.execute("SELECT COUNT(*) FROM inspections WHERE inspection_type='room' AND week_key=?",(week,)).fetchone()[0]
  pending=c.execute("SELECT COUNT(*) FROM requests WHERE status='pending'").fetchone()[0]
  open_t=c.execute("SELECT COUNT(*) FROM maintenance_tickets WHERE status NOT IN ('closed','verified')").fetchone()[0]
 return page('''<h2>لوحة التحكم</h2><div class="cards"><div class="card"><div>الغرف المتاحة لك</div><div class="num">{{rooms}}</div></div><div class="card"><div>العمال الظاهرون لك</div><div class="num">{{workers}}</div></div><div class="card"><div>جرد هذا الأسبوع</div><div class="num">{{done}}</div></div><div class="card"><div>طلبات بانتظار الاعتماد</div><div class="num">{{pending}}</div></div><div class="card"><div>بلاغات صيانة مفتوحة</div><div class="num">{{open_t}}</div></div></div>''','الرئيسية',u,rooms=rooms,workers=workers,done=done,pending=pending,open_t=open_t)
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
 u=current_user();q=request.args.get('q','').strip();cl,args=assigned_clause(u,'r');sql=f'''SELECT r.*,COUNT(w.id) occupied FROM rooms r LEFT JOIN workers w ON w.room_no=r.room_no AND w.archived=0 WHERE {cl}''';p=list(args)
 if q:sql+=' AND r.room_no LIKE ?';p.append(f'%{q}%')
 sql+=' GROUP BY r.id ORDER BY CAST(r.room_no AS INTEGER) LIMIT 500'
 with closing(conn()) as c:rows=c.execute(sql,p).fetchall()
 return page('''<h2>الغرف</h2><form><input class="search" name="q" value="{{q}}" placeholder="رقم الغرفة"><button class="btn">بحث</button></form><table class="tbl"><tr><th>الزون</th><th>الغرفة</th><th>السعة</th><th>المشغول</th><th>الشاغر</th><th>الجرد</th></tr>{% for x in rows %}<tr><td>{{x.zone}}</td><td>{{x.room_no}}</td><td>{{x.capacity}}</td><td>{{x.occupied}}</td><td>{{x.capacity-x.occupied}}</td><td><a href="{{url_for('new_inspection',room_no=x.room_no)}}">إجراء</a></td></tr>{% endfor %}</table>''','الغرف',u,rows=rows,q=q)

@app.get('/inspections')
@login_required
def inspections():
 u=current_user();where='';p=[]
 if u['role']=='housing_supervisor':where='WHERE i.inspector_id=?';p=[u['id']]
 with closing(conn()) as c:rows=c.execute(f'''SELECT i.*,u.display_name FROM inspections i JOIN users u ON u.id=i.inspector_id {where} AND i.inspection_type='room' ORDER BY i.id DESC LIMIT 300''' if where else '''SELECT i.*,u.display_name FROM inspections i JOIN users u ON u.id=i.inspector_id WHERE i.inspection_type='room' ORDER BY i.id DESC LIMIT 300''',p).fetchall()
 return page('''<h2>الجرد الأسبوعي للغرف</h2><a class="btn" href="{{url_for('new_inspection')}}">جرد غرفة</a><table class="tbl"><tr><th>التاريخ</th><th>الغرفة</th><th>المسجل</th><th>الفعلي</th><th>النظافة</th><th>المشرف</th><th>ملاحظات</th></tr>{% for x in rows %}<tr><td>{{x.created_at}}</td><td>{{x.location_id}}</td><td>{{x.registered_count}}</td><td>{{x.actual_count}}</td><td>{{x.cleanliness}}</td><td>{{x.display_name}}</td><td>{{x.notes}}</td></tr>{% endfor %}</table>''','الجرد الأسبوعي',u,rows=rows)
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
 return page('''<div class="card"><h2>جرد غرفة</h2>{% if err %}<p class="err">{{err}}</p>{% endif %}<form method="post"><div class="grid"><div class="field"><label>رقم الغرفة</label><input name="room_no" value="{{room}}" required></div><div class="field"><label>العدد الفعلي</label><input type="number" min="0" name="actual_count" required></div><div class="field"><label>النظافة</label><select name="cleanliness"><option>ممتاز</option><option>جيد</option><option>يحتاج متابعة</option></select></div></div><div class="field"><label>الملاحظات</label><textarea name="notes"></textarea></div><button class="btn">حفظ الجرد</button></form></div>''','جرد غرفة',u,room=room,err=err)

@app.get('/bathroom-inspections')
@login_required
def bathroom_inspections():
 u=current_user()
 with closing(conn()) as c:rows=c.execute("SELECT i.*,u.display_name FROM inspections i JOIN users u ON u.id=i.inspector_id WHERE i.inspection_type='bathroom' ORDER BY i.id DESC LIMIT 300").fetchall()
 return page('''<h2>جرد دورات المياه</h2><a class="btn" href="{{url_for('new_bathroom_inspection')}}">إضافة جرد</a><table class="tbl"><tr><th>التاريخ</th><th>رقم الدورة</th><th>الحالة</th><th>المفتش</th><th>الملاحظات</th></tr>{% for x in rows %}<tr><td>{{x.created_at}}</td><td>{{x.location_id}}</td><td>{{x.cleanliness}}</td><td>{{x.display_name}}</td><td>{{x.notes}}</td></tr>{% endfor %}</table>''','جرد دورات المياه',u,rows=rows)
@app.route('/bathroom-inspections/new',methods=['GET','POST'])
@login_required
def new_bathroom_inspection():
 u=current_user()
 if request.method=='POST':
  no=request.form['bathroom_no'].strip();state=request.form['state'];notes=request.form.get('notes','')
  with closing(conn()) as c:
   cur=c.execute('INSERT INTO inspections(inspection_type,location_id,zone_name,inspector_id,cleanliness,notes,status,created_at,week_key) VALUES(?,?,?,?,?,?,?,?,?)',('bathroom',no,'',u['id'],state,notes,'completed',now(),date.today().strftime('%Y-W%W')));audit(c,u,'create','bathroom_inspection',cur.lastrowid,{'bathroom_no':no});c.commit()
  return redirect(url_for('bathroom_inspections'))
 return page('''<div class="card"><h2>جرد دورة مياه</h2><form method="post"><div class="grid"><div class="field"><label>رقم دورة المياه</label><input name="bathroom_no" required></div><div class="field"><label>الحالة</label><select name="state"><option>ممتاز</option><option>مغلق بسبب مستخدم</option><option>بدون محبس أرضي</option><option>بدون محبس ترويش</option><option>تسريب</option><option>يحتاج صيانة</option></select></div></div><div class="field"><label>الملاحظات</label><textarea name="notes"></textarea></div><button class="btn">حفظ</button></form></div>''','جرد دورة مياه',u)

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

@app.get('/tickets')
@login_required
def tickets():
 u=current_user()
 with closing(conn()) as c:rows=c.execute('''SELECT t.*,u.display_name reporter FROM maintenance_tickets t LEFT JOIN users u ON u.id=t.reported_by ORDER BY t.id DESC LIMIT 300''').fetchall()
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
