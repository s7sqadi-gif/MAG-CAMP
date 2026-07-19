import io, shutil, tempfile
from pathlib import Path
import app as mag
ROOT=Path(__file__).resolve().parents[1]

def auth(client, eno):
 c=mag.conn(); u=c.execute('select id from users where employee_no=?',(eno,)).fetchone(); c.execute('update users set must_change_password=0 where id=?',(u['id'],)); c.commit(); c.close()
 with client.session_transaction() as s: s['uid']=u['id']; s['lang']='ar'

def setup_tmp():
 td=tempfile.TemporaryDirectory(); db=Path(td.name)/'db.sqlite'; shutil.copy2(ROOT/'data/mhoms.db',db); mag.DB=str(db); mag.ensure_schema(); mag.app.config.update(TESTING=True); return td,mag.app.test_client()

def test_dashboard_has_donuts_and_no_vacancy_tables():
 td,cl=setup_tmp(); auth(cl,'109753'); r=cl.get('/'); assert r.status_code==200; assert 'الشواغر'.encode() in r.data; assert 'مواقع الشواغر'.encode() not in r.data; td.cleanup()

def test_monitor_add_requires_target_supervisor_then_manager():
 td,cl=setup_tmp(); auth(cl,'96880')
 r=cl.post('/worker-change-requests/new',data={'action_type':'add','employee_no':'NEW9001','iqama_no':'299','full_name':'NEW TEST','nationality':'Test','profession':'Worker','phone':'0500000000','target_room':'2504','reason':'new','kit_bed':'on'},follow_redirects=False); assert r.status_code==302
 c=mag.conn(); q=c.execute("select * from housing_actions where employee_no='NEW9001'").fetchone(); assert q['target_supervisor_status']=='pending' and q['final_status']=='pending_supervisors'; qid=q['id']; c.close()
 cl.get('/logout'); auth(cl,'149867'); assert cl.post(f'/worker-change-requests/{qid}',data={'decision':'approved'}).status_code==302
 c=mag.conn(); q=c.execute('select * from housing_actions where id=?',(qid,)).fetchone(); assert q['final_status']=='pending_management'; assert c.execute("select 1 from workers where employee_no='NEW9001'").fetchone() is None; c.close()
 cl.get('/logout'); auth(cl,'109753'); assert cl.post(f'/worker-change-requests/{qid}',data={'decision':'approved'}).status_code==302
 c=mag.conn(); assert c.execute("select 1 from workers where employee_no='NEW9001' and archived=0").fetchone(); c.close(); td.cleanup()

def test_transfer_requires_two_supervisors():
 td,cl=setup_tmp(); c=mag.conn(); w=c.execute("select employee_no from workers where room_no='2413' and archived=0 limit 1").fetchone(); c.close(); assert w
 auth(cl,'96880'); r=cl.post('/worker-change-requests/new',data={'action_type':'transfer','employee_no':w['employee_no'],'target_room':'3101','reason':'move'}); assert r.status_code==302
 c=mag.conn();q=c.execute('select * from housing_actions order by id desc limit 1').fetchone(); assert q['source_supervisor_id']!=q['target_supervisor_id']; assert q['source_supervisor_status']=='pending' and q['target_supervisor_status']=='pending'; qid=q['id']; c.close()
 cl.get('/logout');auth(cl,'149867');cl.post(f'/worker-change-requests/{qid}',data={'decision':'approved'})
 c=mag.conn();assert c.execute('select final_status from housing_actions where id=?',(qid,)).fetchone()[0]=='pending_supervisors';c.close()
 cl.get('/logout');auth(cl,'138430');cl.post(f'/worker-change-requests/{qid}',data={'decision':'approved'})
 c=mag.conn();assert c.execute('select final_status from housing_actions where id=?',(qid,)).fetchone()[0]=='pending_management';c.close();td.cleanup()

def test_maintenance_workflow_and_photos():
 td,cl=setup_tmp();auth(cl,'149867')
 r=cl.post('/tickets/new',data={'location_type':'room','location_id':'2413','zone_name':'2','category':'أبواب','priority':'urgent','description':'broken','photos':(io.BytesIO(b'img'),'before.jpg')},content_type='multipart/form-data'); assert r.status_code==302
 c=mag.conn();t=c.execute('select * from maintenance_tickets order by id desc limit 1').fetchone();tid=t['id'];assert t['status']=='pending_maintenance';c.close()
 cl.get('/logout');auth(cl,'91644');cl.post(f'/tickets/{tid}',data={'action':'accept'});cl.post(f'/tickets/{tid}',data={'action':'start','technician_name':'Tech'});r=cl.post(f'/tickets/{tid}',data={'action':'complete','notes':'done','after_photos':(io.BytesIO(b'img2'),'after.jpg')},content_type='multipart/form-data');assert r.status_code==302
 c=mag.conn();assert c.execute('select status from maintenance_tickets where id=?',(tid,)).fetchone()[0]=='awaiting_reporter';assert c.execute("select count(*) from ticket_photos where ticket_id=? and photo_kind='after'",(tid,)).fetchone()[0]>=1;c.close()
 cl.get('/logout');auth(cl,'149867');cl.post(f'/tickets/{tid}',data={'action':'approve_close'});c=mag.conn();assert c.execute('select status from maintenance_tickets where id=?',(tid,)).fetchone()[0]=='closed';c.close();td.cleanup()

def test_maintenance_has_no_housing_nav():
 td,cl=setup_tmp();auth(cl,'91644');r=cl.get('/tickets');assert 'التسكين الجديد'.encode() not in r.data;td.cleanup()
