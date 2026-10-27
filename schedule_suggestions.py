"""Privacy-conscious historical *group* presence-based meeting suggestions.

Only approved members who explicitly opt in are used. A completed, applied
enter->exit visit is evidence of past presence, not future availability.
LINE enter-only, stale/unapplied events and incomplete visits are excluded.
No per-user history leaves this module.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
DAY_MS = 86_400_000
MIN_VISIT_MS = 2*60_000
MAX_VISIT_MS = 12*3600_000
HISTORY_DAYS = 42


def extract_visits(db, now_ms: int):
    """Return (uid,start,end) verified past stays, bounded and consent filtered."""
    since = now_ms - HISTORY_DAYS*DAY_MS - MAX_VISIT_MS
    rows = db.execute('''
        SELECT e.user_id,e.event_type,e.event_ms FROM app_presence_events e
        JOIN members m ON m.user_id=e.user_id
        WHERE m.approved=1 AND m.presence_recommend_opt_in=1
          AND e.applied=1 AND e.event_type IN ('enter','exit')
          AND e.event_ms>=? AND e.event_ms<?
        ORDER BY e.user_id,e.event_ms,e.event_id
        ''',(since,now_ms)).fetchall()
    visits = []
    entered = {}
    for r in rows:
        uid,typ,ms=r['user_id'],r['event_type'],r['event_ms']
        if typ=='enter':
            entered[uid]=ms
        elif typ=='exit':
            start=entered.pop(uid,None)
            if start is not None and MIN_VISIT_MS <= ms-start <= MAX_VISIT_MS:
                visits.append((uid,start,ms))
    return visits


def generate_suggestions(db, now_ms: int, *, first_day: str, duration: int,
                         room_id: int|None, min_start_ms: int|None=None, limit: int=6):
    if type(duration) is not int or duration not in (30,60,90,120):
        raise ValueError('会議時間は30・60・90・120分から選んでください')
    try:
        day = datetime.strptime(first_day,'%Y-%m-%d').replace(tzinfo=JST)
    except (ValueError,TypeError):
        raise ValueError('開始日が不正です') from None
    today = datetime.fromtimestamp(now_ms/1000,JST).replace(hour=0,minute=0,second=0,microsecond=0)
    if not today <= day <= today+timedelta(days=30):
        raise ValueError('開始日は今日から30日以内にしてください')
    if room_id is not None:
        if type(room_id) is not int:raise ValueError('会議室が不正です')
        room=db.execute("SELECT 1 FROM equipment WHERE id=? AND kind='room' AND active=1",(room_id,)).fetchone()
        if not room:raise ValueError('会議室が存在しません')
    min_start=max(now_ms+10*60_000,min_start_ms or 0)
    duration_ms=duration*60_000
    end_day=day+timedelta(days=7)
    reserved=[]
    if room_id is not None:
        reserved=[(r['starts_ms'],r['ends_ms']) for r in db.execute('''
            SELECT starts_ms,ends_ms FROM reservations WHERE equipment_id=?
            AND status='active' AND starts_ms<? AND ends_ms>?
            ''',(room_id,int(end_day.timestamp()*1000),int(day.timestamp()*1000)))]
    visits=extract_visits(db,now_ms)
    consenting=db.execute('''SELECT COUNT(*) AS n FROM members WHERE approved=1 AND
                                   presence_recommend_opt_in=1''').fetchone()['n']
    visit_days={datetime.fromtimestamp(s/1000,JST).date() for _,s,_ in visits}
    # Suppress history-based rankings in tiny populations or with insufficient
    # days. Never reveal identities or individual timestamps via this API.
    enough=consenting>=2 and len({uid for uid,_,_ in visits})>=2 and len(visit_days)>=3 and len(visits)>=5
    historic=defaultdict(list)
    if enough:
        for uid,s,e in visits:
            sday=datetime.fromtimestamp(s/1000,JST).date()
            historic[(sday.weekday(),sday)].append((uid,s,e))
    ranked=[]
    for nday in range(7):
        dt=day+timedelta(days=nday)
        for minute in range(9*60,19*60-duration+1,30):
            at=dt+timedelta(minutes=minute)
            start=int(at.timestamp()*1000);end=start+duration_ms
            if start<=min_start:continue
            if any(a<end and b>start for a,b in reserved):continue
            score=0.0
            if enough:
                # We compare *time of day* and day of week, never a historical
                # calendar timestamp with a future timestamp.
                from_minutes=minute; to_minutes=minute+duration
                overlaps=[]
                for (weekday,hday),stays in historic.items():
                    if weekday!=dt.weekday():continue
                    day_start=datetime.combine(hday,datetime.min.time(),JST).timestamp()*1000
                    attended=set()
                    for uid,s,e in stays:
                        overlap=max(0,min(e,day_start+to_minutes*60_000)-max(s,day_start+from_minutes*60_000))
                        if overlap>=duration_ms*.70:attended.add(uid)
                    overlaps.append(len(attended))
                score=sum(overlaps)/len(overlaps) if overlaps else 0
            # For equal historical ranks prefer earlier days and 10/14/16 o'clock.
            friendly=min(abs(minute-600),abs(minute-840),abs(minute-960))
            rank=(score*100 if enough else 0) - (nday*.08) - friendly*.002
            ranked.append((rank,score,start,end,nday))
    ranked.sort(key=lambda r:(-r[0],r[2]))
    result=[]
    day_counts=defaultdict(int)
    for _,score,start,end,day_n in ranked:
        if day_counts[day_n]>=2:continue
        if any(abs(start-r['starts_ms'])<120*60_000 for r in result):continue
        result.append({'starts_ms':start,'ends_ms':end,
                       'trend_score':round(score,1) if enough else None})
        day_counts[day_n]+=1
        if len(result)>=limit:break
    return {'slots':result,'basis':'historical' if enough else 'free_time_only',
            'history_days':HISTORY_DAYS,
            'message':('許可したメンバーの過去の入退室から、集まりやすい傾向がある時間を参考として提案しています。参加可能かは必ず投票で確認してください。'
                      if enough else '参加履歴がまだ十分でないため、空いている日時を候補として表示しています。実際に参加できるかは投票で確認してください。')}
