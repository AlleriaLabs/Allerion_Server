import tempfile
import unittest

from auth_manager import AuthManager


class AuthManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.auth = AuthManager(self.temp.name)
        self.bootstrap = "test-bootstrap-secret"

    def tearDown(self):
        self.temp.cleanup()

    def test_first_admin_setup_and_password_login(self):
        self.assertTrue(self.auth.status()["setup_required"])
        with self.assertRaises(PermissionError):
            self.auth.create_first_admin("owner", "long-enough-password", "wrong", self.bootstrap)
        session = self.auth.create_first_admin(
            "owner", "long-enough-password", self.bootstrap, self.bootstrap
        )
        self.assertFalse(self.auth.status()["setup_required"])
        actor = self.auth.authenticate(session["access_token"])
        self.assertEqual(actor["username"], "owner")
        self.assertEqual(actor["role"], "admin")

    def test_passwords_are_not_stored_and_bad_login_fails(self):
        self.auth.create_first_admin("owner", "long-enough-password", self.bootstrap, self.bootstrap)
        with self.assertRaises(PermissionError):
            self.auth.login("owner", "incorrect-password")
        with self.auth._connect() as db:
            row = db.execute("SELECT password_hash FROM users WHERE username='owner'").fetchone()
        self.assertNotEqual(bytes(row["password_hash"]), b"long-enough-password")

    def test_admin_can_create_individual_accounts(self):
        session = self.auth.create_first_admin("owner", "long-enough-password", self.bootstrap, self.bootstrap)
        actor = self.auth.authenticate(session["access_token"])
        created = self.auth.create_user(actor, "viewer1", "another-long-password", "user")
        self.assertEqual(created["role"], "user")
        with self.assertRaises(PermissionError):
            self.auth.create_user({"role": "user"}, "viewer2", "another-long-password")
        self.assertEqual(len(self.auth.list_users(actor)["users"]), 2)

    def test_setup_cannot_be_repeated(self):
        self.auth.create_first_admin("owner", "long-enough-password", self.bootstrap, self.bootstrap)
        with self.assertRaises(PermissionError):
            self.auth.create_first_admin("second", "another-long-password", self.bootstrap, self.bootstrap)


if __name__ == "__main__":
    unittest.main()
