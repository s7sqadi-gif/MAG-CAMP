import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("SECRET_KEY", "test-secret")

import app as magcamp


class MagCampPhase1Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.test_db = Path(self.tempdir.name) / "mhoms-test.db"
        shutil.copy2(ROOT / "data" / "mhoms.db", self.test_db)
        magcamp.DB = str(self.test_db)
        magcamp.app.config.update(TESTING=True)
        self.client = magcamp.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def login(self, employee_no, password="123456"):
        return self.client.post(
            "/login",
            data={"employee_no": employee_no, "password": password},
            follow_redirects=False,
        )

    def authenticate_without_forced_change(self, employee_no):
        c = magcamp.conn()
        try:
            user = c.execute("SELECT id FROM users WHERE employee_no=?", (employee_no,)).fetchone()
            c.execute("UPDATE users SET must_change_password=0 WHERE id=?", (user["id"],))
            c.commit()
        finally:
            c.close()
        with self.client.session_transaction() as session:
            session["uid"] = user["id"]

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")
        self.assertEqual(response.json["database"], "ok")
        self.assertEqual(response.json["phase"], 4)

    def test_counts_and_active_roles(self):
        c = magcamp.conn()
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM workers WHERE archived=0").fetchone()[0], 4372)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 974)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM users WHERE active=1 AND role='housing_supervisor'").fetchone()[0], 11)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM users WHERE active=1 AND role='housing_monitor'").fetchone()[0], 4)
        finally:
            c.close()

    def test_manager_login_forces_password_change(self):
        response = self.login("109753")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/change-password"))

    def test_change_password_flow(self):
        self.login("109753")
        response = self.client.post(
            "/change-password",
            data={
                "current_password": "123456",
                "new_password": "NewPass789",
                "confirm_password": "NewPass789",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("تم تغيير كلمة المرور بنجاح".encode("utf-8"), response.data)
        self.client.get("/logout")
        response = self.login("109753", "NewPass789")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/"))

    def test_manager_users_access(self):
        self.authenticate_without_forced_change("109753")
        self.assertEqual(self.client.get("/users").status_code, 200)

    def test_supervisor_cannot_access_users(self):
        self.authenticate_without_forced_change("149867")
        self.assertEqual(self.client.get("/users").status_code, 403)

    def test_abdulrahman_explicit_ranges(self):
        self.authenticate_without_forced_change("100627")
        for room in ("1101", "1148", "4913", "4924"):
            response = self.client.get(f"/rooms?q={room}")
            self.assertIn(f">{room}<".encode(), response.data)
        for room in ("1149", "4912"):
            response = self.client.get(f"/rooms?q={room}")
            self.assertNotIn(f">{room}<".encode(), response.data)

    def test_all_supervisor_rooms_have_no_overlap(self):
        c = magcamp.conn()
        try:
            rooms = [r[0] for r in c.execute("SELECT CAST(room_no AS INTEGER) FROM rooms")]
            ranges = c.execute("SELECT user_id,room_start,room_end FROM assignments").fetchall()
        finally:
            c.close()
        for room in rooms:
            owners = {r[0] for r in ranges if min(r[1], r[2]) <= room <= max(r[1], r[2])}
            self.assertLessEqual(len(owners), 1, f"room {room} assigned to multiple supervisors")

    def test_phase3_room_schema(self):
        c = magcamp.conn()
        try:
            cols = {row[1] for row in c.execute("PRAGMA table_info(rooms)")}
            self.assertTrue({"usage_type", "length_m", "width_m", "area_m2", "status"}.issubset(cols))
            self.assertEqual(c.execute("SELECT COUNT(*) FROM rooms WHERE usage_type!='residential'").fetchone()[0], 14)
        finally:
            c.close()

    def test_phase3_dashboard(self):
        self.authenticate_without_forced_change("109753")
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("لوحة التحكم التنفيذية".encode("utf-8"), response.data)

    def test_phase3_room_detail(self):
        self.authenticate_without_forced_change("109753")
        response = self.client.get("/rooms/1101")
        self.assertEqual(response.status_code, 200)
        self.assertIn("الغرفة 1101".encode("utf-8"), response.data)

    def test_amir_suhail_account_active(self):
        c = magcamp.conn()
        try:
            u = c.execute("SELECT * FROM users WHERE employee_no='96880'").fetchone()
            self.assertEqual(u["display_name"], "أمير سهيل")
            self.assertEqual(u["active"], 1)
            self.assertEqual(u["role"], "housing_monitor")
        finally:
            c.close()

    def test_supervisor_cannot_access_audit_logs(self):
        self.authenticate_without_forced_change("149867")
        self.assertEqual(self.client.get("/audit-logs").status_code, 403)

    def test_bathroom_report_creates_ticket(self):
        self.authenticate_without_forced_change("149867")
        response = self.client.post("/bathroom-reports/new", data={
            "bathroom_no": "B-101", "zone_name": "1", "issue_type": "تسريب",
            "priority": "urgent", "description": "تسريب اختبار"
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        c = magcamp.conn()
        try:
            report = c.execute("SELECT * FROM bathroom_reports WHERE bathroom_no='B-101'").fetchone()
            self.assertIsNotNone(report)
            ticket = c.execute("SELECT * FROM maintenance_tickets WHERE id=?", (report["maintenance_ticket_id"],)).fetchone()
            self.assertEqual(ticket["location_type"], "bathroom")
        finally:
            c.close()

    def test_supervisor_add_worker_requires_manager_approval(self):
        self.authenticate_without_forced_change("149867")
        response = self.client.post("/worker-change-requests/new", data={
            "change_type": "add", "employee_no": "P4TEST001", "iqama_no": "2999999999",
            "full_name": "PHASE FOUR TEST", "nationality": "Test", "profession": "Tester",
            "room_no": "2413", "reason": "اختبار"
        }, follow_redirects=False)
        self.assertIn(response.status_code, (302, 200))
        c = magcamp.conn()
        try:
            self.assertIsNone(c.execute("SELECT * FROM workers WHERE employee_no='P4TEST001' AND archived=0").fetchone())
            q = c.execute("SELECT * FROM worker_change_requests WHERE employee_no='P4TEST001'").fetchone()
            self.assertIsNotNone(q)
            self.assertEqual(q["status"], "pending")
        finally:
            c.close()

    def test_manager_approves_add_worker(self):
        self.authenticate_without_forced_change("149867")
        self.client.post("/worker-change-requests/new", data={
            "change_type": "add", "employee_no": "P4APPROVE1", "iqama_no": "2888888888",
            "full_name": "APPROVAL TEST", "nationality": "Test", "profession": "Tester",
            "room_no": "2504", "reason": "اختبار اعتماد"
        })
        c = magcamp.conn()
        try:
            qid = c.execute("SELECT id FROM worker_change_requests WHERE employee_no='P4APPROVE1'").fetchone()[0]
        finally:
            c.close()
        self.client.get("/logout")
        self.authenticate_without_forced_change("109753")
        response = self.client.post(f"/worker-change-requests/{qid}", data={
            "decision": "approved", "decision_reason": "معتمد"
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        c = magcamp.conn()
        try:
            worker = c.execute("SELECT * FROM workers WHERE employee_no='P4APPROVE1' AND archived=0").fetchone()
            request_row = c.execute("SELECT status FROM worker_change_requests WHERE id=?", (qid,)).fetchone()
            self.assertIsNotNone(worker)
            self.assertEqual(request_row["status"], "approved")
        finally:
            c.close()

    def test_phase41_supervisor_weekly_round_shows_assigned_rooms_and_names(self):
        self.authenticate_without_forced_change("149867")
        c = magcamp.conn()
        try:
            rng = c.execute("SELECT room_start,room_end FROM assignments a JOIN users u ON u.id=a.user_id WHERE u.employee_no='149867' ORDER BY a.id LIMIT 1").fetchone()
            worker = c.execute("SELECT room_no,full_name FROM workers WHERE archived=0 AND CAST(room_no AS INTEGER) BETWEEN ? AND ? ORDER BY CAST(room_no AS INTEGER) LIMIT 1", (min(rng[0],rng[1]),max(rng[0],rng[1]))).fetchone()
        finally:
            c.close()
        self.assertIsNotNone(worker)
        response = self.client.get("/inspections")
        self.assertEqual(response.status_code, 200)
        self.assertIn(str(worker["room_no"]).encode(), response.data)
        self.assertIn(worker["full_name"].encode("utf-8"), response.data)
        self.assertNotIn(b">1101<", response.data)

    def test_phase41_inspection_can_create_linked_maintenance_ticket(self):
        self.authenticate_without_forced_change("149867")
        c = magcamp.conn()
        try:
            rng = c.execute("SELECT room_start,room_end FROM assignments a JOIN users u ON u.id=a.user_id WHERE u.employee_no='149867' ORDER BY a.id LIMIT 1").fetchone()
            room = c.execute("SELECT room_no FROM rooms WHERE CAST(room_no AS INTEGER) BETWEEN ? AND ? AND usage_type='residential' ORDER BY CAST(room_no AS INTEGER) LIMIT 1", (min(rng[0],rng[1]),max(rng[0],rng[1]))).fetchone()[0]
        finally:
            c.close()
        response = self.client.post("/inspections/new", data={
            "room_no": room, "actual_count": "4", "cleanliness": "جيد",
            "notes": "جولة اختبار 4.1", "maintenance_required": "1",
            "maintenance_category": "تكييف", "maintenance_priority": "urgent",
            "maintenance_description": "عطل تكييف من الجولة"
        }, follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        c = magcamp.conn()
        try:
            inspection = c.execute("SELECT * FROM inspections WHERE location_id=? AND notes='جولة اختبار 4.1' ORDER BY id DESC LIMIT 1", (room,)).fetchone()
            self.assertIsNotNone(inspection)
            self.assertIsNotNone(inspection["maintenance_ticket_id"])
            ticket = c.execute("SELECT * FROM maintenance_tickets WHERE id=?", (inspection["maintenance_ticket_id"],)).fetchone()
            self.assertEqual(ticket["location_type"], "room")
            self.assertEqual(ticket["location_id"], room)
            self.assertEqual(ticket["category"], "تكييف")
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
