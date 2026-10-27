"""LabHub v5.1 activity tests. Windows: python -m unittest test_v51.py -v"""
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

os.environ['LINE_CHANNEL_SECRET'] = 'unit-test-only-secret'
os.environ['LINE_CHANNEL_ACCESS_TOKEN'] = 'unit-test-only-access-token'
os.environ['LABHUB_ADMIN_KEY'] = 'test-admin-key'
_root = tempfile.TemporaryDirectory()
os.environ['LABHUB_DB_PATH'] = str(Path(_root.name) / 'import.db')

import server

A = 'U11111111111111111111111111111111'
B = 'U22222222222222222222222222222222'
C = 'U33333333333333333333333333333333'


class TestActivity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = server.DB_PATH
        self.old_auth = server.verified_line_user
        server.DB_PATH = Path(self.temp.name) / 'labhub.db'
        server.init_db()
        # The v5 feature module is retained, not replaced.
        from labhub_features import init_feature_db
        init_feature_db(server.connect_db)
        self.ms = int(time.time() * 1000)
        with server.connect_db() as db:
            for uid, name, approved, status, source in (
                (A, 'A', 1, 'present', 'app'),
                (B, 'B', 1, 'away', 'manual'),
                (C, 'C', 0, 'away', 'line_login'),
            ):
                db.execute('''INSERT INTO members
                    (user_id,display_name,status,activity,updated_ms,source,approved,last_presence_ms)
                    VALUES (?,?,?,'未設定',?,?,?,?)''',
                    (uid, name, status, self.ms, source, approved, self.ms))
        # Isolate from LINE API: simulate LINE ID-token verification, but leave
        # authorized_user() and approved status checks fully intact.
        def fake_verified():
            from flask import request
            token = request.headers.get('Authorization', '').removeprefix('Bearer ')
            return {'id': token, 'name': 'テスト'} if token.startswith('U') else None
        server.verified_line_user = fake_verified
        self.client = server.app.test_client()

    def tearDown(self):
        server.DB_PATH = self.old_db
        server.verified_line_user = self.old_auth
        self.temp.cleanup()

    def call(self, method, path, uid=A, body=None):
        headers = {'Authorization': 'Bearer ' + uid} if uid else {}
        return self.client.open(path, method=method, headers=headers, json=body)

    def test_set_activity_does_not_change_position_or_timestamps(self):
        with server.connect_db() as db:
            before = dict(db.execute('SELECT * FROM members WHERE user_id=?', (A,)).fetchone())
        r = self.call('POST', '/api/me/activity', body={'activity': '食事中', 'ttl_minutes': 60})
        self.assertEqual(r.status_code, 200, r.json)
        me = self.call('GET', '/api/presence').json['self_member']
        self.assertEqual(me['activity'], '食事中')
        self.assertEqual(me['status'], 'auto')
        with server.connect_db() as db:
            after = dict(db.execute('SELECT * FROM members WHERE user_id=?', (A,)).fetchone())
        for key in ('status', 'source', 'updated_ms', 'last_presence_ms', 'last_enter_ms'):
            self.assertEqual(before[key], after[key], key)
        # Simulate iBeacon's exit event: activity is not wiped.
        with server.connect_db() as db:
            db.execute("UPDATE members SET status='away',last_presence_ms=? WHERE user_id=?", (self.ms+100, A))
        me = self.call('GET', '/api/presence').json['self_member']
        self.assertEqual(me['status'], 'away')
        self.assertEqual(me['activity'], '食事中')

    def test_expiration_is_effective_without_scheduler(self):
        self.call('POST', '/api/me/activity', body={'activity': '授業中', 'ttl_minutes': 30})
        with server.connect_db() as db:
            db.execute('UPDATE members SET activity_expires_ms=? WHERE user_id=?', (self.ms - 1000, A))
        me = self.call('GET', '/api/presence').json['self_member']
        self.assertEqual(me['activity'], '未設定')
        self.assertIsNone(me['activity_expires_ms'])
        history = self.call('GET', '/api/me/activity/history').json['history']
        self.assertEqual(history[0]['activity'], '授業中')

    def test_only_approved_user_and_self_history(self):
        self.assertEqual(self.call('POST','/api/me/activity', C, {'activity': '外出中'}).status_code, 403)
        self.assertEqual(self.call('GET','/api/me/activity/history', C).status_code, 403)
        self.assertEqual(self.call('POST','/api/me/activity', None, {'activity': '帰宅'}).status_code, 403)
        self.assertEqual(self.call('POST','/api/me/activity', A, {'activity': '研究中'}).status_code, 200)
        self.assertEqual(self.call('GET','/api/me/activity/history', B).json['history'], [])
        with server.connect_db() as db:
            row = db.execute('SELECT activity FROM members WHERE user_id=?',(B,)).fetchone()
        self.assertEqual(row['activity'], '未設定')

    def test_invalid_payload_and_indefinite_reset(self):
        for body in ({'activity':'other'}, {'activity':'研究中', 'ttl_minutes': '60'},
                     {'activity':'研究中', 'ttl_minutes': True},
                     {'activity':'研究中', 'ttl_minutes': 20}, []):
            self.assertEqual(self.call('POST','/api/me/activity', A, body).status_code, 400, body)
        result = self.call('POST','/api/me/activity', A, {'activity':'集中中', 'ttl_minutes':0})
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.json['activity_expires_ms'])
        result = self.call('POST','/api/me/activity', A, {'activity':'未設定'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.call('GET','/api/presence').json['self_member']['activity'], '未設定')

    def test_existing_v5_db_migrates_without_data_loss(self):
        # Create a minimal *legacy* v5 members table (no new v5.1 columns).
        legacy = Path(self.temp.name) / 'old_v5.db'
        db = sqlite3.connect(legacy)
        db.execute('''CREATE TABLE members (
          user_id TEXT PRIMARY KEY,display_name TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'away',
          activity TEXT NOT NULL DEFAULT '未設定',last_enter_ms INTEGER,updated_ms INTEGER NOT NULL,
          source TEXT NOT NULL DEFAULT 'manual',approved INTEGER NOT NULL DEFAULT 0,
          last_presence_ms INTEGER,last_beacon_ms INTEGER,last_app_ms INTEGER,
          arrival_sharing INTEGER NOT NULL DEFAULT 0)''')
        db.execute("INSERT INTO members (user_id,display_name,status,activity,updated_ms,approved) VALUES (?,?,?,?,?,?)",
                   (A, '旧ユーザー', 'away', '会議中', self.ms, 1))
        db.commit(); db.close()
        server.DB_PATH = legacy
        server.init_db()
        from labhub_features import init_feature_db
        init_feature_db(server.connect_db)
        with server.connect_db() as db:
            row = db.execute('SELECT * FROM members WHERE user_id=?', (A,)).fetchone()
            self.assertEqual(row['display_name'], '旧ユーザー')
            self.assertEqual(row['activity'], '会議中')
            self.assertIsNone(row['activity_expires_ms'])
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='activity_events'").fetchone())


if __name__ == '__main__':
    unittest.main()
