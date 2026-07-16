from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from http.cookies import SimpleCookie
import sqlite3,os,json,html,hashlib,hmac,secrets
from datetime import datetime
ROOT=os.path.dirname(os.path.abspath(__file__));DB=os.path.join(ROOT,'data','mhoms.db');SESS={}
TXT={'ar':{'dash':'الرئيسية','workers':'العمال','rooms':'الغرف','requests':'الطلبات','approvals':'الاعتمادات','users':'المستخدمون','audit':'سجل النشاط','logout':'خروج','lang':'English','title':'MAG CAMP - نظام إدارة السكن','site':'سكن ولي العهد'},'en':{'dash':'Dashboard','workers':'Workers','rooms':'Rooms','requests':'Requests','approvals':'Approvals','users':'Users','audit':'Activity Log','logout':'Logout','lang':'العربية','title':'MAG CAMP','site':'Wali Al Ahad Camp'}}
def db(): c=sqlite3.connect(DB);c.row_factory=sqlite3.Row;return c
def e(v):return html.escape('' if v is None else str(v))
def now():return datetime.now().isoformat(timespec='seconds')
def verify(stored,p):
 try:s,h=stored.split(':');return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256',p.encode(),bytes.fromhex(s),150000).hex(),h)
 except:return False
def reqno(prefix):return f"{prefix}-{datetime.now().strftime('%Y%m%d')}-{secrets.randbelow(9000)+1000}"
def can_approve(u,room):
 if u['role'] in ('services_manager','housing_manager'):return True
 if u['role']!='housing_supervisor':return False
 c=db();a=c.execute('select * from assignments where user_id=?',(u['id'],)).fetchone();c.close()
 try:r=int(room)
 except:return False
 return bool(a and a['room_start'] is not None and a['room_start']<=r<=a['room_end'])
def layout(body,u,lang):
 t=TXT[lang];d='rtl' if lang=='ar' else 'ltr';other='en' if lang=='ar' else 'ar';links=''.join(f'<a href="{url}">{t[key]}</a>' for key,url in [('dash','/'),('workers','/workers'),('rooms','/rooms'),('requests','/requests'),('approvals','/approvals'),('users','/users'),('audit','/audit')])
 return f'<!doctype html><html lang="{lang}" dir="{d}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body class="{d}"><header><div class="brand"><img src="/static/mag_logo.png"><div><b>{t["title"]}</b><br><small>{t["site"]}</small></div></div><div>{e(u["display_name"])} | <a href="/lang?set={other}">{t["lang"]}</a> | <a href="/logout">{t["logout"]}</a></div></header><div class="layout"><aside>{links}</aside><main>{body}</main></div></body></html>'
class App(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def html(self,x,code=200):
  b=x.encode();self.send_response(code);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def redir(self,p):self.send_response(302);self.send_header('Location',p);self.end_headers()
 def form(self):
  n=int(self.headers.get('Content-Length','0') or 0);return{k:v[0] for k,v in parse_qs(self.rfile.read(n).decode()).items()}
 def sess(self):
  c=SimpleCookie(self.headers.get('Cookie'));s=c.get('mhoms');return SESS.get(s.value) if s else None
 def auth(self):
  s=self.sess()
  if not s:self.redir('/login')
  return s
 def do_GET(self):
  p=urlparse(self.path);path=p.path
  if path.startswith('/static/'):
   f=os.path.join(ROOT,path.lstrip('/'));b=open(f,'rb').read();self.send_response(200);self.send_header('Content-Type','image/png' if f.endswith('.png') else 'text/css');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
  if path=='/login':return self.login()
  if path=='/logout':self.send_response(302);self.send_header('Set-Cookie','mhoms=; Max-Age=0; Path=/');self.send_header('Location','/login');self.end_headers();return
  s=self.auth()
  if not s:return
  u,lang=s['u'],s['lang']
  if path=='/lang':s['lang']=(parse_qs(p.query).get('set')or['ar'])[0];return self.redir('/')
  if path=='/':return self.dashboard(u,lang)
  if path=='/workers':return self.workers(u,lang,parse_qs(p.query))
  if path=='/rooms':return self.rooms(u,lang)
  if path=='/requests':return self.requests(u,lang)
  if path=='/requests/new':return self.new_form(u,lang)
  if path.startswith('/workers/') and path.endswith('/transfer'):return self.transfer_form(u,lang,int(path.split('/')[2]))
  if path=='/approvals':return self.approvals(u,lang)
  if path=='/users':return self.users(u,lang)
  if path=='/audit':return self.audit(u,lang)
  self.html('Not found',404)
 def do_POST(self):
  path=urlparse(self.path).path
  if path=='/login':return self.login_post()
  s=self.auth()
  if not s:return
  u,lang=s['u'],s['lang']
  if path=='/requests/new':return self.new_post(u)
  if path.startswith('/workers/') and path.endswith('/transfer'):return self.transfer_post(u,int(path.split('/')[2]))
  if path.startswith('/approvals/'):return self.decide(u,int(path.split('/')[2]))
 def login(self,msg=''):
  self.html(f'<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body class="login"><form class="loginbox" method="post"><img src="/static/mag_logo.png"><h2>MAG CAMP - نظام إدارة السكن</h2><p>MAG CAMP</p>{"<div class=notice>"+e(msg)+"</div>" if msg else ""}<input name="username" placeholder="اسم المستخدم / Username" required><br><br><input type="password" name="password" placeholder="كلمة المرور / Password" required><br><br><select name="lang"><option value="ar">العربية</option><option value="en">English</option></select><br><br><button class="btn primary" style="width:100%">دخول / Login</button></form></body></html>')
 def login_post(self):
  f=self.form();c=db();u=c.execute('select * from users where username=? and active=1',(f.get('username',''),)).fetchone();c.close()
  if not u or not verify(u['password_hash'],f.get('password','')):return self.login('بيانات الدخول غير صحيحة / Invalid credentials')
  sid=secrets.token_urlsafe(24);SESS[sid]={'u':dict(u),'lang':f.get('lang','ar')};self.send_response(302);self.send_header('Set-Cookie',f'mhoms={sid}; Path=/; HttpOnly');self.send_header('Location','/');self.end_headers()
 def dashboard(self,u,lang):
  c=db();tw=c.execute('select count(*) c from workers where archived=0').fetchone()['c'];rr=c.execute('select count(*) c from rooms').fetchone()['c'];cap=c.execute('select coalesce(sum(capacity),0)c from rooms').fetchone()['c'];pr=c.execute("select count(*)c from requests where status='pending'").fetchone()['c'];c.close();body=f'<div class="cards"><div class="card">إجمالي العمال / Total Workers<div class="value">{tw}</div></div><div class="card">إجمالي الغرف / Total Rooms<div class="value">{rr}</div></div><div class="card">الأسرة الشاغرة / Vacant Beds<div class="value">{max(cap-tw,0)}</div></div><div class="card">طلبات معلقة / Pending<div class="value warn">{pr}</div></div></div>';self.html(layout(body,u,lang))
 def workers(self,u,lang,q):
  term=(q.get('q')or[''])[0];sql='select * from workers where archived=0';a=[]
  if term:sql+=' and (employee_no like ? or iqama_no like ? or full_name like ? or room_no like ?)';x='%'+term+'%';a=[x,x,x,x]
  sql+=' order by full_name limit 500';c=db();rs=c.execute(sql,a).fetchall();c.close();rows=''.join(f'<tr><td>{e(r["employee_no"])}</td><td>{e(r["full_name"])}</td><td>{e(r["iqama_no"])}</td><td>{r["zone"]}</td><td>{e(r["room_no"])}</td><td><a class="btn amber" href="/workers/{r["id"]}/transfer">طلب نقل / Transfer</a></td></tr>' for r in rs);body=f'<div class="panel"><form><input name="q" value="{e(term)}" placeholder="بحث بالاسم أو الرقم أو الغرفة"><br><br><button class="btn primary">بحث / Search</button> <a class="btn primary" href="/requests/new">طلب تسكين جديد</a></form></div><div class="panel"><table><tr><th>الرقم الوظيفي</th><th>الاسم</th><th>الإقامة</th><th>الزون</th><th>الغرفة</th><th>الإجراء</th></tr>{rows}</table></div>';self.html(layout(body,u,lang))
 def rooms(self,u,lang):
  c=db();rs=c.execute('select r.zone,r.room_no,r.capacity,count(w.id) occ from rooms r left join workers w on w.room_no=r.room_no and w.archived=0 group by r.id order by r.zone,r.room_no').fetchall();c.close();rows=''.join(f'<tr><td>{r["zone"]}</td><td>{e(r["room_no"])}</td><td>{r["capacity"]}</td><td>{r["occ"]}</td><td>{max(r["capacity"]-r["occ"],0)}</td></tr>' for r in rs);self.html(layout(f'<div class="panel"><table><tr><th>الزون</th><th>الغرفة</th><th>السعة</th><th>الساكنون</th><th>الشاغر</th></tr>{rows}</table></div>',u,lang))
 def requests(self,u,lang):
  c=db();rs=c.execute('select r.*,u.display_name requester from requests r join users u on u.id=r.requested_by order by r.id desc').fetchall();c.close();rows=''.join(f'<tr><td>{e(r["request_no"])}</td><td>{e(r["request_type"])}</td><td>{e(r["requester"])}</td><td>{e(r["status"])}</td><td>{e(r["created_at"])}</td><td>{e(r["decision_reason"] or "")}</td></tr>' for r in rs);self.html(layout(f'<div class="panel"><table><tr><th>رقم الطلب</th><th>النوع</th><th>المقدم</th><th>الحالة</th><th>التاريخ</th><th>السبب</th></tr>{rows}</table></div>',u,lang))
 def new_form(self,u,lang):
  body='<div class="panel"><h3>طلب تسكين جديد / New Housing Request</h3><form method="post" class="grid"><input name="employee_no" placeholder="الرقم الوظيفي" required><input name="iqama_no" placeholder="رقم الإقامة"><input class="full" name="full_name" placeholder="الاسم" required><input name="nationality" placeholder="الجنسية"><input name="profession" placeholder="المهنة"><select name="zone"><option>1</option><option>2</option><option>3</option><option>4</option></select><input name="room_no" placeholder="رقم الغرفة" required><textarea class="full" name="reason" placeholder="سبب الطلب" required></textarea><button class="btn primary">إرسال الطلب</button></form></div>';self.html(layout(body,u,lang))
 def new_post(self,u):
  f=self.form();c=db();p={k:f.get(k,'') for k in ('employee_no','iqama_no','full_name','nationality','profession','zone','room_no','reason')};rn=reqno('ACC');c.execute('insert into requests(request_no,request_type,payload_json,requested_by,status,created_at) values(?,?,?,?,?,?)',(rn,'new_housing',json.dumps(p,ensure_ascii=False),u['id'],'pending',now()));c.execute('insert into audit_logs(user_id,username,action,entity_type,details_json,created_at) values(?,?,?,?,?,?)',(u['id'],u['display_name'],'create_request','request',json.dumps({'request_no':rn},ensure_ascii=False),now()));c.commit();c.close();self.redir('/requests')
 def transfer_form(self,u,lang,wid):
  c=db();w=c.execute('select * from workers where id=?',(wid,)).fetchone();c.close();body=f'<div class="panel"><h3>نقل العامل: {e(w["full_name"])}</h3><p>الغرفة الحالية: {e(w["room_no"])}</p><form method="post" class="grid"><select name="zone"><option>1</option><option>2</option><option>3</option><option>4</option></select><input name="room_no" placeholder="الغرفة الجديدة" required><textarea class="full" name="reason" placeholder="سبب النقل" required></textarea><button class="btn amber">إرسال طلب النقل</button></form></div>';self.html(layout(body,u,lang))
 def transfer_post(self,u,wid):
  f=self.form();c=db();w=c.execute('select * from workers where id=?',(wid,)).fetchone();p={'old_room':w['room_no'],'old_zone':w['zone'],'new_room':f['room_no'],'new_zone':f['zone'],'reason':f['reason']};rn=reqno('TRF');c.execute('insert into requests(request_no,request_type,worker_id,payload_json,requested_by,status,created_at) values(?,?,?,?,?,?,?)',(rn,'transfer_req',wid,json.dumps(p,ensure_ascii=False),u['id'],'pending',now()));c.commit();c.close();self.redir('/requests')
 def approvals(self,u,lang):
  c=db();rs=c.execute("select r.*,u.display_name requester from requests r join users u on u.id=r.requested_by where r.status='pending'").fetchall();arr=[]
  for r in rs:
   p=json.loads(r['payload_json']);room=p.get('room_no') or p.get('new_room') or ''
   if can_approve(u,room):arr.append(f'<tr><td>{e(r["request_no"])}</td><td>{e(r["request_type"])}</td><td>{e(r["requester"])}</td><td>{e(room)}</td><td><form method="post" action="/approvals/{r["id"]}"><input name="reason" placeholder="سبب القرار"><br><br><button name="decision" value="approve" class="btn primary">اعتماد</button> <button name="decision" value="reject" class="btn danger">رفض</button></form></td></tr>')
  c.close();self.html(layout(f'<div class="panel"><table><tr><th>رقم الطلب</th><th>النوع</th><th>المقدم</th><th>الغرفة</th><th>الإجراء</th></tr>{"".join(arr) or "<tr><td colspan=5>لا توجد طلبات</td></tr>"}</table></div>',u,lang))
 def decide(self,u,rid):
  f=self.form();c=db();r=c.execute('select * from requests where id=?',(rid,)).fetchone();p=json.loads(r['payload_json']);room=p.get('room_no') or p.get('new_room') or ''
  if not can_approve(u,room):c.close();return self.html('Forbidden',403)
  st='approved' if f.get('decision')=='approve' else 'rejected'
  if st=='approved':
   if r['request_type']=='new_housing':c.execute("insert into workers(employee_no,iqama_no,full_name,nationality,profession,zone,room_no,status,created_at,updated_at) values(?,?,?,?,?,?,?,'active',?,?)",(p['employee_no'],p.get('iqama_no',''),p['full_name'],p.get('nationality',''),p.get('profession',''),p['zone'],p['room_no'],now(),now()))
   else:c.execute('update workers set zone=?,room_no=?,updated_at=? where id=?',(p['new_zone'],p['new_room'],now(),r['worker_id']))
  c.execute('update requests set status=?,approver_id=?,decision_reason=?,decided_at=? where id=?',(st,u['id'],f.get('reason',''),now(),rid));c.execute('insert into audit_logs(user_id,username,action,entity_type,entity_id,details_json,created_at) values(?,?,?,?,?,?,?)',(u['id'],u['display_name'],st+'_request','request',rid,json.dumps({'request_no':r['request_no']},ensure_ascii=False),now()));c.commit();c.close();self.redir('/approvals')
 def users(self,u,lang):
  if u['role'] not in ('services_manager','housing_manager'):return self.html('Forbidden',403)
  c=db();rs=c.execute('select u.*,a.room_text,a.bathrooms_group from users u left join assignments a on a.user_id=u.id order by u.role,u.display_name').fetchall();c.close();rows=''.join(f'<tr><td>{e(r["employee_no"])}</td><td>{e(r["display_name"])}</td><td>{e(r["username"])}</td><td>{e(r["role"])}</td><td>{e((r["room_text"] or "")+" "+(r["bathrooms_group"] or ""))}</td></tr>' for r in rs);self.html(layout(f'<div class="panel"><table><tr><th>الرقم الوظيفي</th><th>الاسم</th><th>اسم المستخدم</th><th>الدور</th><th>النطاق</th></tr>{rows}</table></div>',u,lang))
 def audit(self,u,lang):
  if u['role'] not in ('services_manager','housing_manager','housing_supervisor'):return self.html('Forbidden',403)
  c=db();rs=c.execute('select * from audit_logs order by id desc limit 1000').fetchall();c.close();rows=''.join(f'<tr><td>{e(r["created_at"])}</td><td>{e(r["username"])}</td><td>{e(r["action"])}</td><td>{e(r["entity_type"])}</td><td>{e(r["details_json"] or "")}</td></tr>' for r in rs);self.html(layout(f'<div class="panel"><table><tr><th>التاريخ</th><th>المستخدم</th><th>العملية</th><th>النوع</th><th>التفاصيل</th></tr>{rows}</table></div>',u,lang))
if __name__=='__main__':ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT','8080'))),App).serve_forever()
