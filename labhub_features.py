"""LabHub v5: equipment booking, meeting polls, opted-in arrival alerts and events.

Integrates with v4 via install_features(...). No extra pip dependencies.
"""
from __future__ import annotations

import hmac
import time
from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify, request
from schedule_suggestions import generate_suggestions

JST = timezone(timedelta(hours=9))
MAX_AHEAD_MS = 120 * 86400_000
MAX_BOOKING_MS = 14 * 86400_000
MAX_SLOTS = 12


def init_feature_db(connect_db):
    with connect_db() as db:
        cols = {r['name'] for r in db.execute('PRAGMA table_info(members)')}
        if 'arrival_sharing' not in cols:
            db.execute('ALTER TABLE members ADD COLUMN arrival_sharing INTEGER NOT NULL DEFAULT 0')
        if 'presence_recommend_opt_in' not in cols:
            db.execute('ALTER TABLE members ADD COLUMN presence_recommend_opt_in INTEGER NOT NULL DEFAULT 0')
        # Keep all existing reservations, equipment and data.
        # Existing devices remain 'equipment' unless their name clearly denotes a room.

        db.executescript('''
          CREATE TABLE IF NOT EXISTS equipment (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            location TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1, created_ms INTEGER NOT NULL
          );
          CREATE TABLE IF NOT EXISTS reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, equipment_id INTEGER NOT NULL REFERENCES equipment(id),
            user_id TEXT NOT NULL, starts_ms INTEGER NOT NULL, ends_ms INTEGER NOT NULL,
            purpose TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', created_ms INTEGER NOT NULL
          );
          CREATE INDEX IF NOT EXISTS idx_reservations_equipment_time ON reservations(equipment_id, starts_ms, ends_ms);
          CREATE TABLE IF NOT EXISTS meeting_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', creator_id TEXT NOT NULL,
            created_ms INTEGER NOT NULL, deadline_ms INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            equipment_id INTEGER REFERENCES equipment(id), chosen_slot_id INTEGER,
            reservation_id INTEGER REFERENCES reservations(id)
          );
          CREATE TABLE IF NOT EXISTS meeting_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, poll_id INTEGER NOT NULL REFERENCES meeting_polls(id),
            starts_ms INTEGER NOT NULL, ends_ms INTEGER NOT NULL
          );
          CREATE TABLE IF NOT EXISTS meeting_votes (
            poll_id INTEGER NOT NULL, slot_id INTEGER NOT NULL, user_id TEXT NOT NULL,
            available INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
            PRIMARY KEY(slot_id, user_id)
          );
          CREATE TABLE IF NOT EXISTS arrival_watches (
            watcher_id TEXT NOT NULL, target_id TEXT NOT NULL,
            created_ms INTEGER NOT NULL, PRIMARY KEY(watcher_id,target_id)
          );
          CREATE TABLE IF NOT EXISTS arrival_notification_log (
            watcher_id TEXT NOT NULL, target_id TEXT NOT NULL,
            last_attempt_ms INTEGER NOT NULL, PRIMARY KEY(watcher_id,target_id)
          );
          CREATE TABLE IF NOT EXISTS reminder_log (
            reservation_id INTEGER PRIMARY KEY, last_attempt_ms INTEGER NOT NULL,
            success INTEGER NOT NULL DEFAULT 0
          );
          CREATE TABLE IF NOT EXISTS lab_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT '', starts_ms INTEGER NOT NULL,
            ends_ms INTEGER NOT NULL, created_by TEXT NOT NULL, created_ms INTEGER NOT NULL
          );
        ''')
        eqcols = {r['name'] for r in db.execute('PRAGMA table_info(equipment)')}
        if 'kind' not in eqcols:
            db.execute("ALTER TABLE equipment ADD COLUMN kind TEXT NOT NULL DEFAULT 'equipment'")
            db.execute('''UPDATE equipment SET kind='room' WHERE
                name LIKE '%会議室%' OR name LIKE '%打合せ室%' OR name LIKE '%ミーティングルーム%' OR
                lower(name) LIKE '%meeting room%' OR name LIKE '%セミナールーム%' ''')


def _approved(db, user_id):
    return db.execute('SELECT 1 FROM members WHERE user_id=? AND approved=1', (user_id,)).fetchone() is not None


def _valid_interval(start, end, now, *, allow_past=False):
    if type(start) is not int or type(end) is not int:
        return False
    if end <= start or end-start > MAX_BOOKING_MS or start > now + MAX_AHEAD_MS:
        return False
    if not allow_past and start < now - 60_000:
        return False
    return True


def _conflict(db, equipment_id, start, end):
    return db.execute('''SELECT 1 FROM reservations WHERE equipment_id=? AND status='active'
                     AND starts_ms < ? AND ends_ms > ? LIMIT 1''', (equipment_id, end, start)).fetchone() is not None


def install_features(app, connect_db, authorized_user, is_admin, now_ms):
    init_feature_db(connect_db)
    bp = Blueprint('labhub_v5', __name__)

    def access():
        user = authorized_user()
        admin = is_admin()
        if not user and not admin:
            return None, False
        return user, admin

    def identity(user, admin):
        if user:
            return user['id']
        if admin:
            # Admin-only browser clients can manage resources, but cannot create
            # reservations or votes impersonating a lab member.
            return None
        return None

    @bp.get('/api/v5/overview')
    def overview():
        user, admin = access()
        if not user and not admin:
            return jsonify(error='承認済みメンバーのみ閲覧できます'), 403
        uid = identity(user, admin)
        current = now_ms()
        with connect_db() as db:
            gear = [dict(r) for r in db.execute('SELECT * FROM equipment WHERE active=1 ORDER BY name')]
            reservations = [dict(r) for r in db.execute('''
                SELECT r.*, e.name AS equipment_name, m.display_name AS user_name
                FROM reservations r JOIN equipment e ON e.id=r.equipment_id
                LEFT JOIN members m ON m.user_id=r.user_id
                WHERE r.status='active' AND r.ends_ms>? AND r.starts_ms<?
                ORDER BY r.starts_ms LIMIT 120''', (current - 3600_000, current + 120*86400_000))]
            polls = [dict(r) for r in db.execute('''SELECT p.*, m.display_name AS creator_name,
                e.name AS equipment_name FROM meeting_polls p
                LEFT JOIN members m ON p.creator_id=m.user_id
                LEFT JOIN equipment e ON p.equipment_id=e.id
                WHERE p.created_ms>? ORDER BY p.created_ms DESC LIMIT 35''', (current - 60*86400_000,))]
            poll_ids = [p['id'] for p in polls]
            slots = []
            if poll_ids:
                placeholders = ','.join('?' for _ in poll_ids)
                slots = [dict(r) for r in db.execute(f'''SELECT s.*,
                    (SELECT COUNT(*) FROM meeting_votes v WHERE v.slot_id=s.id AND v.available=1) AS yes_votes,
                    (SELECT COUNT(*) FROM meeting_votes v WHERE v.slot_id=s.id) AS total_votes
                    FROM meeting_slots s WHERE s.poll_id IN ({placeholders}) ORDER BY s.starts_ms''', poll_ids)]
                if uid:
                    my_votes = {r['slot_id']:r['available'] for r in db.execute(
                        f'SELECT slot_id, available FROM meeting_votes WHERE user_id=? AND poll_id IN ({placeholders})',
                        [uid, *poll_ids])}
                    for s in slots:
                        s['my_vote'] = my_votes.get(s['id'])
            prefs = None
            people = []
            watches = []
            if uid:
                row = db.execute('SELECT arrival_sharing FROM members WHERE user_id=?', (uid,)).fetchone()
                row = db.execute('SELECT arrival_sharing,presence_recommend_opt_in FROM members WHERE user_id=?',(uid,)).fetchone()
                prefs = {'arrival_sharing': bool(row['arrival_sharing']),
                         'presence_recommend_opt_in': bool(row['presence_recommend_opt_in'])}
                people = [dict(r) for r in db.execute('''SELECT user_id,display_name,arrival_sharing
                    FROM members WHERE approved=1 AND user_id LIKE 'U%' AND user_id != ?
                    ORDER BY display_name''', (uid,))]
                watches = [r['target_id'] for r in db.execute('SELECT target_id FROM arrival_watches WHERE watcher_id=?', (uid,))]
            end_day = (datetime.fromtimestamp(current/1000, JST) + timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
            start_day = end_day - timedelta(days=1)
            today_start, today_end = int(start_day.timestamp()*1000), int(end_day.timestamp()*1000)
            events = [dict(r) for r in db.execute('''SELECT * FROM lab_events WHERE starts_ms<? AND ends_ms>?
                                                       ORDER BY starts_ms''', (today_end, today_start))]
            meeting_today = [dict(r) for r in db.execute('''SELECT p.title, s.starts_ms, s.ends_ms,
                  COALESCE(e.name,'') AS location
                  FROM meeting_polls p JOIN meeting_slots s ON s.id=p.chosen_slot_id
                  LEFT JOIN equipment e ON e.id=p.equipment_id
                  WHERE p.status='closed' AND s.starts_ms<? AND s.ends_ms>? ORDER BY s.starts_ms''',
                  (today_end, today_start))]
            own_history = []
            if uid:
                own_history = [dict(r) for r in db.execute('''SELECT r.*, e.name AS equipment_name FROM reservations r
                     JOIN equipment e ON e.id=r.equipment_id WHERE user_id=? ORDER BY created_ms DESC LIMIT 30''', (uid,))]
        return jsonify(equipment=gear,reservations=reservations,polls=polls,slots=slots,
                       me=uid,admin=admin,today_events=events, today_meetings=meeting_today,
                       preferences=prefs,people=people,watches=watches,my_reservations=own_history)

    @bp.post('/api/v5/equipment')
    def add_equipment():
        if not is_admin(): return jsonify(error='管理者のみ登録できます'), 403
        data = request.get_json(silent=True) or {}
        name = str(data.get('name','')).strip()[:80]
        location = str(data.get('location','')).strip()[:100]
        desc = str(data.get('description','')).strip()[:300]
        kind = data.get('kind','equipment')
        if kind not in ('equipment','room'):
            return jsonify(error='種類は研究設備か会議室を選んでください'),400
        if not name: return jsonify(error='設備名を入力してください'), 400
        with connect_db() as db:
            try:
                cursor=db.execute('INSERT INTO equipment(name,location,description,created_ms,kind) VALUES (?,?,?,?,?)',
                                  (name,location,desc,now_ms(),kind))
            except Exception as exc:
                if 'UNIQUE' in str(exc): return jsonify(error='同名の設備は登録済みです'), 409
                raise
        return jsonify(ok=True,id=cursor.lastrowid), 201

    @bp.post('/api/v5/reservations')
    def book_equipment():
        user=authorized_user()
        if not user:return jsonify(error='研究室メンバーとしてLINEログインしてください'),403
        body=request.get_json(silent=True) or {}
        eid,start,end=body.get('equipment_id'),body.get('starts_ms'),body.get('ends_ms')
        if type(eid) is not int or not _valid_interval(start,end,now_ms()):
            return jsonify(error='設備・開始時刻・終了時刻を確認してください'),400
        purpose=str(body.get('purpose','')).strip()[:200]
        with connect_db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM equipment WHERE id=? AND active=1',(eid,)).fetchone():
                return jsonify(error='設備が見つかりません'),404
            if _conflict(db,eid,start,end):return jsonify(error='その時間帯は予約済みです'),409
            cursor=db.execute('''INSERT INTO reservations(equipment_id,user_id,starts_ms,ends_ms,purpose,created_ms)
                VALUES(?,?,?,?,?,?)''',(eid,user['id'],start,end,purpose,now_ms()))
        return jsonify(ok=True,id=cursor.lastrowid),201

    @bp.post('/api/v5/reservations/<int:rid>/cancel')
    def cancel_reservation(rid):
        user,admin=access()
        if not user and not admin:return jsonify(error='権限がありません'),403
        with connect_db() as db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute('SELECT user_id,status FROM reservations WHERE id=?',(rid,)).fetchone()
            if not r:return jsonify(error='予約がありません'),404
            if not admin and r['user_id'] != user['id']:return jsonify(error='自分の予約だけ取り消せます'),403
            db.execute("UPDATE reservations SET status='cancelled' WHERE id=?",(rid,))
        return jsonify(ok=True)

    @bp.post('/api/v5/polls')
    def create_poll():
        user=authorized_user()
        if not user:return jsonify(error='研究室メンバーのみ作成できます'),403
        body=request.get_json(silent=True) or {}
        title=str(body.get('title','')).strip()[:100]
        desc=str(body.get('description','')).strip()[:300]
        candidates=body.get('slots')
        eid=body.get('equipment_id') or None
        deadline=body.get('deadline_ms')
        if not title or not isinstance(candidates,list) or not 2<=len(candidates)<=MAX_SLOTS:
            return jsonify(error='タイトルと2〜12件の候補日時を指定してください'),400
        if type(deadline) is not int or not now_ms() < deadline <= now_ms()+30*86400_000:
            return jsonify(error='回答期限は30日以内の未来日時にしてください'),400
        if deadline >= min((c.get('starts_ms', -1) if isinstance(c, dict) else -1) for c in candidates):
            return jsonify(error='回答期限は候補日時より前にしてください'), 400
        seen=set()
        for candidate in candidates:
            if not isinstance(candidate,dict) or not _valid_interval(candidate.get('starts_ms'),candidate.get('ends_ms'),now_ms()):
                return jsonify(error='候補日時に不正な値があります'),400
            key=(candidate['starts_ms'],candidate['ends_ms'])
            if key in seen:return jsonify(error='同じ候補日時があります'),400
            seen.add(key)
        if eid is not None and type(eid) is not int:return jsonify(error='設備指定が不正です'),400
        with connect_db() as db:
            db.execute('BEGIN IMMEDIATE')
            if eid is not None and not db.execute("SELECT 1 FROM equipment WHERE id=? AND active=1 AND kind='room'",(eid,)).fetchone():
                return jsonify(error='会議室を選択してください（計算機などの研究設備は指定できません）'),400
            cur=db.execute('''INSERT INTO meeting_polls(title,description,creator_id,created_ms,deadline_ms,equipment_id)
                              VALUES(?,?,?,?,?,?)''',(title,desc,user['id'],now_ms(),deadline,eid))
            poll_id=cur.lastrowid
            db.executemany('INSERT INTO meeting_slots(poll_id,starts_ms,ends_ms) VALUES(?,?,?)',
                           [(poll_id,c['starts_ms'],c['ends_ms']) for c in candidates])
        return jsonify(ok=True,id=poll_id),201

    @bp.post('/api/v5/polls/<int:pid>/vote')
    def cast_vote(pid):
        user=authorized_user()
        if not user:return jsonify(error='承認済みメンバーのみ回答できます'),403
        body=request.get_json(silent=True) or {}
        available=body.get('available_slot_ids')
        if not isinstance(available,list) or any(type(i) is not int for i in available):
            return jsonify(error='候補日時を指定してください'),400
        with connect_db() as db:
            db.execute('BEGIN IMMEDIATE')
            poll=db.execute('SELECT status,deadline_ms FROM meeting_polls WHERE id=?',(pid,)).fetchone()
            if not poll:return jsonify(error='日程調整が見つかりません'),404
            if poll['status']!='open' or poll['deadline_ms'] < now_ms():
                return jsonify(error='回答を締め切りました'),409
            valid=[r['id'] for r in db.execute('SELECT id FROM meeting_slots WHERE poll_id=?',(pid,))]
            if not set(available).issubset(set(valid)):
                return jsonify(error='候補外の日時が含まれています'),400
            db.executemany('''INSERT INTO meeting_votes(poll_id,slot_id,user_id,available,updated_ms)
                VALUES(?,?,?,?,?) ON CONFLICT(slot_id,user_id) DO UPDATE SET
                available=excluded.available,updated_ms=excluded.updated_ms''',
                [(pid,slot,user['id'],int(slot in available),now_ms()) for slot in valid])
        return jsonify(ok=True)

    @bp.post('/api/v5/polls/<int:pid>/finalize')
    def finalize_poll(pid):
        user,admin=access()
        if not user and not admin:return jsonify(error='権限がありません'),403
        body=request.get_json(silent=True) or {}
        sid=body.get('slot_id')
        if type(sid) is not int:return jsonify(error='確定する候補日時を選んでください'),400
        with connect_db() as db:
            db.execute('BEGIN IMMEDIATE')
            p=db.execute('SELECT * FROM meeting_polls WHERE id=?',(pid,)).fetchone()
            if not p:return jsonify(error='日程調整がありません'),404
            if not admin and p['creator_id']!=user['id']:
                return jsonify(error='作成者だけが確定できます'),403
            if p['status']!='open':return jsonify(error='確定済みです'),409
            slot=db.execute('SELECT * FROM meeting_slots WHERE id=? AND poll_id=?',(sid,pid)).fetchone()
            if not slot:return jsonify(error='その候補は存在しません'),400
            if slot['starts_ms'] <= now_ms():return jsonify(error='過去の日時は確定できません'),400
            rid=None
            if p['equipment_id'] is not None:
                if not db.execute("SELECT 1 FROM equipment WHERE id=? AND kind='room' AND active=1",(p['equipment_id'],)).fetchone():
                    return jsonify(error='会議室以外の設備は指定できません'),400
                if _conflict(db,p['equipment_id'],slot['starts_ms'],slot['ends_ms']):
                    return jsonify(error='会議室がすでに予約されています。別の候補を選んでください'),409
                rid=db.execute('''INSERT INTO reservations(equipment_id,user_id,starts_ms,ends_ms,purpose,created_ms)
                    VALUES(?,?,?,?,?,?)''',(p['equipment_id'],p['creator_id'],slot['starts_ms'],slot['ends_ms'],
                                            'ミーティング：'+p['title'],now_ms())).lastrowid
            db.execute("UPDATE meeting_polls SET status='closed',chosen_slot_id=?,reservation_id=? WHERE id=?",
                       (sid,rid,pid))
        return jsonify(ok=True,reservation_id=rid)

    @bp.put('/api/v5/me/presence-recommend-opt-in')
    def recommend_preference():
        user=authorized_user()
        if not user:return jsonify(error='承認済みメンバーのみ利用できます'),403
        body=request.get_json(silent=True) or {}
        allow=body.get('allow')
        if type(allow) is not bool:return jsonify(error='allowは真偽値です'),400
        with connect_db() as db:
            db.execute('UPDATE members SET presence_recommend_opt_in=? WHERE user_id=?',
                       (int(allow),user['id']))
        return jsonify(ok=True)

    @bp.get('/api/v5/schedule-suggestions')
    def schedule_suggestions():
        user=authorized_user()
        if not user:return jsonify(error='承認済みメンバーのみ利用できます'),403
        try:
            duration=int(request.args.get('duration','60'))
            room_text=request.args.get('room_id','')
            room_id=int(room_text) if room_text else None
            deadline_text=request.args.get('deadline_ms','')
            minimum=int(deadline_text) if deadline_text else None
            with connect_db() as db:
                outcome=generate_suggestions(db,now_ms(),
                    first_day=request.args.get('first_day',''),duration=duration,
                    room_id=room_id,min_start_ms=minimum)
        except (ValueError,OverflowError) as e:
            return jsonify(error=str(e)),400
        return jsonify(outcome)

    @bp.put('/api/v5/me/arrival-sharing')
    def arrival_preference():
        user=authorized_user()
        if not user:return jsonify(error='承認済みメンバーのみ利用できます'),403
        body=request.get_json(silent=True) or {}
        allow=body.get('allow')
        if type(allow) is not bool:return jsonify(error='allowは真偽値です'),400
        with connect_db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE members SET arrival_sharing=? WHERE user_id=?',(int(allow),user['id']))
            if not allow:db.execute('DELETE FROM arrival_watches WHERE target_id=?',(user['id'],))
        return jsonify(ok=True)

    @bp.put('/api/v5/me/watches/<target_id>')
    def update_watch(target_id):
        user=authorized_user()
        if not user:return jsonify(error='承認済みメンバーのみ利用できます'),403
        if target_id==user['id']:return jsonify(error='自分自身は登録できません'),400
        body=request.get_json(silent=True) or {}
        enabled=body.get('enabled')
        if type(enabled) is not bool:return jsonify(error='enabledは真偽値です'),400
        with connect_db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT approved,arrival_sharing FROM members WHERE user_id=?',(target_id,)).fetchone()
            if not row or not row['approved'] or (enabled and not row['arrival_sharing']):
                return jsonify(error='対象者が通知を許可していません'),403
            if enabled:
                db.execute('''INSERT OR IGNORE INTO arrival_watches(watcher_id,target_id,created_ms)
                              VALUES(?,?,?)''',(user['id'],target_id,now_ms()))
            else:
                db.execute('DELETE FROM arrival_watches WHERE watcher_id=? AND target_id=?',(user['id'],target_id))
        return jsonify(ok=True)

    @bp.post('/api/v5/events')
    def create_event():
        if not is_admin():return jsonify(error='管理者のみ登録できます'),403
        body=request.get_json(silent=True) or {}
        title=str(body.get('title','')).strip()[:100]
        location=str(body.get('location','')).strip()[:100]
        start,end=body.get('starts_ms'),body.get('ends_ms')
        if not title or not _valid_interval(start,end,now_ms()):return jsonify(error='イベント名と日時を確認してください'),400
        with connect_db() as db:
            rid=db.execute('''INSERT INTO lab_events(title,location,starts_ms,ends_ms,created_by,created_ms)
                              VALUES(?,?,?,?,?,?)''',(title,location,start,end,'admin',now_ms())).lastrowid
        return jsonify(ok=True,id=rid),201

    app.register_blueprint(bp)


def deliver_arrival_alerts(user_id, event_ms, connect_db, now_ms, line_push):
    """Only opted-in targets; only approved watchers; at most once per 5 minutes.

    Deduplication is reserved inside a write transaction to survive server restarts.
    A LINE delivery failure is logged but not automatically retried (avoid duplicates).
    """
    now=now_ms()
    if event_ms < now-10*60_000 or event_ms > now+60_000:
        return 0
    send=[]
    with connect_db() as db:
        db.execute('BEGIN IMMEDIATE')
        target=db.execute('SELECT display_name,approved,arrival_sharing FROM members WHERE user_id=?', (user_id,)).fetchone()
        if not target or not target['approved'] or not target['arrival_sharing']:
            return 0
        target_name=target['display_name']
        watchers=db.execute('''SELECT w.watcher_id,l.last_attempt_ms FROM arrival_watches w
            JOIN members m ON m.user_id=w.watcher_id AND m.approved=1
            LEFT JOIN arrival_notification_log l ON l.watcher_id=w.watcher_id AND l.target_id=w.target_id
            WHERE w.target_id=?''',(user_id,)).fetchall()
        for watcher in watchers:
            if watcher['last_attempt_ms'] is not None and now - watcher['last_attempt_ms'] < 5*60_000:
                continue
            db.execute('''INSERT INTO arrival_notification_log(watcher_id,target_id,last_attempt_ms)
               VALUES(?,?,?) ON CONFLICT(watcher_id,target_id) DO UPDATE SET last_attempt_ms=excluded.last_attempt_ms''',
               (watcher['watcher_id'],user_id,now))
            send.append(watcher['watcher_id'])
    for watcher_id in send:
        line_push(watcher_id,f'🔔 LabHub：{target_name}さんの入室を検知しました。\n研究室の在室状況をご確認ください。')
    return len(send)


def send_due_reservation_reminders(connect_db, now_ms, line_push):
    """Call from Windows Task Scheduler once each minute (single instance).

    A reminder is attempted when start is within 10 min. Failed pushes retry
    no more than once every 5 min; no notification after reservation starts.
    """
    now=now_ms()
    with connect_db() as db:
        rows=[dict(r) for r in db.execute('''SELECT r.id, r.user_id,r.starts_ms,e.name, l.last_attempt_ms,l.success
            FROM reservations r JOIN equipment e ON e.id=r.equipment_id
            JOIN members m ON m.user_id=r.user_id AND m.approved=1
            LEFT JOIN reminder_log l ON l.reservation_id=r.id
            WHERE r.status='active' AND r.starts_ms>? AND r.starts_ms <= ?
              AND (l.reservation_id IS NULL OR (l.success=0 AND l.last_attempt_ms<?))''',
            (now,now+10*60_000,now-5*60_000))]
    done=0
    for r in rows:
        starts=datetime.fromtimestamp(r['starts_ms']/1000,JST).strftime('%m/%d %H:%M')
        ok=line_push(r['user_id'],f'⏰ LabHub 設備予約リマインド\n{r["name"]}\n開始：{starts}\nまもなく予約時刻です。')
        with connect_db() as db:
            db.execute('''INSERT INTO reminder_log(reservation_id,last_attempt_ms,success)
              VALUES(?,?,?) ON CONFLICT(reservation_id) DO UPDATE SET
              last_attempt_ms=excluded.last_attempt_ms,success=excluded.success''', (r['id'], now, int(ok)))
        done+=int(ok)
    return done
