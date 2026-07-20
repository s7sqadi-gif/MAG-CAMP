import unittest
import app as mag

class Phase5UnifiedTests(unittest.TestCase):
    def setUp(self):
        mag.app.config.update(TESTING=True)
        self.client=mag.app.test_client()

    def login_as(self, uid):
        with mag.conn() as c:
            c.execute('UPDATE users SET must_change_password=0 WHERE id=?',(uid,)); c.commit()
        with self.client.session_transaction() as s:
            s['uid']=uid; s['lang']='ar'

    def test_map_is_retired_to_sector(self):
        self.login_as(2)
        r=self.client.get('/occupancy-map')
        self.assertEqual(r.status_code,302)
        self.assertTrue(r.headers['Location'].endswith('/sector'))

    def test_bathroom_reports_are_merged_into_maintenance(self):
        self.login_as(4)
        r=self.client.get('/bathroom-reports')
        self.assertEqual(r.status_code,302)
        self.assertIn('/tickets/new',r.headers['Location'])
        self.assertIn('location_type=bathroom',r.headers['Location'])

    def test_manager_navigation_has_no_map_or_inspections(self):
        self.login_as(2)
        r=self.client.get('/')
        self.assertNotIn('خريطة السكن'.encode(),r.data)
        self.assertNotIn('href="/inspections"'.encode(),r.data)
        self.assertIn('إدارة الطلبات'.encode(),r.data)

    def test_unified_request_types_exist(self):
        self.login_as(4)
        r=self.client.get('/worker-change-requests/new')
        for value in (b'temporary_exit',b'permanent_exit',b'final_exit'):
            self.assertIn(value,r.data)

if __name__=='__main__': unittest.main()
