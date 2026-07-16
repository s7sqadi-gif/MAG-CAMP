from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
from http.cookies import SimpleCookie
from email.parser import BytesParser
from email.policy import default
import sqlite3, os, json, html, hashlib, hmac, secrets, re
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, 'data', 'mhoms.db')
UP = os.path.join(ROOT, 'uploads')
os.makedirs(UP, exist_ok=True)
SESS = {}
ROLE_AR = {'services_manager':'مدير الخدمات المساندة','housing_manager':'مدير السكن','housing_supervisor':'مشرف السكن','housing_monitor':'مراقب السكن','maintenance_manager':'مدير الصيانة','maintenance_supervisor':'مشرف الصيانة'}
STATUS_AR = {'new':'جديد','assigned':'تم الإسناد','in_progress':'تحت التنفيذ','pending_verification':'بانتظار تحقق مشرف السكن','closed':'مغلق','reopened':'معاد للصيانة'}

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def e(v): return html.escape('' if v is None else str(v))
def now(): return datetime.now().isoformat(timespec='seconds')
def verify(stored, p):
    try:
        s, h = stored.split(':')
        return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256', p.encode(), bytes.fromhex(s), 150000).hex(), h)
    except Exception:
        return False

def ph(p):
    s = secrets.token_bytes(16)
    return s.hex()+':'+hashlib.pbkdf2_hmac('sha256', p.encode(), s, 150000).hex()

def reqno(prefix): return f"{prefix}-{datetime.now().strftime('%Y%m%d')}-{secrets.randbelow(90000)+10000}"
def is_admin(u): return u['role'] in ('services_manager','housing_manager')

def assignment(u):
    c=db(); a=c.execute('select * from assignments where user_id=?',(u['id'],)).fetchone(); c.close(); return a

def room_allowed(u, room):
    if u['role'] in ('services_manager','housing_manager','housing_monitor','maintenance_manager','maintenance_supervisor'): return True
    if u['role'] != 'housing_supervisor': return False
    a = assignment(u)
    try: r = int(room)
    except Exception: return False
    return bool(a and a['room_start'] is not None and a['room_end'] is not None and a['room_start'] <= r <= a['room_end'])

def bathroom_allowed(u, zone):
    if u['role'] in ('services_manager','housing_manager','housing_monitor','maintenance_manager','maintenance_supervisor'): return True
    if u['role'] != 'housing_supervisor': return False
    a = assignment(u)
    return bool(a and zone and zone in (a['bathrooms_group'] or ''))

def responsible_supervisor(location_type, location_id, zone=''):
    c=db()
    if location_type == 'room':
        try: r=int(location_id)
        except Exception: r=-1
        x=c.execute("select u.id from users u join assignments a on a.user_id=u.id where u.role='housing_supervisor' and a.room_start is not null and ? between a.room_start and a.room_end limit 1",(r,)).fetchone()
    else:
        x=c.execute("select u.id from users u join assignments a on a.user_id=u.id where u.role='housing_supervisor' and a.bathrooms_group like ? limit 1",('%'+zone+'%',)).fetchone()
    c.close(); return x['id'] if x else None

def age_class(created):
    try:
        hours=(datetime.now()-datetime.fromisoformat(created)).total_seconds()/3600
        return 'age-green' if hours<24 else 'age-yellow' if hours<48 else 'age-orange' if hours<72 else 'age-red'
    except Exception: return ''

def multipart(handler):
    n=int(handler.headers.get('Content-Length','0') or 0); body=handler.rfile.read(n); ct=handler.headers.get('Content-Type','')
    msg=BytesParser(policy=default).parsebytes((f'Content-Type: {ct}\r\nMIME-Version: 1.0\r\n\r\n').encode()+body)
    fields={}; files={}
    if msg.is_multipart():
        for part in msg.iter_parts():
            name=part.get_param('name',header='content-disposition'); filename=part.get_filename(); data=part.get_payload(decode=True) or b''
            if filename: files[name]=(filename,part.get_content_type(),data)
            elif name: fields[name]=data.decode('utf-8','replace')
    return fields,files

def save_image(filetuple,prefix):
    if not filetuple or not filetuple[2]: return None
    fn,ctype,data=filetuple
    ext={'image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp'}.get(ctype,os.path.splitext(fn)[1].lower() or '.jpg')
    name=f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}{ext}"
    with open(os.path.join(UP,name),'wb') as f: f.write(data)
    return name

def nav(u):
    items=[('الرئيسية','/'),('العمال','/workers'),('الغرف','/rooms')]
    if u['role'] in ('housing_supervisor','housing_monitor','services_manager','housing_manager'):
        items += [('دورات المياه','/bathrooms'),('جولاتي','/rounds')]
    items += [('بلاغات الصيانة','/maintenance'),('طلبات التسكين','/requests')]
    if u['role'] in ('housing_supervisor','services_manager','housing_manager'): items.append(('الاعتمادات','/approvals'))
    if is_admin(u): items += [('المستخدمون','/users'),('سجل النشاط','/audit')]
    return ''.join(f'<a href="{url}">{title}</a>' for title,url in items)

def layout(body,u,title='MAG CAMP'):
    return f"""<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{e(title)}</title><link rel='stylesheet' href='/static/style.css'></head><body class='rtl'><header><div class='brand'><img src='/static/mag_logo.png'><div><b>MAG CAMP</b><br><small>نظام إدارة وتشغيل سكن ولي العهد</small></div></div><div><b>{e(u['display_name'])}</b><br><small>{e(ROLE_AR.get(u['role'],u['role']))}</small> | <a href='/logout'>خروج</a></div></header><div class='layout'><aside>{nav(u)}</aside><main>{body}</main></div></body></html>"""

class App(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def html(self,x,code=200):
        b=x.encode('utf-8'); self.send_response(code); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def redir(self,p): self.send_response(302); self.send_header('Location',p); self.end_headers()
    def form(self):
        n=int(self.headers.get('Content-Length','0') or 0); return {k:v[0] for k,v in parse_qs(self.rfile.read(n).decode()).items()}
    def sess(self):
        c=SimpleCookie(self.headers.get('Cookie')); s=c.get('magcamp'); return SESS.get(s.value) if s else None
    def auth(self):
        s=self.sess()
        if not s: self.redir('/login'); return None
        return s['u']
    def do_GET(self):
        p=urlparse(self.path); path=p.path
        if path.startswith('/static/'):
            f=os.path.join(ROOT,path.lstrip('/'))
            if not os.path.isfile(f): return self.html('Not found',404)
            b=open(f,'rb').read(); self.send_response(200); self.send_header('Content-Type','image/png' if f.endswith('.png') else 'text/css; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
        if path.startswith('/uploads/'):
            f=os.path.join(UP,os.path.basename(path))
            if not os.path.isfile(f): return self.html('Not found',404)
            b=open(f,'rb').read(); self.send_response(200); self.send_header('Content-Type','image/jpeg'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
        if path=='/login': return self.login()
        if path=='/logout':
            self.send_response(302); self.send_header('Set-Cookie','magcamp=; Max-Age=0; Path=/'); self.send_header('Location','/login'); self.end_headers(); return
        u=self.auth()
        if not u: return
        if path=='/': return self.dashboard(u)
        if path=='/change-password': return self.change_password_form(u)
        if path=='/workers': return self.workers(u,parse_qs(p.query))
        if path=='/rooms': return self.rooms(u,parse_qs(p.query))
        if path.startswith('/room/') and path.count('/')==2: return self.room(u,path.split('/')[2])
        if path.startswith('/room/') and path.endswith('/inspect'): return self.room_inspect_form(u,path.split('/')[2])
        if path=='/bathrooms': return self.bathrooms(u,parse_qs(p.query))
        if path.startswith('/bathroom/') and path.endswith('/inspect'): return self.bath_inspect_form(u,int(path.split('/')[2]))
        if path=='/rounds': return self.rounds(u)
        if path=='/maintenance': return self.maintenance(u,parse_qs(p.query))
        if path=='/ticket/new': return self.ticket_form(u,parse_qs(p.query))
        if path.startswith('/ticket/') and path.count('/')==2: return self.ticket(u,int(path.split('/')[2]))
        if path=='/requests': return self.requests(u)
        if path=='/requests/new': return self.new_form(u)
        if path.startswith('/workers/') and path.endswith('/transfer'): return self.transfer_form(u,int(path.split('/')[2]))
        if path=='/approvals': return self.approvals(u)
        if path=='/users': return self.users(u)
        if path=='/audit': return self.audit(u)
        self.html('Not found',404)
    def do_POST(self):
        path=urlparse(self.path).path
        if path=='/login': return self.login_post()
        u=self.auth()
        if not u: return
        if path=='/change-password': return self.change_password_post(u)
        if path.startswith('/room/') and path.endswith('/inspect'): return self.room_inspect_post(u,path.split('/')[2])
        if path.startswith('/bathroom/') and path.endswith('/inspect'): return self.bath_inspect_post(u,int(path.split('/')[2]))
        if path=='/ticket/new': return self.ticket_post(u)
        if path.startswith('/ticket/') and path.endswith('/update'): return self.ticket_update(u,int(path.split('/')[2]))
        if path=='/requests/new': return self.new_post(u)
        if path.startswith('/workers/') and path.endswith('/transfer'): return self.transfer_post(u,int(path.split('/')[2]))
        if path.startswith('/approvals/'): return self.decide(u,int(path.split('/')[2]))
        self.html('Not found',404)
    def login(self,msg=''):
        notice=f"<div class='notice'>{e(msg)}</div>" if msg else ''
        self.html(f"""<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><link rel='stylesheet' href='/static/style.css'></head><body class='login'><form class='loginbox' method='post'><img src='/static/mag_logo.png'><h1>MAG CAMP</h1><p>نظام إدارة وتشغيل السكن</p>{notice}<input name='username' inputmode='numeric' placeholder='الرقم الوظيفي' required><br><br><input type='password' name='password' placeholder='كلمة المرور' required><br><br><button class='btn primary' style='width:100%'>تسجيل الدخول</button><p class='muted'>كلمة المرور الافتراضية: 123456</p></form></body></html>""")
    def login_post(self):
        f=self.form(); c=db(); u=c.execute('select * from users where username=? and active=1',(f.get('username',''),)).fetchone()
        if not u or not verify(u['password_hash'],f.get('password','')): c.close(); return self.login('بيانات الدخول غير صحيحة')
        c.close()
        sid=secrets.token_urlsafe(24)
        SESS[sid]={'u':dict(u)}
        self.send_response(302)
        self.send_header('Set-Cookie',f'magcamp={sid}; Path=/; HttpOnly; SameSite=Lax')
        self.send_header('Location','/change-password' if u['must_change_password'] else '/')
        self.end_headers()
    def change_password_form(self,u,msg=''):
        notice=f"<div class='notice'>{e(msg)}</div>" if msg else ''
        self.html(layout(f"<div class='panel narrow'><h2>تغيير كلمة المرور</h2>{notice}<form method='post'><input type='password' name='p1' placeholder='كلمة المرور الجديدة' required><br><br><input type='password' name='p2' placeholder='تأكيد كلمة المرور' required><br><br><button class='btn primary'>حفظ</button></form></div>",u))
    def change_password_post(self,u):
        f=self.form()
        if len(f.get('p1',''))<6 or f.get('p1')!=f.get('p2'): return self.change_password_form(u,'كلمة المرور غير متطابقة أو أقل من 6 خانات')
        c=db(); c.execute('update users set password_hash=?,must_change_password=0 where id=?',(ph(f['p1']),u['id'])); c.commit(); c.close(); u['must_change_password']=0; self.redir('/')
    def dashboard(self,u):
        c=db(); tw=c.execute('select count(*) c from workers where archived=0').fetchone()['c']; rr=c.execute('select count(*) c from rooms').fetchone()['c']; pr=c.execute("select count(*) c from requests where status='pending'").fetchone()['c']; mt=c.execute("select count(*) c from maintenance_tickets where status<>'closed'").fetchone()['c']; pv=c.execute("select count(*) c from maintenance_tickets where status='pending_verification'").fetchone()['c']; c.close()
        body=f"<div class='cards'><div class='card'>إجمالي العمال<div class='value'>{tw}</div></div><div class='card'>إجمالي الغرف<div class='value'>{rr}</div></div><div class='card'>بلاغات مفتوحة<div class='value warn'>{mt}</div></div><div class='card'>بانتظار التحقق<div class='value ambertext'>{pv}</div></div><div class='card'>طلبات تسكين معلقة<div class='value'>{pr}</div></div></div><div class='panel'><h2>مرحبًا {e(u['display_name'])}</h2><p>استخدم القائمة للوصول إلى الغرف والجولات وبلاغات الصيانة.</p></div>"
        self.html(layout(body,u))
    def workers(self,u,q):
        term=(q.get('q')or[''])[0]; sql='select * from workers where archived=0'; a=[]
        if term: sql+=' and (employee_no like ? or iqama_no like ? or full_name like ? or room_no like ?)'; x='%'+term+'%'; a=[x,x,x,x]
        sql+=' order by full_name limit 500'; c=db(); rs=c.execute(sql,a).fetchall(); c.close()
        rows=''.join(f"<tr><td>{e(r['employee_no'])}</td><td>{e(r['full_name'])}</td><td>{e(r['room_no'])}</td><td><a class='btn small' href='/workers/{r['id']}/transfer'>طلب نقل</a></td></tr>" for r in rs)
        self.html(layout(f"<div class='panel'><form class='filters'><input name='q' value='{e(term)}' placeholder='الاسم أو الرقم الوظيفي أو الغرفة'><button class='btn primary'>بحث</button></form></div><div class='panel tablewrap'><table><tr><th>الرقم الوظيفي</th><th>الاسم</th><th>الغرفة</th><th></th></tr>{rows}</table></div>",u))
    def rooms(self,u,q):
        term=(q.get('q')or[''])[0]; c=db(); sql='select r.zone,r.room_no,r.capacity,count(w.id) occ from rooms r left join workers w on w.room_no=r.room_no and w.archived=0'; a=[]; where=[]
        if term: where.append('r.room_no like ?'); a.append('%'+term+'%')
        if u['role']=='housing_supervisor':
            ass=assignment(u)
            if ass and ass['room_start'] is not None: where.append('cast(r.room_no as integer) between ? and ?'); a += [ass['room_start'],ass['room_end']]
            else: where.append('1=0')
        if where: sql+=' where '+' and '.join(where)
        sql+=' group by r.id order by cast(r.room_no as integer) limit 1000'; rs=c.execute(sql,a).fetchall(); c.close()
        cards=''.join(f"<a class='roomcard {'over' if r['occ']>r['capacity'] else ''}' href='/room/{e(r['room_no'])}'><b>غرفة {e(r['room_no'])}</b><span>المسجل: {r['occ']} / السعة: {r['capacity']}</span></a>" for r in rs)
        self.html(layout(f"<div class='panel'><form class='filters'><input name='q' value='{e(term)}' placeholder='رقم الغرفة'><button class='btn primary'>بحث</button></form></div><div class='roomgrid'>{cards}</div>",u))
    def room(self,u,no):
        if not room_allowed(u,no): return self.html('غير مصرح',403)
        c=db(); r=c.execute('select * from rooms where room_no=?',(no,)).fetchone(); ws=c.execute('select * from workers where room_no=? and archived=0 order by full_name',(no,)).fetchall(); last=c.execute("select * from inspections where inspection_type='room' and location_id=? order by id desc limit 1",(no,)).fetchone(); c.close()
        people=''.join(f"<tr><td>{e(w['employee_no'])}</td><td>{e(w['full_name'])}</td></tr>" for w in ws); lasttxt=f"آخر جولة: العدد الفعلي {last['actual_count']} - النظافة {e(last['cleanliness'])}" if last else 'لا توجد جولة مسجلة'; cap=r['capacity'] if r else '-'; zone=r['zone'] if r else ''
        body=f"<div class='panel'><h2>الغرفة {e(no)}</h2><div class='statline'><span>السعة: {cap}</span><span>المسجلون: {len(ws)}</span></div><p>{lasttxt}</p><div class='actions'><a class='btn primary' href='/room/{e(no)}/inspect'>بدء جولة الغرفة</a><a class='btn danger' href='/ticket/new?type=room&id={quote(no)}&zone={zone}'>بلاغ صيانة</a></div></div><div class='panel tablewrap'><h3>الأشخاص المسجلون</h3><table><tr><th>الرقم الوظيفي</th><th>الاسم</th></tr>{people}</table></div>"
        self.html(layout(body,u))
    def room_inspect_form(self,u,no):
        if not room_allowed(u,no): return self.html('غير مصرح',403)
        c=db(); count=c.execute('select count(*) c from workers where room_no=? and archived=0',(no,)).fetchone()['c']; c.close()
        body=f"<div class='panel narrow'><h2>جولة الغرفة {e(no)}</h2><form method='post'><label>العدد المسجل بالنظام</label><input value='{count}' readonly><br><br><label>العدد الفعلي الموجود</label><input type='number' min='0' name='actual_count' required><br><br><label>حالة النظافة</label><select name='cleanliness'><option>ممتازة</option><option>مقبولة</option><option>سيئة</option></select><br><br><label>الأرقام الوظيفية للأشخاص الزائدين (افصل بينها بفاصلة)</label><input name='extra_employee_nos' placeholder='مثال: 12345, 67890'><br><br><label>ملاحظات</label><textarea name='notes'></textarea><br><br><button class='btn primary'>حفظ الجولة</button></form></div>"
        self.html(layout(body,u))
    def room_inspect_post(self,u,no):
        if not room_allowed(u,no): return self.html('غير مصرح',403)
        f=self.form(); c=db(); reg=c.execute('select count(*) c from workers where room_no=? and archived=0',(no,)).fetchone()['c']; c.execute("insert into inspections(inspection_type,location_id,inspector_id,registered_count,actual_count,cleanliness,notes,created_at) values('room',?,?,?,?,?,?,?)",(no,u['id'],reg,int(f.get('actual_count') or 0),f.get('cleanliness'),f.get('notes',''),now())); iid=c.lastrowid
        for emp in re.split(r'[,،\s]+',f.get('extra_employee_nos','').strip()):
            if not emp: continue
            w=c.execute('select id,room_no from workers where employee_no=?',(emp,)).fetchone(); c.execute('insert into inspection_people(inspection_id,employee_no,worker_id,registered_room,discrepancy_type) values(?,?,?,?,?)',(iid,emp,w['id'] if w else None,w['room_no'] if w else None,'extra'))
        c.execute('insert into audit_logs(user_id,username,action,entity_type,entity_id,details_json,created_at) values(?,?,?,?,?,?,?)',(u['id'],u['display_name'],'room_inspection','room',iid,json.dumps({'room':no,'registered':reg,'actual':f.get('actual_count')},ensure_ascii=False),now())); c.commit(); c.close(); self.redir('/room/'+quote(no))
    def bathrooms(self,u,q):
        term=(q.get('q')or[''])[0]; c=db(); sql='select * from bathrooms where active=1'; a=[]
        if u['role']=='housing_supervisor':
            ass=assignment(u); group=(ass['bathrooms_group'] or '').replace('دورات مياه','').strip() if ass else ''
            if group: sql+=' and zone_name like ?'; a.append('%'+group+'%')
            else: sql+=' and 1=0'
        if term: sql+=' and (bathroom_no like ? or zone_name like ?)'; a += ['%'+term+'%','%'+term+'%']
        sql+=' order by cast(bathroom_no as integer)'; rs=c.execute(sql,a).fetchall(); c.close()
        cards=''.join(f"<div class='roomcard'><b>دورة مياه رقم {e(r['bathroom_no'])}</b><span>زون {e(r['zone_name'])}</span><div class='actions'><a class='btn small' href='/bathroom/{r['id']}/inspect'>تسجيل جولة</a><a class='btn danger small' href='/ticket/new?type=bathroom&id={quote(r['bathroom_no'])}&zone={quote(r['zone_name'])}'>بلاغ</a></div></div>" for r in rs)
        self.html(layout(f"<div class='panel'><form class='filters'><input name='q' value='{e(term)}' placeholder='رقم دورة المياه أو الزون'><button class='btn primary'>بحث</button></form></div><div class='roomgrid'>{cards}</div>",u))
    def bath_inspect_form(self,u,bid):
        c=db(); b=c.execute('select * from bathrooms where id=?',(bid,)).fetchone(); c.close()
        if not b or not bathroom_allowed(u,b['zone_name']): return self.html('غير مصرح',403)
        self.html(layout(f"<div class='panel narrow'><h2>جولة دورة المياه رقم {e(b['bathroom_no'])}</h2><p>زون {e(b['zone_name'])}</p><form method='post'><label>النظافة</label><select name='cleanliness'><option>ممتازة</option><option>مقبولة</option><option>سيئة</option></select><br><br><label>ملاحظات</label><textarea name='notes'></textarea><br><br><button class='btn primary'>حفظ الجولة</button></form></div>",u))
    def bath_inspect_post(self,u,bid):
        f=self.form(); c=db(); b=c.execute('select * from bathrooms where id=?',(bid,)).fetchone()
        if not b or not bathroom_allowed(u,b['zone_name']): c.close(); return self.html('غير مصرح',403)
        c.execute("insert into inspections(inspection_type,location_id,zone_name,inspector_id,cleanliness,notes,created_at) values('bathroom',?,?,?,?,?,?)",(b['bathroom_no'],b['zone_name'],u['id'],f.get('cleanliness'),f.get('notes',''),now())); c.commit(); c.close(); self.redir('/bathrooms')
    def rounds(self,u):
        c=db(); rs=c.execute('select i.*,u.display_name from inspections i join users u on u.id=i.inspector_id where i.inspector_id=? order by i.id desc limit 200',(u['id'],)).fetchall(); c.close()
        rows=''.join(f"<tr><td>{e(r['created_at'])}</td><td>{'غرفة' if r['inspection_type']=='room' else 'دورة مياه'} {e(r['location_id'])}</td><td>{e(r['cleanliness'])}</td><td>{r['actual_count'] if r['inspection_type']=='room' else '-'}</td><td>{e(r['notes'])}</td></tr>" for r in rs)
        self.html(layout(f"<div class='panel tablewrap'><h2>سجل جولاتي</h2><table><tr><th>التاريخ</th><th>الموقع</th><th>النظافة</th><th>العدد الفعلي</th><th>الملاحظات</th></tr>{rows}</table></div>",u))
    def ticket_form(self,u,q):
        typ=(q.get('type')or['room'])[0]; lid=(q.get('id')or[''])[0]; zone=(q.get('zone')or[''])[0]
        body=f"<div class='panel narrow'><h2>إنشاء بلاغ صيانة</h2><form method='post' enctype='multipart/form-data'><label>نوع الموقع</label><select name='location_type'><option value='room' {'selected' if typ=='room' else ''}>غرفة</option><option value='bathroom' {'selected' if typ=='bathroom' else ''}>دورة مياه</option></select><br><br><label>رقم الموقع</label><input name='location_id' value='{e(lid)}' required><br><br><label>الزون</label><input name='zone_name' value='{e(zone)}'><br><br><label>نوع العطل</label><select name='category'><option>سباكة</option><option>كهرباء</option><option>تكييف</option><option>نجارة</option><option>دهانات</option><option>أخرى</option></select><br><br><label>الأولوية</label><select name='priority'><option value='normal'>عادي</option><option value='medium'>متوسط</option><option value='urgent'>عاجل</option></select><br><br><label>وصف العطل</label><textarea name='description' required></textarea><br><br><label>صورة الملاحظة</label><input type='file' name='photo' accept='image/*' capture='environment' required><p class='muted'>تُرفع الصورة مباشرة إلى النظام.</p><button class='btn danger'>إرسال البلاغ</button></form></div>"
        self.html(layout(body,u))
    def ticket_post(self,u):
        f,files=multipart(self); typ=f.get('location_type','room'); lid=f.get('location_id',''); zone=f.get('zone_name','')
        if typ=='room' and not room_allowed(u,lid): return self.html('غير مصرح',403)
        if typ=='bathroom' and not bathroom_allowed(u,zone): return self.html('غير مصرح',403)
        photo=save_image(files.get('photo'),'before'); tn=reqno('MNT'); ver=responsible_supervisor(typ,lid,zone); c=db(); c.execute('insert into maintenance_tickets(ticket_no,location_type,location_id,zone_name,category,description,priority,status,reported_by,verification_by,before_photo,created_at) values(?,?,?,?,?,?,?,?,?,?,?,?)',(tn,typ,lid,zone,f.get('category'),f.get('description'),f.get('priority','normal'),'new',u['id'],ver,photo,now())); tid=c.lastrowid; c.execute('insert into ticket_updates(ticket_id,user_id,action,notes,photo_path,created_at) values(?,?,?,?,?,?)',(tid,u['id'],'created',f.get('description'),photo,now())); c.commit(); c.close(); self.redir('/ticket/'+str(tid))
    def maintenance(self,u,q):
        status=(q.get('status')or[''])[0]; c=db(); sql='select t.*,ru.display_name reporter,vu.display_name verifier from maintenance_tickets t join users ru on ru.id=t.reported_by left join users vu on vu.id=t.verification_by where 1=1'; a=[]
        if status: sql+=' and t.status=?'; a.append(status)
        if u['role']=='housing_supervisor': sql+=' and (t.reported_by=? or t.verification_by=?)'; a += [u['id'],u['id']]
        elif u['role']=='housing_monitor': sql+=' and t.reported_by=?'; a.append(u['id'])
        sql+=' order by t.id desc'; rs=c.execute(sql,a).fetchall(); c.close()
        rows=''.join(f"<tr class='{age_class(r['created_at'])}'><td><a href='/ticket/{r['id']}'>{e(r['ticket_no'])}</a></td><td>{'غرفة' if r['location_type']=='room' else 'دورة مياه'} {e(r['location_id'])}<br><small>{e(r['zone_name'])}</small></td><td>{e(r['category'])}</td><td>{e(r['reporter'])}</td><td>{e(STATUS_AR.get(r['status'],r['status']))}</td><td>{e(r['created_at'])}</td></tr>" for r in rs)
        body=f"<div class='panel'><div class='actions'><a class='btn danger' href='/ticket/new'>بلاغ جديد</a><a class='btn small' href='/maintenance'>الكل</a><a class='btn small' href='/maintenance?status=new'>جديد</a><a class='btn small' href='/maintenance?status=in_progress'>تحت التنفيذ</a><a class='btn small' href='/maintenance?status=pending_verification'>بانتظار التحقق</a></div></div><div class='panel tablewrap'><table><tr><th>البلاغ</th><th>الموقع</th><th>النوع</th><th>المبلغ</th><th>الحالة</th><th>التاريخ</th></tr>{rows}</table></div>"
        self.html(layout(body,u))
    def ticket(self,u,tid):
        c=db(); t=c.execute('select t.*,ru.display_name reporter,vu.display_name verifier,au.display_name assigned from maintenance_tickets t join users ru on ru.id=t.reported_by left join users vu on vu.id=t.verification_by left join users au on au.id=t.assigned_to where t.id=?',(tid,)).fetchone(); ups=c.execute('select x.*,u.display_name from ticket_updates x join users u on u.id=x.user_id where x.ticket_id=? order by x.id',(tid,)).fetchall(); c.close()
        if not t: return self.html('غير موجود',404)
        if u['role']=='housing_supervisor' and u['id'] not in (t['reported_by'],t['verification_by']): return self.html('غير مصرح',403)
        if u['role']=='housing_monitor' and u['id']!=t['reported_by']: return self.html('غير مصرح',403)
        tl=[]
        for x in ups:
            img=f"<img src='/uploads/{e(x['photo_path'])}'>" if x['photo_path'] else ''
            tl.append(f"<div class='timelineitem'><b>{e(x['display_name'])}</b> — {e(x['action'])}<br><small>{e(x['created_at'])}</small><p>{e(x['notes'])}</p>{img}</div>")
        before=f"<img class='ticketimg' src='/uploads/{e(t['before_photo'])}'>" if t['before_photo'] else ''
        after=f"<img class='ticketimg' src='/uploads/{e(t['after_photo'])}'>" if t['after_photo'] else ''
        actions=''
        if u['role'] in ('maintenance_manager','maintenance_supervisor') and t['status'] in ('new','assigned','reopened'):
            actions=f"<form method='post' action='/ticket/{tid}/update' enctype='multipart/form-data' class='panel'><h3>إجراء الصيانة</h3><input type='hidden' name='action' value='complete'><label>اسم الفني</label><input name='technician_name' required><br><br><label>القطعة المستخدمة</label><input name='part_name' required><br><br><label>ما تم تنفيذه</label><textarea name='notes' required></textarea><br><br><label>صورة بعد الإصلاح</label><input type='file' name='photo' accept='image/*' capture='environment' required><br><br><button class='btn primary'>تم الانتهاء وإرسال للتحقق</button></form>"
        if t['status']=='pending_verification' and (u['id']==t['verification_by'] or is_admin(u)):
            actions+=f"<form method='post' action='/ticket/{tid}/update' class='panel'><h3>تحقق مشرف السكن ميدانيًا</h3><textarea name='notes' placeholder='ملاحظة التحقق أو سبب إعادة البلاغ'></textarea><div class='actions'><button name='action' value='verify' class='btn primary'>تم التحقق وإغلاق البلاغ</button><button name='action' value='reopen' class='btn danger'>الإصلاح غير مكتمل وإعادته للصيانة</button></div></form>"
        body=f"<div class='panel'><h2>{e(t['ticket_no'])}</h2><div class='statline'><span>الموقع: {'غرفة' if t['location_type']=='room' else 'دورة مياه'} {e(t['location_id'])}</span><span>الزون: {e(t['zone_name'])}</span><span>الحالة: {e(STATUS_AR.get(t['status'],t['status']))}</span></div><p><b>المبلغ:</b> {e(t['reporter'])}</p><p><b>الوصف:</b> {e(t['description'])}</p><div class='imagepair'>{before}{after}</div></div>{actions}<div class='panel'><h3>الخط الزمني</h3>{''.join(tl)}</div>"
        self.html(layout(body,u))
    def ticket_update(self,u,tid):
        ct=self.headers.get('Content-Type',''); f,files=(multipart(self) if ct.startswith('multipart/') else (self.form(),{})); action=f.get('action'); c=db(); t=c.execute('select * from maintenance_tickets where id=?',(tid,)).fetchone()
        if not t: c.close(); return self.html('غير موجود',404)
        if action=='complete':
            if u['role'] not in ('maintenance_manager','maintenance_supervisor'): c.close(); return self.html('غير مصرح',403)
            photo=save_image(files.get('photo'),'after'); c.execute("update maintenance_tickets set status='pending_verification',assigned_to=?,technician_name=?,part_name=?,completion_notes=?,after_photo=?,started_at=coalesce(started_at,?),completed_at=? where id=?",(u['id'],f.get('technician_name'),f.get('part_name'),f.get('notes'),photo,now(),now(),tid)); c.execute('insert into ticket_updates(ticket_id,user_id,action,notes,photo_path,created_at) values(?,?,?,?,?,?)',(tid,u['id'],'maintenance_completed',f"الفني: {f.get('technician_name')} | القطعة: {f.get('part_name')} | {f.get('notes')}",photo,now()))
        elif action in ('verify','reopen'):
            if not (u['id']==t['verification_by'] or is_admin(u)): c.close(); return self.html('غير مصرح',403)
            st='closed' if action=='verify' else 'reopened'; c.execute('update maintenance_tickets set status=?,verified_at=?,closed_at=? where id=?',(st,now(),now() if st=='closed' else None,tid)); c.execute('insert into ticket_updates(ticket_id,user_id,action,notes,created_at) values(?,?,?,?,?)',(tid,u['id'],'verified_closed' if st=='closed' else 'reopened',f.get('notes',''),now()))
        c.commit(); c.close(); self.redir('/ticket/'+str(tid))
    def requests(self,u):
        c=db(); rs=c.execute('select r.*,u.display_name requester from requests r join users u on u.id=r.requested_by order by r.id desc').fetchall(); c.close(); rows=''.join(f"<tr><td>{e(r['request_no'])}</td><td>{e(r['request_type'])}</td><td>{e(r['requester'])}</td><td>{e(r['status'])}</td><td>{e(r['created_at'])}</td></tr>" for r in rs); extra="<a class='btn primary' href='/requests/new'>طلب تسكين جديد</a>" if u['role']=='housing_monitor' else ''
        self.html(layout(f"<div class='panel'>{extra}</div><div class='panel tablewrap'><table><tr><th>الطلب</th><th>النوع</th><th>المقدم</th><th>الحالة</th><th>التاريخ</th></tr>{rows}</table></div>",u))
    def new_form(self,u):
        if u['role']!='housing_monitor': return self.html('مراقبو السكن فقط',403)
        self.html(layout("<div class='panel narrow'><h2>طلب تسكين جديد</h2><form method='post'><input name='employee_no' placeholder='الرقم الوظيفي' required><br><br><input name='iqama_no' placeholder='رقم الإقامة'><br><br><input name='full_name' placeholder='الاسم' required><br><br><input name='nationality' placeholder='الجنسية'><br><br><input name='profession' placeholder='المهنة'><br><br><input name='zone' placeholder='الزون' required><br><br><input name='room_no' placeholder='الغرفة' required><br><br><textarea name='reason' placeholder='السبب' required></textarea><br><br><button class='btn primary'>إرسال للاعتماد</button></form></div>",u))
    def new_post(self,u):
        if u['role']!='housing_monitor': return self.html('غير مصرح',403)
        f=self.form(); p={k:f.get(k,'') for k in ('employee_no','iqama_no','full_name','nationality','profession','zone','room_no','reason')}; c=db(); rn=reqno('ACC'); c.execute('insert into requests(request_no,request_type,payload_json,requested_by,status,created_at) values(?,?,?,?,?,?)',(rn,'new_housing',json.dumps(p,ensure_ascii=False),u['id'],'pending',now())); c.commit(); c.close(); self.redir('/requests')
    def transfer_form(self,u,wid):
        if u['role']!='housing_monitor': return self.html('مراقبو السكن فقط',403)
        c=db(); w=c.execute('select * from workers where id=?',(wid,)).fetchone(); c.close(); self.html(layout(f"<div class='panel narrow'><h2>طلب نقل {e(w['full_name'])}</h2><form method='post'><input name='zone' placeholder='الزون الجديد' required><br><br><input name='room_no' placeholder='الغرفة الجديدة' required><br><br><textarea name='reason' placeholder='سبب النقل' required></textarea><br><br><button class='btn primary'>إرسال للاعتماد</button></form></div>",u))
    def transfer_post(self,u,wid):
        if u['role']!='housing_monitor': return self.html('غير مصرح',403)
        f=self.form(); c=db(); w=c.execute('select * from workers where id=?',(wid,)).fetchone(); p={'old_room':w['room_no'],'old_zone':w['zone'],'new_room':f['room_no'],'new_zone':f['zone'],'reason':f['reason']}; rn=reqno('TRF'); c.execute('insert into requests(request_no,request_type,worker_id,payload_json,requested_by,status,created_at) values(?,?,?,?,?,?,?)',(rn,'transfer_req',wid,json.dumps(p,ensure_ascii=False),u['id'],'pending',now())); c.commit(); c.close(); self.redir('/requests')
    def approvals(self,u):
        if u['role'] not in ('housing_supervisor','services_manager','housing_manager'): return self.html('غير مصرح',403)
        c=db(); rs=c.execute("select r.*,u.display_name requester from requests r join users u on u.id=r.requested_by where r.status='pending'").fetchall(); arr=[]
        for r in rs:
            p=json.loads(r['payload_json']); room=p.get('room_no') or p.get('new_room') or ''
            if room_allowed(u,room): arr.append(f"<tr><td>{e(r['request_no'])}</td><td>{e(r['request_type'])}</td><td>{e(r['requester'])}</td><td>{e(room)}</td><td><form method='post' action='/approvals/{r['id']}'><input name='reason' placeholder='سبب القرار'><button name='decision' value='approve' class='btn primary small'>اعتماد</button><button name='decision' value='reject' class='btn danger small'>رفض</button></form></td></tr>")
        c.close(); self.html(layout(f"<div class='panel tablewrap'><table><tr><th>الطلب</th><th>النوع</th><th>المقدم</th><th>الغرفة</th><th>الإجراء</th></tr>{''.join(arr) or '<tr><td colspan=5>لا توجد طلبات</td></tr>'}</table></div>",u))
    def decide(self,u,rid):
        f=self.form(); c=db(); r=c.execute('select * from requests where id=?',(rid,)).fetchone(); p=json.loads(r['payload_json']); room=p.get('room_no') or p.get('new_room') or ''
        if not room_allowed(u,room): c.close(); return self.html('غير مصرح',403)
        st='approved' if f.get('decision')=='approve' else 'rejected'
        if st=='approved':
            if r['request_type']=='new_housing': c.execute("insert into workers(employee_no,iqama_no,full_name,nationality,profession,zone,room_no,status,created_at,updated_at) values(?,?,?,?,?,?,?,'active',?,?)",(p['employee_no'],p.get('iqama_no',''),p['full_name'],p.get('nationality',''),p.get('profession',''),p['zone'],p['room_no'],now(),now()))
            else: c.execute('update workers set zone=?,room_no=?,updated_at=? where id=?',(p['new_zone'],p['new_room'],now(),r['worker_id']))
        c.execute('update requests set status=?,approver_id=?,decision_reason=?,decided_at=? where id=?',(st,u['id'],f.get('reason',''),now(),rid)); c.commit(); c.close(); self.redir('/approvals')
    def users(self,u):
        if not is_admin(u): return self.html('غير مصرح',403)
        c=db(); rs=c.execute('select u.*,a.room_text,a.bathrooms_group from users u left join assignments a on a.user_id=u.id order by u.role,u.display_name').fetchall(); c.close(); rows=''.join(f"<tr><td>{e(r['employee_no'])}</td><td>{e(r['display_name'])}</td><td>{e(r['username'])}</td><td>{e(ROLE_AR.get(r['role'],r['role']))}</td><td>{e((r['room_text'] or '')+' '+(r['bathrooms_group'] or ''))}</td></tr>" for r in rs)
        self.html(layout(f"<div class='panel'><p>اسم المستخدم لكل موظف هو الرقم الوظيفي، وكلمة المرور الافتراضية 123456.</p></div><div class='panel tablewrap'><table><tr><th>الرقم الوظيفي</th><th>الاسم</th><th>اسم المستخدم</th><th>الدور</th><th>النطاق</th></tr>{rows}</table></div>",u))
    def audit(self,u):
        if not is_admin(u): return self.html('غير مصرح',403)
        c=db(); rs=c.execute('select * from audit_logs order by id desc limit 1000').fetchall(); c.close(); rows=''.join(f"<tr><td>{e(r['created_at'])}</td><td>{e(r['username'])}</td><td>{e(r['action'])}</td><td>{e(r['details_json'])}</td></tr>" for r in rs)
        self.html(layout(f"<div class='panel tablewrap'><table><tr><th>التاريخ</th><th>المستخدم</th><th>العملية</th><th>التفاصيل</th></tr>{rows}</table></div>",u))

if __name__=='__main__':
    ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),App).serve_forever()
