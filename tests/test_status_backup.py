"""swarmctl status backup warnings (install-test defect 6): no false alarm on a new install."""
import gzip
import os
import shutil
import tempfile
import time
import unittest

from helpers import Backend

OLD = "older than 36 hours"
NONE = "no database backup yet"


class StatusBackupWarningTest(unittest.TestCase):
    def setUp(self):
        self.bdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bdir, True)
        os.environ["SWARM_DB_BACKUP_DIR"] = self.bdir
        self.addCleanup(os.environ.pop, "SWARM_DB_BACKUP_DIR", None)
        self.b = Backend()
        self.addCleanup(self.b.close)

    def warnings(self):
        return " | ".join(self.b.req("GET", "/status")["warnings"])

    def backup(self, age_hours):
        path = os.path.join(self.bdir, f"swarm-{age_hours}.db.gz")
        with gzip.open(path, "wb") as fh:
            fh.write(b"x")
        t = time.time() - age_hours * 3600
        os.utime(path, (t, t))

    def set_created(self, hours_ago):
        self.b.api.s.run("UPDATE meta SET value = ? WHERE key = 'created_at'", str(int(time.time() - hours_ago * 3600)))

    def test_fresh_install_with_no_backup_has_no_backup_warning(self):
        w = self.warnings()
        self.assertNotIn(OLD, w)
        self.assertNotIn(NONE, w)

    def test_no_backup_after_36_hours_warns(self):
        self.set_created(40)
        w = self.warnings()
        self.assertIn(NONE, w)
        self.assertNotIn(OLD, w)

    def test_old_backup_warns_even_on_a_young_database(self):
        self.backup(48)
        self.assertIn(OLD, self.warnings())

    def test_recent_backup_is_quiet(self):
        self.set_created(400)
        self.backup(2)
        w = self.warnings()
        self.assertNotIn(OLD, w)
        self.assertNotIn(NONE, w)

    def test_created_at_is_kept_across_restarts(self):
        self.set_created(400)
        before = self.b.api.s.one("SELECT value FROM meta WHERE key = 'created_at'")["value"]
        import swarm_backend as sb
        sb.Store(self.b.db)   # a restart opens the same database again
        after = self.b.api.s.one("SELECT value FROM meta WHERE key = 'created_at'")["value"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
