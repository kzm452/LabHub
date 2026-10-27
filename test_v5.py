"""Run: python -m unittest test_v5.py"""
import os
import sqlite3
import tempfile
import atexit
import shutil
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ['LINE_CHANNEL_SECRET']='dummy'
os.environ['LINE_CHANNEL_ACCESS_TOKEN']='dummy'
os.environ['LABHUB_ADMIN_KEY']='test-admin-key'
_test_root=tempfile.mkdtemp(prefix='labhub-v5-tests-')
os.environ['LABHUB_DB_PATH']=str(Path(_test_root)/'initial.db')
atexit.register(lambda:shutil.rmtree(_test_root,ignore_errors=True))
import server
from labhub_features import init_feature_db, send_due_reservation_reminders

A='U11111111111111111111111111111111'
B='U22222222222222222222222222222222'
C='U33333333333333333333333333333333'
T='a'*42

class V5(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old_path=server.DB_PATH
        self.old_log=server.LOG_PATH
        server.LOG_PATH=Path(self.temp.name)/"test-beacon.jsonl"
        server.DB_PATH=Path(self.temp.name)/'test.db'
        server.init_db();init_feature_db(server.connect_db)
        self.now=int(time.time()*1000)
        with server.connect_db() as db:
            for uid,name,ap in [(A,'研究者A',1),(B,'研究者B',1),(C,'未承認',0)]:
                db.execute('''INSERT INTO members(user_id,display_name,status,activity,updated_ms,source,approved)
                              VALUES(?,?,'away','未設定',?,'line_login',?)''',(uid,name,self.now,ap))
        self.client=server.app.test_client()
        self.old_verified=server.verified_line_user
        def verified():
            from flask import request
            val=request.headers.get('Authorization','')
            if val.startswith('Bearer U'):
                return {'id':val[7:],'name':'テスト'}
            return None
        server.verified_line_user=verified
        self.old_tokens=server.APP_TOKENS
        server.APP_TOKENS={T:A}
        self.messages=[]
        self.old_push=server.push_line_text
        server.push_line_text=lambda uid,msg:self.messages.append((uid,msg)) or True
        self.old_appnotify=server.push_app_presence
        server.push_app_presence=lambda *args: None
    def tearDown(self):
        server.DB_PATH=self.old_path
        server.LOG_PATH=self.old_log
        server.verified_line_user=self.old_verified
        server.APP_TOKENS=self.old_tokens
        server.push_line_text=self.old_push
        server.push_app_presence=self.old_appnotify
        self.temp.cleanup()
    def call(self,method,path,identity=None,json=None):
        headers={}
        if identity=='admin':headers['X-LabHub-Key']='test-admin-key'
        elif identity:headers['Authorization']='Bearer '+identity
        return self.client.open(path,method=method,headers=headers,json=json)
    def future(self,minutes):return self.now+minutes*60_000
    def create_equipment(self, kind="equipment"):
        r=self.call('POST','/api/v5/equipment','admin',{'name':'会議室A' if kind=="room" else 'GPU-01','kind':kind,'location':'実験室'})
        self.assertEqual(r.status_code,201,r.json)
        return r.json['id']
    def test_access_gate_and_equipment(self):
        self.assertEqual(self.call('GET','/api/v5/overview').status_code,403)
        self.assertEqual(self.call('GET','/api/v5/overview',C).status_code,403)
        self.assertEqual(self.call('POST','/api/v5/equipment',A,{'name':'no'}).status_code,403)
        self.create_equipment()
        self.assertEqual(len(self.call('GET','/api/v5/overview',A).json['equipment']),1)
    def test_booking_conflict_cancel_history(self):
        eid=self.create_equipment()
        payload={'equipment_id':eid,'starts_ms':self.future(60),'ends_ms':self.future(120),'purpose':'GPU測定'}
        one=self.call('POST','/api/v5/reservations',A,payload)
        self.assertEqual(one.status_code,201,one.json)
        self.assertEqual(self.call('POST','/api/v5/reservations',B,payload).status_code,409)
        self.assertEqual(self.call('POST',f'/api/v5/reservations/{one.json["id"]}/cancel',B,{}).status_code,403)
        self.assertEqual(self.call('POST',f'/api/v5/reservations/{one.json["id"]}/cancel',A,{}).status_code,200)
        self.assertEqual(self.call('POST','/api/v5/reservations',B,payload).status_code,201)
        self.assertEqual(self.call('POST','/api/v5/reservations',C,payload).status_code,403)
    def test_meeting_votes_and_finalization_books_room(self):
        eid=self.create_equipment(kind='room')
        body={'title':'輪講','deadline_ms':self.future(30),'equipment_id':eid,
              'slots':[{'starts_ms':self.future(120),'ends_ms':self.future(180)},
                       {'starts_ms':self.future(240),'ends_ms':self.future(300)}]}
        r=self.call('POST','/api/v5/polls',A,body)
        self.assertEqual(r.status_code,201,r.json)
        pid=r.json['id']
        slots=[s for s in self.call('GET','/api/v5/overview',A).json['slots'] if s['poll_id']==pid]
        sid=slots[0]['id']
        self.assertEqual(self.call('POST',f'/api/v5/polls/{pid}/vote',B,{'available_slot_ids':[sid]}).status_code,200)
        self.assertEqual(self.call('POST',f'/api/v5/polls/{pid}/finalize',B,{'slot_id':sid}).status_code,403)
        done=self.call('POST',f'/api/v5/polls/{pid}/finalize',A,{'slot_id':sid})
        self.assertEqual(done.status_code,200,done.json)
        self.assertIsNotNone(done.json['reservation_id'])
        self.assertEqual(self.call('POST','/api/v5/reservations',B,{'equipment_id':eid,
                'starts_ms':self.future(130),'ends_ms':self.future(160)}).status_code,409)
        today=self.call('GET','/api/v5/overview',A).json
        self.assertEqual([p for p in today['polls'] if p['id']==pid][0]['status'],'closed')
    def test_arrival_opt_in_and_auto_entry_notification(self):
        self.assertEqual(self.call('PUT',f'/api/v5/me/watches/{A}',B,{'enabled':True}).status_code,403)
        self.assertEqual(self.call('PUT','/api/v5/me/arrival-sharing',A,{'allow':True}).status_code,200)
        self.assertEqual(self.call('PUT',f'/api/v5/me/watches/{A}',B,{'enabled':True}).status_code,200)
        event={'id':'abc-12345-ABCDE','event':'enter','ts':self.now,'region':server.APP_REGION}
        r=self.client.post('/api/app/presence',json=event,headers={'Authorization':'Bearer '+T})
        self.assertEqual(r.status_code,200,r.json)
        self.assertTrue(r.json['applied'])
        self.assertEqual(len(self.messages),1)
        self.assertEqual(self.messages[0][0],B)
        self.assertEqual(self.client.post('/api/app/presence',json=event,headers={'Authorization':'Bearer '+T}).json['duplicate'],True)
        self.assertEqual(len(self.messages),1)
        self.assertEqual(self.call('PUT','/api/v5/me/arrival-sharing',A,{'allow':False}).status_code,200)
        self.assertEqual(self.call('GET','/api/v5/overview',B).json['watches'],[])
    def test_reminders_dedup(self):
        eid=self.create_equipment()
        self.call('POST','/api/v5/reservations',A,{'equipment_id':eid,'starts_ms':self.future(8),'ends_ms':self.future(70)})
        called=[]
        def push(uid,msg):called.append(uid);return True
        self.assertEqual(send_due_reservation_reminders(server.connect_db,server.now_ms,push),1)
        self.assertEqual(send_due_reservation_reminders(server.connect_db,server.now_ms,push),0)
        self.assertEqual(called,[A])
    def test_line_beacon_arrival_triggers_watched_notification(self):
        self.call('PUT','/api/v5/me/arrival-sharing',B,{'allow':True})
        self.call('PUT',f'/api/v5/me/watches/{B}',A,{'enabled':True})
        with patch.object(server, 'get_display_name', lambda x: '研究者B'):
            server.handle_beacon({'timestamp':self.now,'source':{'userId':B},'beacon':{'type':'enter','hwid':'019192228d'}},self.now)
        self.assertEqual(len(self.messages),1)
        self.assertEqual(self.messages[0][0],A)
    def test_input_validation(self):
        eq=self.create_equipment()
        self.assertEqual(self.call('POST','/api/v5/reservations',A,{'equipment_id':eq,'starts_ms':self.future(120),'ends_ms':self.future(60)}).status_code,400)
        self.assertEqual(self.call('POST','/api/v5/polls',A,{'title':'悪い値','slots':[],'deadline_ms':self.future(60)}).status_code,400)
        self.assertEqual(self.call('POST','/api/v5/events',A,{'title':'other'}).status_code,403)

if __name__=='__main__':unittest.main(verbosity=2)
