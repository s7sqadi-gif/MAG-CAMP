import re
import app as mag


def client_as(uid):
    c = mag.app.test_client()
    with mag.conn() as db:
        db.execute('UPDATE users SET must_change_password=0 WHERE id=?', (uid,))
        db.commit()
    with c.session_transaction() as s:
        s['uid'] = uid
        s['lang'] = 'ar'
    return c


def test_manager_workers_and_search_no_500():
    c = client_as(2)
    assert c.get('/workers').status_code == 200
    assert c.get('/search?q=237').status_code == 200


def test_worker_detail_route_exists():
    c = client_as(2)
    with mag.conn() as db:
        wid = db.execute('SELECT id FROM workers WHERE COALESCE(archived,0)=0 LIMIT 1').fetchone()[0]
    r = c.get(f'/workers/{wid}')
    assert r.status_code == 200
    assert 'تفاصيل العامل' in r.get_data(as_text=True)


def test_rooms_and_absence_pages_no_500():
    c = client_as(2)
    assert c.get('/rooms').status_code == 200
    assert c.get('/absence-reports').status_code == 200


def test_supervisor_sees_full_zone_bathroom_assets():
    c = client_as(4)
    html = c.get('/').get_data(as_text=True)
    toilets = re.search(r'دورات المياه المكلف بها</div><div class="num">(\d+)', html)
    basins = re.search(r'المغاسل المكلف بها</div><div class="num">(\d+)', html)
    assert toilets and int(toilets.group(1)) == 156
    assert basins and int(basins.group(1)) == 80
    assert c.get('/sector').status_code == 200
