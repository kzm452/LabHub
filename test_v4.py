"""Run: python -m unittest -v test_v4.py. Uses Flask if installed, a routing stub otherwise."""
import importlib
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LINE_CHANNEL_SECRET", "test-secret")
os.environ.setdefault("LINE_CHANNEL_ACCESS_TOKEN", "test-access")
os.environ["LABHUB_APP_TOKENS"] = "t" * 40 + ":Utestmember"
os.environ["LABHUB_NOTIFY_APP_EVENTS"] = "1"
os.environ["LABHUB_ADMIN_KEY"] = "test-admin-only"

# This container lacks Flask and has no network access. When Flask is absent,
# simulate only its request/response routing surface to unit-test DB logic.
try:
    import flask  # noqa: F401
except ImportError:
    import logging
    import sys
    import types
    stub = types.ModuleType("flask")

    class FakeResponse:
        def __init__(self, payload, status_code=200):
            self.json, self.status_code = payload, status_code

    class FakeRequest:
        headers = {}
        payload = {}
        path = ""
        def get_json(self, silent=True):
            return self.payload

    stub.request = FakeRequest()
    stub.jsonify = lambda **kwargs: FakeResponse(kwargs)
    stub.abort = lambda code, *args: (_ for _ in ()).throw(RuntimeError(code))
    stub.render_template = lambda *args, **kwargs: None

    class FakeFlask:
        def __init__(self, name):
            self.routes = {}
            self.logger = logging.getLogger("test-flask")
        def post(self, path):
            def decorator(fn):
                self.routes[path] = fn
                return fn
            return decorator
        get = post
        def after_request(self, fn):
            return fn
        def test_client(self):
            app = self
            class Client:
                def post(self, path, headers=None, json=None):
                    stub.request.headers = headers or {}
                    stub.request.payload = json
                    stub.request.path = path
                    result = app.routes[path]()
                    if isinstance(result, tuple):
                        response, code = result
                        response.status_code = code
                        return response
                    return result
            return Client()
    stub.Flask = FakeFlask
    sys.modules["flask"] = stub

import server  # noqa: E402


class PresenceTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.old_db, self.old_log = server.DB_PATH, server.LOG_PATH
        server.DB_PATH = Path(self.folder.name) / "labhub.db"
        server.LOG_PATH = Path(self.folder.name) / "events.jsonl"
        self.addCleanup(lambda: setattr(server, "DB_PATH", self.old_db))
        self.addCleanup(lambda: setattr(server, "LOG_PATH", self.old_log))
        server.init_db()
        self.client = server.app.test_client()
        self.push_patcher = patch.object(server.requests, "post")
        self.fake_push = self.push_patcher.start()
        self.fake_push.return_value.ok = True
        self.addCleanup(self.push_patcher.stop)
        self.t = server.now_ms()
        self.token = "t" * 40
        self.auth = {"Authorization": f"Bearer {self.token}"}
        with server.connect_db() as db:
            db.execute("""INSERT INTO members
                (user_id,display_name,status,activity,updated_ms,approved)
                VALUES ('Utestmember','Tester','away','未設定',?,1)""", (self.t - 360000,))
            db.commit()

    def event(self, action, ts=None, event_id=None, token=None, region="labhub.lab1.room1"):
        headers = {"Authorization": f"Bearer {token or self.token}"}
        return self.client.post("/api/app/presence", headers=headers, json={
            "id": event_id or str(uuid.uuid4()), "event": action,
            "ts": self.t if ts is None else ts, "region": region,
        })

    def row(self):
        with server.connect_db() as db:
            return dict(db.execute("SELECT * FROM members WHERE user_id = 'Utestmember'").fetchone())

    def test_auth_and_membership(self):
        self.assertEqual(self.event("enter", token="nope").status_code, 401)
        with server.connect_db() as db:
            db.execute("UPDATE members SET approved=0")
        self.assertEqual(self.event("enter").status_code, 403)
        self.assertEqual(self.row()["status"], "away")

    def test_initial_events_and_notifications(self):
        with patch.object(server.requests, "post") as push:
            push.return_value.ok = True
            self.assertFalse(self.event("test").json["applied"])
            self.assertEqual(push.call_count, 0)
            enter_id = str(uuid.uuid4())
            self.assertTrue(self.event("enter", event_id=enter_id).json["applied"])
            self.assertEqual(self.row()["status"], "present")
            self.assertEqual(self.row()["source"], "app")
            self.assertEqual(server.effective_status(self.row(), self.t + 100), "auto")
            self.assertEqual(push.call_count, 1)
            self.assertTrue(self.event("enter", event_id=enter_id).json["duplicate"])
            self.assertEqual(push.call_count, 1)
            self.assertTrue(self.event("exit", ts=self.t + 10).json["applied"])
            self.assertEqual(self.row()["status"], "away")
            self.assertEqual(push.call_count, 2)
            self.assertFalse(self.event("exit", ts=self.t + 20).json["notification_attempted"])
            self.assertEqual(push.call_count, 2)
            with server.connect_db() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM app_presence_events").fetchone()[0], 4)

    def test_old_events_do_not_undo_manual_checkout(self):
        with server.connect_db() as db:
            db.execute("""UPDATE members SET status='away',source='manual',last_presence_ms=?
                       WHERE user_id='Utestmember'""", (self.t + 30,))
        reply = self.event("enter", ts=self.t)
        self.assertFalse(reply.json["applied"])
        self.assertTrue(reply.json["stale"])
        self.assertEqual(self.row()["status"], "away")

    def test_replay_across_restart_and_reordering(self):
        enter_id = str(uuid.uuid4())
        self.assertTrue(self.event("enter", ts=self.t, event_id=enter_id).json["applied"])
        self.assertTrue(self.event("exit", ts=self.t + 15).json["applied"])
        self.assertFalse(self.event("enter", ts=self.t + 10).json["applied"])
        self.assertEqual(self.row()["status"], "away")
        server.init_db()  # simulates reopening the DB after a restart
        self.assertTrue(self.event("enter", ts=self.t, event_id=enter_id).json["duplicate"])
        self.assertEqual(self.row()["status"], "away")

    def test_app_owned_member_ignores_line_enter(self):
        self.event("exit", ts=self.t + 12)
        with patch.object(server, "get_display_name", return_value="Tester"):
            server.handle_beacon({"source": {"userId": "Utestmember"},
                "beacon": {"type": "enter", "hwid": "019192228d"},
                "timestamp": self.t + 50}, self.t + 80)
        self.assertEqual(self.row()["status"], "away")
        self.assertEqual(self.row()["last_beacon_ms"], self.t + 50)

    def test_deny_other_region_and_future_event(self):
        self.assertEqual(self.event("enter", region="wrong-room").status_code, 400)
        self.assertEqual(self.event("enter", ts=self.t + 10*60*1000).status_code, 400)
        self.assertEqual(self.row()["status"], "away")

    def test_v3_db_migration_keeps_existing_member(self):
        with server.connect_db() as db:
            db.execute("ALTER TABLE members ADD COLUMN deliberate_old_column TEXT")
            db.execute("UPDATE members SET deliberate_old_column='keep'")
        server.init_db()
        self.assertEqual(self.row()["deliberate_old_column"], "keep")
        self.assertIn("last_app_ms", self.row())

if __name__ == "__main__":
    unittest.main()
