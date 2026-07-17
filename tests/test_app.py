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

    def test_counts_and_active_roles(self):
        c = magcamp.conn()
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM workers").fetchone()[0], 4347)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 968)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM users WHERE active=1 AND role='housing_supervisor'").fetchone()[0], 11)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM users WHERE active=1 AND role='housing_monitor'").fetchone()[0], 3)
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


if __name__ == "__main__":
    unittest.main()
