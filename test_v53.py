"""LabHub v5.3 integration tests (run on Windows in .venv).
Usage: .\\.venv\\Scripts\\python.exe -m unittest test_v53 -v
"""
from __future__ import annotations
import unittest
from datetime import datetime,timedelta,timezone
from test_v5 import V5,A,B,C
from schedule_suggestions import generate_suggestions,extract_visits
import server

JST=timezone(timedelta(hours=9))
DAY=86400_000

class SchedulingV53(V5):
    # Inherit v5 smoke checks and also exercise v5.3 behavior.
    def test_resource_categories_and_legacy_data(self):
        gpu=self.create_equipment()
        overview=self.call('GET','/api/v5/overview',A).json
        self.assertEqual(overview['equipment'][0]['kind'],'equipment')
        room=self.call('POST','/api/v5/equipment','admin',{'name':'会議室A','kind':'room'}).json['id']
        now=self.future(60)
        payload={'title':'進捗相談','deadline_ms':now,'equipment_id':gpu,
                 'slots':[{'starts_ms':self.future(120),'ends_ms':self.future(180)},
                          {'starts_ms':self.future(240),'ends_ms':self.future(300)}]}
        result=self.call('POST','/api/v5/polls',A,payload)
        self.assertEqual(result.status_code,400,result.json)
        payload['equipment_id']=room
        self.assertEqual(self.call('POST','/api/v5/polls',A,payload).status_code,201)
        # GPU can still be booked normally.
        self.assertEqual(self.call('POST','/api/v5/reservations',A,{
            'equipment_id':gpu,'starts_ms':self.future(120),'ends_ms':self.future(180)}).status_code,201)
    def test_recommendation_privacy_consent_and_conflicts(self):
        room=self.call('POST','/api/v5/equipment','admin',{'name':'会議室B','kind':'room'}).json['id']
        self.assertEqual(self.call('GET','/api/v5/schedule-suggestions').status_code,403)
        self.assertEqual(self.call('GET','/api/v5/schedule-suggestions',C).status_code,403)
        self.assertFalse(self.call('GET','/api/v5/overview',A).json['preferences']['presence_recommend_opt_in'])
        self.assertEqual(self.call('PUT','/api/v5/me/presence-recommend-opt-in',A,{'allow':True}).status_code,200)
        self.assertTrue(self.call('GET','/api/v5/overview',A).json['preferences']['presence_recommend_opt_in'])
        self.assertFalse(self.call('GET','/api/v5/overview',B).json['preferences']['presence_recommend_opt_in'])
        day=(datetime.fromtimestamp(self.now/1000,JST)+timedelta(days=2)).strftime('%Y-%m-%d')
        r=self.call('GET',f'/api/v5/schedule-suggestions?first_day={day}&duration=60&room_id={room}',A)
        self.assertEqual(r.status_code,200,r.json)
        self.assertEqual(r.json['basis'],'free_time_only')
        self.assertGreaterEqual(len(r.json['slots']),2)
        candidate=r.json['slots'][0]
        self.call('POST','/api/v5/reservations',A,{
            'equipment_id':room,'starts_ms':candidate['starts_ms'],
            'ends_ms':candidate['ends_ms']})
        new=self.call('GET',f'/api/v5/schedule-suggestions?first_day={day}&duration=60&room_id={room}',A).json
        self.assertNotIn(candidate['starts_ms'],[s['starts_ms'] for s in new['slots']])
        # Cannot inspect other users' history or recommend with unconsented data.
        self.assertEqual(self.call('PUT','/api/v5/me/presence-recommend-opt-in',B,{'allow':'yes'}).status_code,400)
        self.assertEqual(self.call('PUT','/api/v5/me/presence-recommend-opt-in',A,{'allow':False}).status_code,200)
    def test_history_recommendations_require_confirmed_visits(self):
        today=datetime.fromtimestamp(self.now/1000,JST).replace(hour=0,minute=0,second=0,microsecond=0)
        with server.connect_db() as db:
            for uid in (A,B):db.execute('UPDATE members SET presence_recommend_opt_in=1 WHERE user_id=?',(uid,))
            for d in (7,14,21,28):
                day=today-timedelta(days=d)
                for uid,hr in ((A,9),(B,9)):
                    enter=int((day+timedelta(hours=hr,minutes=30)).timestamp()*1000)
                    leave=int((day+timedelta(hours=hr+3)).timestamp()*1000)
                    db.execute('''INSERT INTO app_presence_events(event_id,user_id,event_type,event_ms,received_ms,region,applied)
                    VALUES (?,?,?,?,?,?,1)''',(f'{uid}{d}in',uid,'enter',enter,enter,'labhub.test'))
                    db.execute('''INSERT INTO app_presence_events(event_id,user_id,event_type,event_ms,received_ms,region,applied)
                    VALUES (?,?,?,?,?,?,1)''',(f'{uid}{d}out',uid,'exit',leave,leave,'labhub.test'))
            visits=extract_visits(db,self.now)
            self.assertEqual(len(visits),8)
            result=generate_suggestions(db,self.now,first_day=(today+timedelta(days=1)).strftime('%Y-%m-%d'),duration=60,room_id=None)
            self.assertEqual(result['basis'],'historical')
            self.assertTrue(all(x['trend_score'] is not None for x in result['slots']))
            db.execute('UPDATE members SET presence_recommend_opt_in=0 WHERE user_id=?',(B,))
            no_longer=generate_suggestions(db,self.now,first_day=(today+timedelta(days=1)).strftime('%Y-%m-%d'),duration=60,room_id=None)
            self.assertEqual(no_longer['basis'],'free_time_only')

if __name__=='__main__':unittest.main(verbosity=2)
