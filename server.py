"""LabHub v5.1: member-managed activities independent from iBeacon presence.

Keeps the existing /callback LINE signature verification and bot replies.
The dashboard only serves member data to verified, approved LINE users or admins.
The admin header key is a temporary MVP control; use server-side admin accounts for production.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, render_template, request
from labhub_features import install_features, deliver_arrival_alerts

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("LABHUB_DB_PATH", str(BASE_DIR / "labhub.db")))
LOG_PATH = BASE_DIR / "beacon_events.jsonl"
CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ADMIN_KEY = os.environ.get("LABHUB_ADMIN_KEY", "").strip()
LIFF_ID = os.environ.get("LABHUB_LIFF_ID", "")
LINE_LOGIN_CHANNEL_ID = os.environ.get("LINE_LOGIN_CHANNEL_ID", "")
REPLY_ON_ENTER = os.environ.get("REPLY_ON_ENTER", "1") == "1"
PRESENCE_HOURS = float(os.environ.get("PRESENCE_HOURS", "8"))
API = "https://api.line.me/v2/bot"
JST = timezone(timedelta(hours=9))
# Keep old choices readable/editable when migrating a v5 database.
ACTIVITY_CHOICES = (
    "未設定", "研究中", "食事中", "授業中", "外出中", "会議中",
    "帰宅", "対応可能", "集中中", "離席中", "会話OK",
)
ACTIVITIES = set(ACTIVITY_CHOICES)
ACTIVITY_TTLS = {30, 60, 120, 240, 480}  # minutes; 0 = keep until explicitly cleared
APP_REGION = os.environ.get("LABHUB_BEACON_REGION", "labhub.lab1.room1")
APP_NOTIFY = os.environ.get("LABHUB_NOTIFY_APP_EVENTS", "0") == "1"
# An individual, long random bearer token binds one iPhone to an approved LINE member.
def parse_app_tokens(raw: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        token, separator, user_id = item.partition(":")
        if not separator or len(token) < 32 or not user_id.startswith("U") or len(user_id) > 128:
            raise ValueError("LABHUB_APP_TOKENS: token(32+ characters):LINE_USER_ID format required")
        if token in parsed:
            raise ValueError("LABHUB_APP_TOKENS: duplicate tokens")
        parsed[token] = user_id
    return parsed

APP_TOKENS = parse_app_tokens(os.environ.get("LABHUB_APP_TOKENS", ""))
APP_USERS = set(APP_TOKENS.values())

app = Flask(__name__)
seen_event_ids: set[str] = set()
display_names: dict[str, str] = {}


@contextmanager
def connect_db():
    # sqlite3.Connection's default context manager commits but DOES NOT close.
    # Closing explicitly is important on Windows (DB backup/replacement).
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS members (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'away',
                activity TEXT NOT NULL DEFAULT '未設定',
                last_enter_ms INTEGER,
                updated_ms INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual'
            )
        """)
        columns = {r["name"] for r in db.execute("PRAGMA table_info(members)")}
        if "approved" not in columns:
            db.execute("ALTER TABLE members ADD COLUMN approved INTEGER NOT NULL DEFAULT 0")
            # Local test users are created by admins, but they cannot LINE-login until linked.
            db.execute("UPDATE members SET approved = 1 WHERE user_id LIKE 'local-%'")
        # v3 migrates existing DBs in-place, without dropping rows.
        # Status timestamps must be separate from activity/profile changes.
        if "last_presence_ms" not in columns:
            db.execute("ALTER TABLE members ADD COLUMN last_presence_ms INTEGER")
            db.execute("""UPDATE members SET last_presence_ms =
                CASE WHEN status = 'present' THEN COALESCE(last_enter_ms, updated_ms)
                ELSE updated_ms END""")
        if "last_beacon_ms" not in columns:
            db.execute("ALTER TABLE members ADD COLUMN last_beacon_ms INTEGER")
            db.execute("""UPDATE members SET last_beacon_ms = last_enter_ms
                WHERE source = 'beacon' AND last_enter_ms IS NOT NULL""")
        if "last_app_ms" not in columns:
            db.execute("ALTER TABLE members ADD COLUMN last_app_ms INTEGER")
        # Activity and position are orthogonal: iBeacon must not overwrite activity.
        # On legacy databases, existing activities are kept with no expiry.
        if "activity_expires_ms" not in columns:
            db.execute("ALTER TABLE members ADD COLUMN activity_expires_ms INTEGER")
        if "activity_updated_ms" not in columns:
            db.execute("ALTER TABLE members ADD COLUMN activity_updated_ms INTEGER")
        db.execute("""
            CREATE TABLE IF NOT EXISTS activity_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                activity TEXT NOT NULL,
                changed_ms INTEGER NOT NULL,
                expires_ms INTEGER,
                changed_by TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES members(user_id)
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_activity_events_user_time ON activity_events(user_id,changed_ms DESC)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS app_presence_events (
                event_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_ms INTEGER NOT NULL,
                received_ms INTEGER NOT NULL,
                region TEXT NOT NULL,
                applied INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_app_events_user_time ON app_presence_events(user_id,event_ms)")
        db.commit()


init_db()


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, JST).strftime("%m/%d %H:%M")


def verify_signature(body: bytes, signature: str | None) -> bool:
    digest = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature or "")


def headers():
    return {"Authorization": f"Bearer {ACCESS_TOKEN}"}


def reply(reply_token: str, text: str):
    if not reply_token:
        return
    try:
        response = requests.post(
            f"{API}/message/reply",
            headers=headers(),
            json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]},
            timeout=10,
        )
        if not response.ok:
            app.logger.warning("LINE reply failed: %s %s", response.status_code, response.text)
    except requests.RequestException as exc:
        app.logger.warning("LINE reply request failed: %s", exc)


def get_display_name(user_id: str) -> str:
    if user_id not in display_names:
        try:
            response = requests.get(f"{API}/profile/{user_id}", headers=headers(), timeout=8)
            if response.ok:
                name = response.json().get("displayName", "")
                if isinstance(name, str) and name.strip():
                    display_names[user_id] = name.strip()[:80]
        except (requests.RequestException, ValueError) as exc:
            app.logger.info("Could not fetch LINE display name: %s", exc)
    return display_names.get(user_id, "LINEメンバー")


def log_event(record: dict):
    with LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def handle_beacon(event: dict, received_ms: int):
    user_id = event.get("source", {}).get("userId")
    beacon = event.get("beacon", {})
    event_ms = event.get("timestamp", received_ms)
    delay_s = (received_ms - event_ms) / 1000
    record = {
        "received_at": fmt(received_ms),
        "user_id": user_id,
        "hwid": beacon.get("hwid"),
        "type": beacon.get("type"),
        "dm": beacon.get("dm"),
        "delay_s": round(delay_s, 2),
    }
    log_event(record)
    app.logger.info("beacon: %s", record)

    if beacon.get("type") != "enter" or not user_id:
        return

    # Once this LINE user has a LabHub iPhone token, let the dedicated app
    # own their presence. LINE's delayed 'enter' must not undo an app exit.
    if user_id in APP_USERS:
        with connect_db() as db:
            db.execute("""UPDATE members SET last_beacon_ms = MAX(
                COALESCE(last_beacon_ms, 0), ?) WHERE user_id = ?""", (event_ms, user_id))
            db.commit()
        return

    name = get_display_name(user_id)
    new_arrival = False
    with connect_db() as db:
        before = db.execute('SELECT status,last_presence_ms FROM members WHERE user_id=?', (user_id,)).fetchone()
        new_arrival = (not before or event_ms > (before['last_presence_ms'] or 0)) and (
            not before or before['status'] != 'present' or
            before['last_presence_ms'] is None or
            event_ms - before['last_presence_ms'] >= int(PRESENCE_HOURS * 3600_000))

        db.execute("""
            INSERT INTO members (user_id, display_name, status, activity,
                                 last_enter_ms, updated_ms, source,
                                 last_presence_ms, last_beacon_ms)
            VALUES (?, ?, 'present', '未設定', ?, ?, 'beacon', ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              display_name = CASE WHEN excluded.display_name = 'LINEメンバー'
                                  THEN members.display_name ELSE excluded.display_name END,
              last_beacon_ms = MAX(COALESCE(members.last_beacon_ms, 0), excluded.last_beacon_ms),
              status = CASE WHEN excluded.last_presence_ms > COALESCE(members.last_presence_ms, 0)
                            THEN 'present' ELSE members.status END,
              last_enter_ms = CASE WHEN excluded.last_presence_ms > COALESCE(members.last_presence_ms, 0)
                                   THEN excluded.last_enter_ms ELSE members.last_enter_ms END,
              last_presence_ms = MAX(COALESCE(members.last_presence_ms, 0), excluded.last_presence_ms),
              updated_ms = MAX(members.updated_ms, excluded.updated_ms),
              source = CASE WHEN excluded.last_presence_ms > COALESCE(members.last_presence_ms, 0)
                            THEN 'beacon' ELSE members.source END
        """, (user_id, name, event_ms, received_ms, event_ms, event_ms))
        db.commit()
    if new_arrival:
        deliver_arrival_alerts(user_id, event_ms, connect_db, now_ms, push_line_text)
    if REPLY_ON_ENTER:
        reply(event.get("replyToken", ""),
              f"入室を検知しました（{fmt(event_ms)}、遅延 {delay_s:.1f} 秒）")


def effective_status(row, reference_ms: int) -> str:
    """Do not interpret stale presence as a confirmed departure.

    present = explicit check-in; auto = Beacon enter; unknown = too old;
    away = explicit manual check-out. An activity update never extends presence.
    """
    if row["status"] != "present":
        return "away"
    presence_ms = row["last_presence_ms"]
    if presence_ms is None or reference_ms - presence_ms >= int(PRESENCE_HOURS * 3600 * 1000):
        return "unknown"
    return "auto" if row["source"] in ("beacon", "app") else "present"


def effective_activity(row, reference_ms: int) -> tuple[str, int | None]:
    """Return effective activity without modifying presence or overwriting history."""
    activity = row["activity"]
    until = row["activity_expires_ms"]
    if not activity or activity not in ACTIVITIES or activity == "未設定":
        return "未設定", None
    if until is not None and reference_ms >= until:
        return "未設定", None
    return activity, until


def parse_activity_payload(data):
    """Validate a user-chosen activity and expiry, without trusting JSON types."""
    if not isinstance(data, dict):
        return None, None, "JSONオブジェクトを指定してください"
    activity = data.get("activity")
    if not isinstance(activity, str) or activity not in ACTIVITIES:
        return None, None, "活動状態を選択してください"
    if activity == "未設定":
        return activity, None, None
    ttl = data.get("ttl_minutes", 60)
    if type(ttl) is not int or (ttl not in ACTIVITY_TTLS and ttl != 0):
        return None, None, "有効期限は30・60・120・240・480分、または解除まで(0)を選択してください"
    return activity, ttl, None


def save_activity(db, user_id: str, activity: str, ttl: int | None,
                  actor: str, ms: int) -> bool:
    """Save an activity + append audit trail; do NOT touch presence timestamps."""
    expires = ms + ttl * 60_000 if ttl else None
    cursor = db.execute("""
        UPDATE members SET activity=?, activity_updated_ms=?, activity_expires_ms=?
        WHERE user_id=? AND approved=1
    """, (activity, ms, expires, user_id))
    if not cursor.rowcount:
        return False
    db.execute("""
        INSERT INTO activity_events (user_id,activity,changed_ms,expires_ms,changed_by)
        VALUES (?,?,?,?,?)
    """, (user_id, activity, ms, expires, actor))
    return True


def current_present(rows, reference_ms: int):
    return [row for row in rows if effective_status(row, reference_ms) in ("present", "auto")]


def handle_message(event: dict):
    message = event.get("message", {})
    text = message.get("text", "")
    if "誰いる" not in text and "今日の予定" not in text and "今日のイベント" not in text:
        return
    with connect_db() as db:
        rows = db.execute("SELECT * FROM members ORDER BY last_enter_ms DESC").fetchall()
    # A LINE friend is NOT automatically an approved member.
    source_id = event.get("source", {}).get("userId", "")
    if not any(row["user_id"] == source_id and row["approved"] for row in rows):
        reply(event.get("replyToken", ""), "研究室の在室情報は承認済みメンバー限定です。LabHubからLINEログインし、管理者の承認を受けてください。")
        return
    if "今日の予定" in text or "今日のイベント" in text:
        current = datetime.fromtimestamp(now_ms() / 1000, JST)
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        a, b = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        with connect_db() as db:
            events = [dict(r) for r in db.execute(
                "SELECT title,starts_ms,location FROM lab_events WHERE starts_ms<? AND ends_ms>? ORDER BY starts_ms",
                (b, a))]
            meetings = [dict(r) for r in db.execute('''
                SELECT p.title,s.starts_ms,COALESCE(e.name,'') AS location
                FROM meeting_polls p JOIN meeting_slots s ON s.id=p.chosen_slot_id
                LEFT JOIN equipment e ON e.id=p.equipment_id
                WHERE p.status='closed' AND s.starts_ms<? AND s.ends_ms>?
                ORDER BY s.starts_ms''', (b, a))]
        agenda = sorted(events + meetings, key=lambda item: item["starts_ms"])
        if not agenda:
            reply(event.get("replyToken", ""), "今日の研究室イベントは登録されていません。")
        else:
            lines = [f"・{fmt(item['starts_ms'])} {item['title']}" +
                     (f"（{item['location']}）" if item['location'] else "") for item in agenda[:15]]
            reply(event.get("replyToken", ""), "今日の研究室予定\n" + "\n".join(lines))
        return
    rows = [row for row in rows if row["approved"]]
    present = current_present(rows, now_ms())
    if not present:
        reply(event.get("replyToken", ""), "今は誰もいないようです")
        return
    present = sorted(present, key=lambda row: row["last_enter_ms"] or 0)
    lines = [f"・{row['display_name']}（{'自動検知' if effective_status(row, now_ms()) == 'auto' else '手動申告'}、{fmt(row['last_enter_ms']) if row['last_enter_ms'] else '時刻不明'} 入室）" for row in present]
    reply(event.get("replyToken", ""), f"在室中 {len(present)} 人\n" + "\n".join(lines))


def handle_follow(event: dict):
    reply(event.get("replyToken", ""),
          "友だち追加ありがとうございます。\n"
          "在室を自動で記録するには、次の設定をオンにしてください。\n"
          "1. スマホの Bluetooth\n"
          "2. LINE の［設定］>［プライバシー管理］>［LINE Beacon］\n"
          "3. iPhone の場合：［設定］>［LINE］>［位置情報］を「常に」にし、［正確な位置情報］をオン\n"
          "※ 位置情報を受け取るのは LINE アプリで、このbotには届きません。")


@app.post("/callback")
def callback():
    body = request.get_data()
    if not verify_signature(body, request.headers.get("X-Line-Signature")):
        abort(400)
    received_ms = now_ms()
    for event in json.loads(body).get("events", []):
        event_id = event.get("webhookEventId")
        if event_id and event_id in seen_event_ids:
            continue
        if event_id:
            seen_event_ids.add(event_id)
            if len(seen_event_ids) > 20000:
                seen_event_ids.clear()  # bounded, in-memory test-only de-duplication
        event_type = event.get("type")
        if event_type == "beacon":
            handle_beacon(event, received_ms)
        elif event_type == "message" and event.get("message", {}).get("type") == "text":
            handle_message(event)
        elif event_type == "follow":
            handle_follow(event)
    return "OK"


def is_admin() -> bool:
    supplied = request.headers.get("X-LabHub-Key", "")
    return bool(ADMIN_KEY) and bool(supplied) and hmac.compare_digest(ADMIN_KEY, supplied)


def require_admin():
    if not ADMIN_KEY:
        abort(503, "LABHUB_ADMIN_KEY が設定されていません")
    if not is_admin():
        abort(401)


@app.get("/")
def dashboard():
    return render_template("index.html", liff_id=LIFF_ID)


def verified_line_user():
    """Only LINE-issued, server-verified ID tokens authorize a user."""
    bearer = request.headers.get("Authorization", "")
    if not bearer.startswith("Bearer "):
        return None
    token = bearer[len("Bearer "):].strip()
    if not token or len(token) > 10000:
        return None
    try:
        response = requests.post(
            "https://api.line.me/oauth2/v2.1/verify",
            data={"id_token": token, "client_id": LINE_LOGIN_CHANNEL_ID},
            timeout=8,
        )
        if not response.ok:
            return None
        details = response.json()
        subject = details.get("sub", "")
        if details.get("aud") != LINE_LOGIN_CHANNEL_ID or not isinstance(subject, str) or not subject.startswith("U"):
            return None
        return {"id": subject, "name": str(details.get("name") or "LINEユーザー")[:80]}
    except (requests.RequestException, ValueError):
        return None


def authorized_user():
    user = verified_line_user()
    if user is None:
        return None
    with connect_db() as db:
        row = db.execute("SELECT approved FROM members WHERE user_id = ?", (user["id"],)).fetchone()
    return user if row and row["approved"] else None


@app.post("/api/auth/me")
def auth_me():
    user = verified_line_user()
    if not user:
        return jsonify({"error": "LINEログインを確認できませんでした"}), 401
    with connect_db() as db:
        db.execute("""
            INSERT INTO members (user_id, display_name, status, activity, updated_ms, source, approved)
            VALUES (?, ?, 'away', '未設定', ?, 'line_login', 0)
            ON CONFLICT(user_id) DO UPDATE SET
              display_name = CASE WHEN members.source IN ('line_login', 'beacon')
                                  THEN excluded.display_name ELSE members.display_name END
        """, (user["id"], user["name"], now_ms()))
        row = db.execute("SELECT approved FROM members WHERE user_id = ?", (user["id"],)).fetchone()
        db.commit()
    return jsonify({"display_name": user["name"], "approved": bool(row["approved"])})


@app.get("/api/presence")
def api_presence():
    admin = is_admin()
    user = verified_line_user()
    with connect_db() as db:
        rows = db.execute("SELECT * FROM members ORDER BY updated_ms DESC, user_id ASC").fetchall()
    approved_id_set = {row["user_id"] for row in rows if row["approved"]}
    approved = admin or (user is not None and user["id"] in approved_id_set)
    if not approved:
        return jsonify({
            "authenticated": bool(user), "approved": False, "can_edit": False,
            "admin_enabled": bool(ADMIN_KEY), "present_count": None,
            "member_count": None, "members": [], "pending": [],
            "presence_hours": PRESENCE_HOURS,
            "updated_at": datetime.fromtimestamp(now_ms() / 1000, JST).isoformat(),
        })
    time_now = now_ms()
    approved_rows = [row for row in rows if row["approved"]]
    status_map = {row["user_id"]: effective_status(row, time_now) for row in approved_rows}
    present_ids = {uid for uid, status in status_map.items() if status in ("present", "auto")}
    members = []
    self_member = None
    for row in approved_rows:
        activity, activity_until = effective_activity(row, time_now)
        member = {
            "name": row["display_name"],
            "status": status_map[row["user_id"]],
            "activity": activity,
            "activity_expires_ms": activity_until,
            "last_enter": fmt(row["last_enter_ms"]) if row["last_enter_ms"] else None,
            "last_beacon": fmt(row["last_beacon_ms"]) if row["last_beacon_ms"] else None,
            "last_auto": fmt(max(row["last_beacon_ms"] or 0, row["last_app_ms"] or 0)) if (row["last_beacon_ms"] or row["last_app_ms"]) else None,
            "last_presence": fmt(row["last_presence_ms"]) if row["last_presence_ms"] else None,
            "source": row["source"],
            "mine": bool(user and row["user_id"] == user["id"]),
        }
        if member["mine"]:
            self_member = {
                "status": member["status"], "last_presence": member["last_presence"],
                "activity": activity, "activity_expires_ms": activity_until,
            }
        if admin:
            member["id"] = row["user_id"]
        members.append(member)
    return jsonify({
        "authenticated": True, "approved": True,
        "present_count": len(present_ids), "member_count": len(approved_rows),
        "unknown_count": sum(status == "unknown" for status in status_map.values()),
        "members": members, "self_member": self_member,
        "pending": [{"name": row["display_name"], "id": row["user_id"]} for row in rows if not row["approved"]] if admin else [],
        "admin_enabled": bool(ADMIN_KEY), "can_edit": admin,
        "can_edit_self": bool(user), "presence_hours": PRESENCE_HOURS,
        "updated_at": datetime.fromtimestamp(time_now / 1000, JST).isoformat(),
    })


@app.post("/api/admin/members/<user_id>/approve")
def approve_member(user_id: str):
    require_admin()
    if not user_id.startswith("U"):
        return jsonify({"error": "LINEでログイン済みのユーザーだけを承認できます"}), 400
    with connect_db() as db:
        cursor = db.execute("UPDATE members SET approved = 1 WHERE user_id = ?", (user_id,))
        db.commit()
        if not cursor.rowcount:
            abort(404)
    return jsonify({"ok": True})


@app.post("/api/me/presence")
def my_presence():
    user = authorized_user()
    if not user:
        return jsonify({"error": "研究室メンバーの承認が必要です"}), 403
    data = request.get_json(silent=True) or {}
    state = data.get("status")
    if state not in {"present", "away"}:
        return jsonify({"error": "statusはpresentまたはawayにしてください"}), 400
    ms = now_ms()
    with connect_db() as db:
        db.execute("""
            UPDATE members SET status = ?, last_enter_ms = CASE
              WHEN ? = 'present' THEN ? ELSE last_enter_ms END,
              updated_ms = ?, last_presence_ms = ?, source = 'manual' WHERE user_id = ? AND approved = 1
        """, (state, state, ms, ms, ms, user["id"]))
        db.commit()
    return jsonify({"ok": True})


def app_token_user() -> str | None:
    """Check a per-device bearer token using constant-time comparison."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    provided = auth[7:].strip()
    if not provided or len(provided) > 512:
        return None
    found = None
    for known_token, user_id in APP_TOKENS.items():
        if hmac.compare_digest(provided.encode(), known_token.encode()):
            found = user_id
    return found


def push_line_text(user_id: str, content: str) -> bool:
    """A LINE push for a consenting, approved member; return delivery acceptance."""
    try:
        response = requests.post(
            f"{API}/message/push", headers=headers(),
            json={"to": user_id, "messages": [{"type": "text", "text": content[:4500]}]},
            timeout=8,
        )
        if not response.ok:
            app.logger.warning("LINE push failed: %s %s", response.status_code, response.text[:200])
        return response.ok
    except requests.RequestException as exc:
        app.logger.warning("LINE push exception: %s", exc)
        return False


def push_app_presence(user_id: str, action: str, event_ms: int) -> None:
    """Best-effort notification; an API failure never rolls back a committed presence."""
    if not APP_NOTIFY:
        return
    word = "入室" if action == "enter" else "退室"
    new_state = "在室（自動検知）" if action == "enter" else "不在"
    content = (f"LabHub Beacon：{word}を検知しました\n"
               f"検知時刻：{fmt(event_ms)}\n"
               f"在室状態：{new_state}\n"
               "※ iBeaconによる推定です。")
    try:
        response = requests.post(
            f"{API}/message/push",
            headers=headers(),
            json={"to": user_id, "messages": [{"type": "text", "text": content}]},
            timeout=8,
        )
        if not response.ok:
            app.logger.warning("LINE push failed: %s %s", response.status_code, response.text[:300])
    except requests.RequestException as exc:
        app.logger.warning("LINE push request failed: %s", exc)


@app.post("/api/app/presence")
def receive_app_presence():
    """Accept authenticated iPhone events, deduplicate durably, apply in event-time order."""
    user_id = app_token_user()
    if not user_id:
        return jsonify(ok=False, error="invalid device token"), 401
    with connect_db() as db:
        member = db.execute("SELECT approved FROM members WHERE user_id = ?", (user_id,)).fetchone()
    if not member or not member["approved"]:
        return jsonify(ok=False, error="research-lab membership not approved"), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error="expected JSON object"), 400
    event_id, action, event_ms, region = (data.get(key) for key in ("id", "event", "ts", "region"))
    if (not isinstance(event_id, str) or not 8 <= len(event_id) <= 128
            or action not in {"enter", "exit", "test"} or type(event_ms) is not int
            or region != APP_REGION):
        return jsonify(ok=False, error="invalid id/event/ts/region"), 400
    received_ms = now_ms()
    # Old events may be retried after a disconnect but cannot change today's state.
    if event_ms > received_ms + 5 * 60_000:
        return jsonify(ok=False, error="event time is too far in the future"), 400
    if event_ms < received_ms - 48 * 60 * 60_000:
        return jsonify(ok=True, applied=False, stale=True, reason="older than 48h")

    changed = False
    applied = False
    new_arrival = False
    with connect_db() as db:
        # An IMMEDIATE transaction serializes competing app/manual/Beacon writes
        # at the SQLite level. The event IDs persist across Flask restarts.
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT 1 FROM app_presence_events WHERE event_id = ?", (event_id,)).fetchone()
        if existing:
            return jsonify(ok=True, applied=False, duplicate=True)
        row = db.execute("SELECT status,last_presence_ms,approved FROM members WHERE user_id = ?", (user_id,)).fetchone()
        if not row or not row["approved"]:
            return jsonify(ok=False, error="research-lab membership not approved"), 403
        # All test events are logged without altering presence.
        if action != "test" and event_ms > (row["last_presence_ms"] or 0):
            new_status = "present" if action == "enter" else "away"
            changed = row["status"] != new_status
            new_arrival = (action == "enter" and (row["status"] != 'present' or
                row['last_presence_ms'] is None or
                event_ms - row['last_presence_ms'] >= int(PRESENCE_HOURS * 3600_000)))
            db.execute("""
                UPDATE members SET status=?, source='app',
                  last_enter_ms = CASE WHEN ? = 'enter' THEN ? ELSE last_enter_ms END,
                  last_presence_ms=?, updated_ms=?, last_app_ms=?
                WHERE user_id=?
            """, (new_status, action, event_ms, event_ms, received_ms, event_ms, user_id))
            applied = True
        db.execute("""
            INSERT INTO app_presence_events
            (event_id,user_id,event_type,event_ms,received_ms,region,applied)
            VALUES (?,?,?,?,?,?,?)
        """, (event_id, user_id, action, event_ms, received_ms, region, int(applied)))
        db.commit()

    if applied and changed:
        push_app_presence(user_id, action, event_ms)
    if applied and new_arrival:
        deliver_arrival_alerts(user_id, event_ms, connect_db, now_ms, push_line_text)
    return jsonify(ok=True, applied=applied, duplicate=False, stale=(action != "test" and not applied),
                   notification_attempted=bool(applied and changed and APP_NOTIFY))


@app.post("/api/admin/check")
def check_admin():
    require_admin()
    return jsonify({"ok": True})


@app.post("/api/admin/members")
def add_member():
    require_admin()
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not (1 <= len(name) <= 40):
        return jsonify({"error": "名前は1〜40文字で入力してください"}), 400
    user_id = "local-" + uuid.uuid4().hex
    with connect_db() as db:
        db.execute("""
            INSERT INTO members (user_id, display_name, status, activity, updated_ms, source, approved)
            VALUES (?, ?, 'away', '未設定', ?, 'manual', 1)
        """, (user_id, name, now_ms()))
        db.commit()
    return jsonify({"ok": True}), 201


@app.post("/api/admin/members/<user_id>/presence")
def set_member_presence(user_id: str):
    require_admin()
    data = request.get_json(silent=True) or {}
    state = data.get("status")
    if state not in {"present", "away"}:
        return jsonify({"error": "statusはpresentまたはawayにしてください"}), 400
    ms = now_ms()
    with connect_db() as db:
        row = db.execute("SELECT user_id FROM members WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            abort(404)
        db.execute("""
            UPDATE members
            SET status = ?, last_enter_ms = CASE WHEN ? = 'present' THEN ? ELSE last_enter_ms END,
                updated_ms = ?, last_presence_ms = ?, source = 'manual'
            WHERE user_id = ?
        """, (state, state, ms, ms, ms, user_id))
        db.commit()
    return jsonify({"ok": True})


@app.post("/api/me/activity")
def my_activity():
    """Approved LINE member may edit only their own activity."""
    user = authorized_user()
    if not user:
        return jsonify({"error": "研究室メンバーの承認が必要です"}), 403
    activity, ttl, error = parse_activity_payload(request.get_json(silent=True))
    if error:
        return jsonify({"error": error}), 400
    ms = now_ms()
    with connect_db() as db:
        if not save_activity(db, user["id"], activity, ttl, user["id"], ms):
            return jsonify({"error": "メンバーが見つかりません"}), 404
    return jsonify({"ok": True, "activity": activity,
                    "activity_expires_ms": ms + ttl * 60_000 if ttl else None})


@app.get("/api/me/activity/history")
def my_activity_history():
    """Private history: never return other members' activity logs."""
    user = authorized_user()
    if not user:
        return jsonify({"error": "研究室メンバーの承認が必要です"}), 403
    with connect_db() as db:
        rows = db.execute("""
            SELECT activity,changed_ms,expires_ms FROM activity_events
            WHERE user_id=? ORDER BY changed_ms DESC,id DESC LIMIT 10
        """, (user["id"],)).fetchall()
    return jsonify({"history": [dict(row) for row in rows]})


@app.post("/api/admin/members/<user_id>/activity")
def set_member_activity(user_id: str):
    require_admin()
    activity, ttl, error = parse_activity_payload(request.get_json(silent=True))
    if error:
        return jsonify({"error": error}), 400
    with connect_db() as db:
        if not save_activity(db, user_id, activity, ttl, "admin", now_ms()):
            abort(404)
    return jsonify({"ok": True})


# v5 features share the v4 LINE login and approval controls.
install_features(app, connect_db, authorized_user, is_admin, now_ms)


@app.after_request
def prevent_data_cache(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
