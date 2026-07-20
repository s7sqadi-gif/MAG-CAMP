import importlib, os, shutil, sqlite3, tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
TMP=tempfile.mkdtemp(prefix='mag_phase6_test_')
DB=os.path.join(TMP,'test.db')
shutil.copy2(ROOT/'data'/'mhoms.db',DB)
os.environ['DATABASE_PATH']=DB
appmod=importlib.import_module('app')
appmod.app.config.update(TESTING=True)

def user(role):
 c=sqlite3.connect(DB);c.row_factory=sqlite3.Row
 r=c.execute('select * from users where role=? and active=1 limit 1',(role,)).fetchone()
 c.execute('update users set must_change_password=0 where id=?',(r['id'],));c.commit();c.close();return r

def login(client,u):
 with client.session_transaction() as s:s['uid']=u['id'];s['lang']='ar'

def test_manager_reports_and_exports():
 cl=appmod.app.test_client();login(cl,user('housing_manager'))
 assert cl.get('/workers').status_code==200
 assert cl.get('/reports').status_code==200
 x=cl.get('/exports/workers.xlsx');assert x.status_code==200 and x.data[:2]==b'PK'
 p=cl.get('/exports/workers.pdf');assert p.status_code==200 and p.data[:4]==b'%PDF'
 assert cl.get('/backup').status_code==200

def test_export_permissions():
 cl=appmod.app.test_client();login(cl,user('housing_supervisor'))
 assert cl.get('/exports/workers.xlsx').status_code==403
 assert cl.get('/exports/workers.pdf').status_code==403

def test_absence_lookup_form():
 cl=appmod.app.test_client();login(cl,user('housing_supervisor'))
 assert cl.get('/absence-reports/new').status_code==200
 assert cl.get('/absence-reports').status_code==200
