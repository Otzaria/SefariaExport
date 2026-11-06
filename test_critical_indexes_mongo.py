"""Optional real-server regression tests.

Install pymongo and set MONGOD_BINARY to run these against a disposable local
server. No existing MongoDB instance is used or changed.
"""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from create_critical_indexes import verify_links_query_plan


@unittest.skipUnless(os.environ.get("MONGOD_BINARY"), "set MONGOD_BINARY for real MongoDB tests")
class RealMongoLinksPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pymongo

        binary = shutil.which(os.environ["MONGOD_BINARY"])
        if not binary:
            raise RuntimeError("MONGOD_BINARY does not name an executable")
        directory = tempfile.TemporaryDirectory(prefix="links-plan-test-")
        cls.addClassCleanup(directory.cleanup)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        log_path = Path(directory.name) / "mongod.log"
        process = subprocess.Popen([
            binary, "--dbpath", directory.name, "--bind_ip", "127.0.0.1",
            "--port", str(port), "--logpath", str(log_path),
            "--setParameter", "enableTestCommands=1",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

        def stop_server():
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        cls.addClassCleanup(stop_server)
        cls.client = pymongo.MongoClient(
            "127.0.0.1", port, serverSelectionTimeoutMS=250, socketTimeoutMS=5000,
        )
        cls.addClassCleanup(cls.client.close)
        deadline = time.monotonic() + 15
        while True:
            try:
                cls.client.admin.command("ping")
                break
            except pymongo.errors.ConnectionFailure:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError(log_path.read_text() if log_path.exists() else "mongod failed")
        cls.db = cls.client.links_plan_test

    def setUp(self):
        self.db.links.drop()
        self.db.links.insert_many([
            {"refs": [f"Book {i}:1", f"Other {i}:2"]} for i in range(5000)
        ])

    def test_server_enforces_production_explain_deadline(self):
        from pymongo.errors import ExecutionTimeout

        self.db.links.create_index("refs.0")
        self.client.admin.command({"configureFailPoint": "maxTimeAlwaysTimeOut", "mode": "alwaysOn"})
        try:
            with self.assertRaises(ExecutionTimeout):
                verify_links_query_plan(self.db)
        finally:
            self.client.admin.command({"configureFailPoint": "maxTimeAlwaysTimeOut", "mode": "off"})

    def test_actual_plans_reject_sort_and_accept_usable_indexes(self):
        with self.assertRaisesRegex(RuntimeError, "blocking SORT"):
            verify_links_query_plan(self.db)
        for keys in ([('refs.0', 1)], [('refs.0', -1)], [('refs.0', 1), ('type', 1)]):
            with self.subTest(keys=keys):
                self.db.links.drop_indexes()
                name = self.db.links.create_index(keys)
                self.assertEqual(verify_links_query_plan(self.db)["indexes"], [name])


if __name__ == "__main__":
    unittest.main()
