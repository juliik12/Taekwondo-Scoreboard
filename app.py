from flask import Flask, jsonify, request, send_file, session, redirect
from functools import wraps
from pathlib import Path
from datetime import datetime
from datetime import timedelta
import sqlite3
import io
import json
import re
import os
import secrets
import hashlib
import hmac
import time
import textwrap
import threading
from urllib.parse import urlparse

app = Flask(__name__)
secret = os.getenv("SECRET_KEY")
if not secret:
    raise RuntimeError("SECRET_KEY no configurada en variables de entorno")
app.secret_key = secret
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

BASE_DIR = Path(__file__).parent
USERS_FILE = BASE_DIR / "users.json"
MATCH_HISTORY_FILE = BASE_DIR / "matches_history.json"
DB_FILE = BASE_DIR / "database.db"

rooms = {}
login_attempts = {}
viewer_presence = {}
room_locks = {}
room_locks_guard = threading.Lock()

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
DISPLAY_NAME_RE = re.compile(r"^[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ .'-]{1,24}$")
ALLOWED_PHASES = {"idle", "running", "paused", "round_end", "match_end", "rest"}
ALLOWED_STAGES = {"AMISTOSO", "OCTAVOS", "CUARTOS", "SEMIFINAL", "FINAL"}


def default_state(room_name="Ring"):
    return {
        "roomName": room_name,
        "redName": "Esquina Roja",
        "blueName": "Esquina Azul",
        "redScore": 0,
        "blueScore": 0,
        "redPenalties": 0,
        "bluePenalties": 0,
        "timeLeft": 120,
        "roundDuration": 120,
        "restDuration": 30,
        "running": False,
        "phase": "idle",
        "matchStage": "SEMIFINAL",
        "currentRound": 1,
        "redRoundsWon": 0,
        "blueRoundsWon": 0,
        "roundWinner": None,
        "matchWinner": None,
        "maxRounds": 3,
        "history": [],
        "resultRecorded": False,
        "revision": 0,
    }


def clamp_int(value, default, min_value, max_value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, parsed))


def sanitize_display_name(value, default):
    text = str(value or "").strip()
    if not text:
        return default
    if not DISPLAY_NAME_RE.fullmatch(text):
        return default
    return text[:24]


def sanitize_stage(value):
    stage = str(value or "").strip().upper()
    if stage in ALLOWED_STAGES:
        return stage
    return "SEMIFINAL"


def sanitize_history(history):
    if not isinstance(history, list):
        return []
    clean = []
    for item in history[-500:]:
        if not isinstance(item, dict):
            continue
        ts = str(item.get("ts", ""))[:8]
        text = str(item.get("text", ""))[:120]
        round_num = clamp_int(item.get("round"), 1, 1, 99)
        clock = str(item.get("clock", ""))[:5]
        phase = str(item.get("phase", ""))[:20]
        clean.append({"ts": ts, "text": text, "round": round_num, "clock": clock, "phase": phase})
    return clean


def sanitize_state(state):
    clean = dict(state)
    clean["redName"] = sanitize_display_name(clean.get("redName"), "Esquina Roja")
    clean["blueName"] = sanitize_display_name(clean.get("blueName"), "Esquina Azul")
    clean["phase"] = clean.get("phase") if clean.get("phase") in ALLOWED_PHASES else "idle"
    clean["matchStage"] = sanitize_stage(clean.get("matchStage"))
    clean["redScore"] = clamp_int(clean.get("redScore"), 0, 0, 999)
    clean["blueScore"] = clamp_int(clean.get("blueScore"), 0, 0, 999)
    clean["redPenalties"] = clamp_int(clean.get("redPenalties"), 0, 0, 99)
    clean["bluePenalties"] = clamp_int(clean.get("bluePenalties"), 0, 0, 99)
    clean["maxRounds"] = clamp_int(clean.get("maxRounds"), 3, 1, 9)
    clean["currentRound"] = clamp_int(clean.get("currentRound"), 1, 1, clean["maxRounds"])
    clean["redRoundsWon"] = clamp_int(clean.get("redRoundsWon"), 0, 0, clean["maxRounds"])
    clean["blueRoundsWon"] = clamp_int(clean.get("blueRoundsWon"), 0, 0, clean["maxRounds"])
    clean["timeLeft"] = clamp_int(clean.get("timeLeft"), 120, 0, 3600)
    clean["roundDuration"] = clamp_int(clean.get("roundDuration"), 120, 30, 3600)
    clean["restDuration"] = clamp_int(clean.get("restDuration"), 30, 10, 180)
    clean["running"] = bool(clean.get("running"))
    clean["resultRecorded"] = bool(clean.get("resultRecorded"))
    clean["history"] = sanitize_history(clean.get("history"))

    round_winner = clean.get("roundWinner")
    clean["roundWinner"] = round_winner if round_winner in {"red", "blue", None} else None
    match_winner = clean.get("matchWinner")
    clean["matchWinner"] = match_winner if match_winner in {"red", "blue", None} else None

    return clean


def normalize_state(data, room_name="Ring"):
    normalized = default_state(room_name=room_name)
    if isinstance(data, dict):
        normalized.update(data)
    normalized["roomName"] = room_name
    return sanitize_state(normalized)


def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','professor','spectator')),
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rooms (
                name TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS match_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room TEXT NOT NULL,
                winner TEXT NOT NULL,
                red_name TEXT NOT NULL,
                blue_name TEXT NOT NULL,
                red_score INTEGER NOT NULL,
                blue_score INTEGER NOT NULL,
                red_rounds_won INTEGER NOT NULL,
                blue_rounds_won INTEGER NOT NULL,
                total_points INTEGER NOT NULL,
                penalties INTEGER NOT NULL,
                date TEXT NOT NULL,
                timeline_json TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        # Upgrade path for old DBs without timeline_json
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(match_history)").fetchall()]
        if "timeline_json" not in cols:
            conn.execute("ALTER TABLE match_history ADD COLUMN timeline_json TEXT NOT NULL DEFAULT '[]'")
        conn.commit()


def load_users_from_json():
    if not USERS_FILE.exists():
        return []
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        users = data.get("users", [])
        if isinstance(users, list):
            return users
    except json.JSONDecodeError:
        pass
    return []


def migrate_initial_data():
    with get_db() as conn:
        users_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        rooms_count = conn.execute("SELECT COUNT(*) AS c FROM rooms").fetchone()["c"]
        history_count = conn.execute("SELECT COUNT(*) AS c FROM match_history").fetchone()["c"]

        if users_count == 0:
            source_users = load_users_from_json()
            if not source_users:
                source_users = [
                    {"username": "admin", "password": "admin123", "role": "admin"},
                    {"username": "profesor", "password": "profesor123", "role": "professor"},
                    {"username": "espectador", "password": "espectador123", "role": "spectator"},
                ]

            for user in source_users:
                username = str(user.get("username", "")).strip()
                role = str(user.get("role", "spectator")).strip()
                if not validate_username(username):
                    continue
                if role not in {"admin", "professor", "spectator"}:
                    role = "spectator"

                password_hash = user.get("password_hash")
                if not password_hash:
                    plain = str(user.get("password", ""))
                    if len(plain) < 1:
                        continue
                    password_hash = hash_password(plain)

                conn.execute(
                    "INSERT OR IGNORE INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                    (username, password_hash, role, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )

        if rooms_count == 0:
            # Crea Ring 1 por defecto para tener una sala inicial.
            default_room_name = "Ring 1"
            default_room_state = default_state(default_room_name)
            conn.execute(
                "INSERT OR IGNORE INTO rooms (name, state_json, updated_at) VALUES (?, ?, ?)",
                (
                    default_room_name,
                    json.dumps(default_room_state, ensure_ascii=False),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

        if history_count == 0 and MATCH_HISTORY_FILE.exists():
            try:
                history = json.loads(MATCH_HISTORY_FILE.read_text(encoding="utf-8"))
                if isinstance(history, list):
                    for item in history:
                        if not isinstance(item, dict):
                            continue
                        conn.execute(
                            """
                            INSERT INTO match_history (
                                room, winner, red_name, blue_name, red_score, blue_score,
                                red_rounds_won, blue_rounds_won, total_points, penalties, date, timeline_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(item.get("room", "")),
                                str(item.get("winner", "")),
                                str(item.get("redName", "")),
                                str(item.get("blueName", "")),
                                clamp_int(item.get("redScore"), 0, 0, 999),
                                clamp_int(item.get("blueScore"), 0, 0, 999),
                                clamp_int(item.get("redRoundsWon"), 0, 0, 9),
                                clamp_int(item.get("blueRoundsWon"), 0, 0, 9),
                                clamp_int(item.get("totalPoints"), 0, 0, 9999),
                                clamp_int(item.get("penalties"), 0, 0, 999),
                                str(item.get("date", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))),
                                json.dumps(item.get("timeline", []), ensure_ascii=False),
                            ),
                        )
            except json.JSONDecodeError:
                pass

        conn.commit()


def load_users():
    with get_db() as conn:
        rows = conn.execute("SELECT username, password_hash, role FROM users").fetchall()
        return [dict(row) for row in rows]


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def verify_password(user, password):
    stored_hash = user.get("password_hash")
    if stored_hash:
        try:
            salt, stored_digest = stored_hash.split("$", 1)
        except ValueError:
            return False
        digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
        return hmac.compare_digest(digest, stored_digest)

    legacy_password = user.get("password")
    if legacy_password is None:
        return False
    return hmac.compare_digest(str(legacy_password), str(password))


def authenticate(username, password):
    users = load_users()

    for user in users:
        if user.get("username") == username and verify_password(user, password):
            # Migra cuentas legacy con password plano.
            if user.get("password") is not None and user.get("password_hash") is None:
                user["password_hash"] = hash_password(password)
                user.pop("password", None)
                save_users(users)
            return {
                "username": user.get("username"),
                "role": user.get("role", "spectator"),
            }
    return None


def save_users(users):
    with get_db() as conn:
        conn.execute("DELETE FROM users")
        for user in users:
            username = str(user.get("username", "")).strip()
            role = str(user.get("role", "spectator")).strip()
            password_hash = str(user.get("password_hash", "")).strip()
            if not validate_username(username):
                continue
            if role not in {"admin", "professor", "spectator"}:
                continue
            if not password_hash:
                continue
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (username, password_hash, role, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
        conn.commit()


def get_current_user():
    username = session.get("username")
    role = session.get("role")

    if not username or not role:
        return None

    return {"username": username, "role": role}


def is_rate_limited(identifier, max_attempts=10, window_seconds=300):
    now = time.time()
    timestamps = login_attempts.get(identifier, [])
    timestamps = [ts for ts in timestamps if now - ts <= window_seconds]
    login_attempts[identifier] = timestamps
    return len(timestamps) >= max_attempts


def register_failed_attempt(identifier):
    now = time.time()
    timestamps = login_attempts.get(identifier, [])
    timestamps.append(now)
    login_attempts[identifier] = timestamps[-20:]


def clear_attempts(identifier):
    login_attempts.pop(identifier, None)


def validate_username(username):
    return bool(USERNAME_RE.fullmatch(username or ""))


def same_origin(url):
    if not url:
        return True
    try:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        expected = request.host_url.rstrip("/")
        return origin == expected
    except Exception:
        return False


@app.before_request
def security_checks():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("Origin")
        referer = request.headers.get("Referer")
        if not same_origin(origin) or not same_origin(referer):
            return jsonify({"ok": False, "error": "bad_origin"}), 403


@app.after_request
def set_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return resp


def require_auth(roles=None, api=False):
    allowed_roles = set(roles or [])

    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            user = get_current_user()

            if not user:
                if api:
                    return jsonify({"ok": False, "error": "unauthorized"}), 401
                return redirect("/login")

            if allowed_roles and user["role"] not in allowed_roles:
                if api:
                    return jsonify({"ok": False, "error": "forbidden"}), 403
                return send_file("forbidden.html"), 403

            return fn(*args, **kwargs)

        return wrapped

    return decorator


def ensure_room(room):
    with get_db() as conn:
        row = conn.execute("SELECT state_json FROM rooms WHERE name = ?", (room,)).fetchone()
        if row is None:
            state = default_state(room_name=room)
            conn.execute(
                "INSERT INTO rooms (name, state_json, updated_at) VALUES (?, ?, ?)",
                (
                    room,
                    json.dumps(state, ensure_ascii=False),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()
            rooms[room] = state
            return

        try:
            state = json.loads(row["state_json"])
        except json.JSONDecodeError:
            state = default_state(room_name=room)

        rooms[room] = normalize_state(state, room_name=room)


def save_room(room):
    if room not in rooms:
        return
    rooms[room] = normalize_state(rooms[room], room_name=room)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO rooms (name, state_json, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (
                room,
                json.dumps(rooms[room], ensure_ascii=False),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()


def delete_room_from_db(room):
    with get_db() as conn:
        conn.execute("DELETE FROM rooms WHERE name = ?", (room,))
        conn.commit()


def load_all_rooms():
    loaded = {}
    with get_db() as conn:
        rows = conn.execute("SELECT name, state_json FROM rooms").fetchall()
    for row in rows:
        room_name = row["name"]
        try:
            raw_state = json.loads(row["state_json"])
        except json.JSONDecodeError:
            raw_state = default_state(room_name=room_name)
        loaded[room_name] = normalize_state(raw_state, room_name=room_name)
    return loaded


def load_match_history():
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT room, winner, red_name, blue_name, red_score, blue_score,
                   red_rounds_won, blue_rounds_won, total_points, penalties, date, timeline_json
            FROM match_history
            ORDER BY id ASC
            """
        ).fetchall()

    return [
        {
            "room": row["room"],
            "winner": row["winner"],
            "redName": row["red_name"],
            "blueName": row["blue_name"],
            "redScore": row["red_score"],
            "blueScore": row["blue_score"],
            "redRoundsWon": row["red_rounds_won"],
            "blueRoundsWon": row["blue_rounds_won"],
            "totalPoints": row["total_points"],
            "penalties": row["penalties"],
            "date": row["date"],
            "timeline": json.loads(row["timeline_json"]) if row["timeline_json"] else [],
        }
        for row in rows
    ]


def save_match_row(item):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO match_history (
                room, winner, red_name, blue_name, red_score, blue_score,
                red_rounds_won, blue_rounds_won, total_points, penalties, date, timeline_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(item.get("room", "")),
                str(item.get("winner", "")),
                str(item.get("redName", "")),
                str(item.get("blueName", "")),
                clamp_int(item.get("redScore"), 0, 0, 999),
                clamp_int(item.get("blueScore"), 0, 0, 999),
                clamp_int(item.get("redRoundsWon"), 0, 0, 9),
                clamp_int(item.get("blueRoundsWon"), 0, 0, 9),
                clamp_int(item.get("totalPoints"), 0, 0, 9999),
                clamp_int(item.get("penalties"), 0, 0, 999),
                str(item.get("date", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))),
                json.dumps(item.get("timeline", []), ensure_ascii=False),
            ),
        )
        conn.commit()


def maybe_record_match(room, state):
    if state.get("phase") != "match_end":
        return
    if state.get("resultRecorded"):
        return

    if state.get("redRoundsWon", 0) > state.get("blueRoundsWon", 0):
        winner = state.get("redName", "Rojo")
    elif state.get("blueRoundsWon", 0) > state.get("redRoundsWon", 0):
        winner = state.get("blueName", "Azul")
    elif state.get("redScore", 0) > state.get("blueScore", 0):
        winner = state.get("redName", "Rojo")
    elif state.get("blueScore", 0) > state.get("redScore", 0):
        winner = state.get("blueName", "Azul")
    else:
        winner = "Empate"

    save_match_row(
        {
            "room": room,
            "winner": winner,
            "redName": state.get("redName", "Rojo"),
            "blueName": state.get("blueName", "Azul"),
            "redScore": state.get("redScore", 0),
            "blueScore": state.get("blueScore", 0),
            "redRoundsWon": state.get("redRoundsWon", 0),
            "blueRoundsWon": state.get("blueRoundsWon", 0),
            "totalPoints": state.get("redScore", 0) + state.get("blueScore", 0),
            "penalties": state.get("redPenalties", 0) + state.get("bluePenalties", 0),
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "timeline": state.get("history", []),
        }
    )
    state["resultRecorded"] = True


def push_history(state, text):
    history = state.get("history")
    if not isinstance(history, list):
        history = []
    moment_clock = f"{clamp_int(state.get('timeLeft'), 0, 0, 3600) // 60}:{str(clamp_int(state.get('timeLeft'), 0, 0, 3600) % 60).zfill(2)}"
    history.append(
        {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "text": text,
            "round": clamp_int(state.get("currentRound"), 1, 1, 99),
            "clock": moment_clock,
            "phase": str(state.get("phase", ""))[:20],
        }
    )
    state["history"] = history[-500:]


def next_ring_name():
    max_ring = 0
    for name in rooms.keys():
        match = re.fullmatch(r"Ring\s+(\d+)", name)
        if not match:
            continue
        max_ring = max(max_ring, int(match.group(1)))
    return f"Ring {max_ring + 1}"


def ring_status(state):
    phase = state.get("phase")
    if phase == "running":
        return "combate activo"
    if phase == "match_end":
        return "terminado"
    return "esperando"


def get_room_lock(room):
    with room_locks_guard:
        lock = room_locks.get(room)
        if lock is None:
            lock = threading.RLock()
            room_locks[room] = lock
        return lock


def get_latest_match_for_room(room):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT room, winner, red_name, blue_name, red_score, blue_score,
                   red_rounds_won, blue_rounds_won, total_points, penalties, date, timeline_json
            FROM match_history
            WHERE room = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (room,),
        ).fetchone()
    if not row:
        return None
    return {
        "room": row["room"],
        "winner": row["winner"],
        "redName": row["red_name"],
        "blueName": row["blue_name"],
        "redScore": row["red_score"],
        "blueScore": row["blue_score"],
        "redRoundsWon": row["red_rounds_won"],
        "blueRoundsWon": row["blue_rounds_won"],
        "totalPoints": row["total_points"],
        "penalties": row["penalties"],
        "date": row["date"],
        "timeline": json.loads(row["timeline_json"]) if row["timeline_json"] else [],
    }


def build_simple_pdf(lines):
    # PDF minimalista de texto, sin dependencias externas.
    chunks = []
    for line in lines:
        wrapped = textwrap.wrap(str(line), width=92) or [""]
        chunks.extend(wrapped)

    lines_per_page = 48
    pages_content = [chunks[i:i + lines_per_page] for i in range(0, len(chunks), lines_per_page)] or [[""]]

    objects = []

    def add_obj(obj_bytes):
        objects.append(obj_bytes)
        return len(objects)

    font_id = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_obj_ids = []
    content_obj_ids = []

    for page_lines in pages_content:
        content_lines = [b"BT", b"/F1 11 Tf", b"14 TL", b"36 770 Td"]
        for i, text in enumerate(page_lines):
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            if i == 0:
                content_lines.append(f"({escaped}) Tj".encode("latin-1", errors="replace"))
            else:
                content_lines.append(b"T*")
                content_lines.append(f"({escaped}) Tj".encode("latin-1", errors="replace"))
        content_lines.append(b"ET")
        content_stream = b"\n".join(content_lines)
        content_obj = b"<< /Length " + str(len(content_stream)).encode("ascii") + b" >>\nstream\n" + content_stream + b"\nendstream"
        content_id = add_obj(content_obj)
        content_obj_ids.append(content_id)

        page_obj = (
            b"<< /Type /Page /Parent __PAGES__ 0 R "
            b"/MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 " + str(font_id).encode("ascii") + b" 0 R >> >> "
            b"/Contents " + str(content_id).encode("ascii") + b" 0 R >>"
        )
        page_id = add_obj(page_obj)
        page_obj_ids.append(page_id)

    kids = b" ".join([str(pid).encode("ascii") + b" 0 R" for pid in page_obj_ids])
    pages_id = add_obj(b"<< /Type /Pages /Count " + str(len(page_obj_ids)).encode("ascii") + b" /Kids [ " + kids + b" ] >>")

    for idx, obj in enumerate(objects):
        if b"__PAGES__" in obj:
            objects[idx] = obj.replace(b"__PAGES__", str(pages_id).encode("ascii"))

    catalog_id = add_obj(b"<< /Type /Catalog /Pages " + str(pages_id).encode("ascii") + b" 0 R >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]

    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode("ascii"))
        out.write(obj)
        out.write(b"\nendobj\n")

    xref_offset = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode("ascii"))

    out.write(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF"
        ).encode("ascii")
    )
    return out.getvalue()


def cleanup_presence(ttl_seconds=35):
    now = time.time()
    empty_rooms = []
    for room, users in viewer_presence.items():
        stale_users = [username for username, ts in users.items() if now - ts > ttl_seconds]
        for username in stale_users:
            users.pop(username, None)
        if not users:
            empty_rooms.append(room)
    for room in empty_rooms:
        viewer_presence.pop(room, None)


def register_viewer(room, username):
    cleanup_presence()
    room_presence = viewer_presence.setdefault(room, {})
    room_presence[username] = time.time()


def get_room_viewers(room):
    cleanup_presence()
    room_presence = viewer_presence.get(room, {})
    users = sorted(room_presence.keys())
    return {"count": len(users), "users": users}


def initialize_storage():
    init_db()
    migrate_initial_data()
    rooms.clear()
    rooms.update(load_all_rooms())


initialize_storage()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if get_current_user():
            return redirect("/")
        return send_file("login.html")

    data = request.get_json(silent=True) or request.form
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    ip = request.remote_addr or "unknown"
    if is_rate_limited(f"login:{ip}"):
        return jsonify({"ok": False, "error": "too_many_attempts"}), 429

    user = authenticate(username, password)
    if not user:
        register_failed_attempt(f"login:{ip}")
        return jsonify({"ok": False, "error": "invalid_credentials"}), 401

    clear_attempts(f"login:{ip}")
    session["username"] = user["username"]
    session["role"] = user["role"]
    session.permanent = True

    return jsonify({"ok": True, "user": user})


@app.route("/register", methods=["GET", "POST"])
def register():
    user = get_current_user()
    if not user:
        return redirect("/login")
    if user["role"] != "admin":
        return send_file("forbidden.html"), 403

    if request.method == "GET":
        return send_file("register.html")

    data = request.get_json(silent=True) or request.form
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    role = (data.get("role") or "").strip()

    if role not in {"admin", "professor"}:
        return jsonify({"ok": False, "error": "invalid_role"}), 400
    if not validate_username(username) or len(password) < 8:
        return jsonify({"ok": False, "error": "invalid_input"}), 400

    users = load_users()
    if any(u.get("username") == username for u in users):
        return jsonify({"ok": False, "error": "username_exists"}), 409

    users.append({"username": username, "password_hash": hash_password(password), "role": role})
    save_users(users)
    return jsonify({"ok": True})


@app.route("/register_spectator", methods=["GET", "POST"])
def register_spectator():
    if request.method == "GET":
        return send_file("register_spectator.html")

    data = request.get_json(silent=True) or request.form
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    ip = request.remote_addr or "unknown"
    if is_rate_limited(f"register_spectator:{ip}", max_attempts=15, window_seconds=300):
        return jsonify({"ok": False, "error": "too_many_attempts"}), 429

    if not validate_username(username) or len(password) < 8:
        register_failed_attempt(f"register_spectator:{ip}")
        return jsonify({"ok": False, "error": "invalid_input"}), 400

    users = load_users()
    if any(u.get("username") == username for u in users):
        register_failed_attempt(f"register_spectator:{ip}")
        return jsonify({"ok": False, "error": "username_exists"}), 409

    users.append({"username": username, "password_hash": hash_password(password), "role": "spectator"})
    save_users(users)
    clear_attempts(f"register_spectator:{ip}")
    return jsonify({"ok": True})


@app.route("/logout", methods=["POST"])
@require_auth(api=True)
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/me")
def me():
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return jsonify({"ok": True, "user": user})


@app.route("/")
@require_auth()
def index():
    return send_file("index.html")


@app.route("/rooms")
@require_auth(api=True)
def get_rooms():
    current_user = get_current_user()

    def room_sort_key(name):
        match = re.fullmatch(r"Ring\s+(\d+)", name)
        if not match:
            return (1, name)
        return (0, int(match.group(1)))

    room_items = []
    for room in sorted(rooms.keys(), key=room_sort_key):
        ensure_room(room)
        state = rooms[room]
        viewers = get_room_viewers(room)
        room_items.append(
            {
                "room": room,
                "redName": state["redName"],
                "blueName": state["blueName"],
                "status": ring_status(state),
                "phase": state.get("phase", "idle"),
                "spectatorCount": viewers["count"],
                "spectatorUsers": viewers["users"] if current_user and current_user.get("role") == "admin" else [],
            }
        )

    return jsonify(room_items)


@app.route("/create_room", methods=["POST"])
@require_auth(roles={"admin", "professor"}, api=True)
def create_room():
    with room_locks_guard:
        room_name = next_ring_name()
        with get_room_lock(room_name):
            rooms[room_name] = default_state(room_name=room_name)
            save_room(room_name)
    return jsonify({"ok": True, "room": room_name})


@app.route("/delete_room/<path:room>", methods=["POST"])
@require_auth(roles={"admin"}, api=True)
def delete_room(room):
    with get_room_lock(room):
        if room in rooms:
            rooms.pop(room)
            delete_room_from_db(room)
            return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "not_found"}), 404


@app.route("/state/<path:room>")
@require_auth(api=True)
def get_state(room):
    with get_room_lock(room):
        ensure_room(room)
        return jsonify(rooms[room])


@app.route("/update/<path:room>", methods=["POST"])
@require_auth(roles={"admin", "professor"}, api=True)
def update_state(room):
    with get_room_lock(room):
        ensure_room(room)

        data = request.json or {}
        current = normalize_state(rooms.get(room), room_name=room)
        incoming = normalize_state(data, room_name=room)

        incoming_revision = incoming.get("revision")
        if incoming_revision != current["revision"]:
            return jsonify({"ok": False, "error": "revision_conflict", "state": current}), 409

        incoming["revision"] = current["revision"] + 1
        maybe_record_match(room, incoming)

        rooms[room] = incoming
        save_room(room)

        return jsonify({"ok": True, "state": incoming})


@app.route("/action/<path:room>", methods=["POST"])
@require_auth(roles={"admin", "professor"}, api=True)
def apply_action(room):
    with get_room_lock(room):
        ensure_room(room)

        payload = request.json or {}
        action_type = payload.get("type")
        user = get_current_user()

        current = normalize_state(rooms.get(room), room_name=room)

        if action_type == "add_score":
            side = payload.get("side")
            pts = int(payload.get("pts", 0))

            if side == "red":
                current["redScore"] = max(0, current["redScore"] + pts)
                push_history(current, f"ROJO {'+' if pts >= 0 else ''}{pts}")
            elif side == "blue":
                current["blueScore"] = max(0, current["blueScore"] + pts)
                push_history(current, f"AZUL {'+' if pts >= 0 else ''}{pts}")
        elif action_type == "add_penalty":
            side = payload.get("side")

            if side == "red":
                current["redPenalties"] += 1
                current["blueScore"] += 1
                push_history(current, "PENALIZACION ROJO ( +1 AZUL )")
            elif side == "blue":
                current["bluePenalties"] += 1
                current["redScore"] += 1
                push_history(current, "PENALIZACION AZUL ( +1 ROJO )")
        elif action_type == "set_names":
            red_name = (payload.get("redName") or "").strip()
            blue_name = (payload.get("blueName") or "").strip()

            if red_name:
                if not DISPLAY_NAME_RE.fullmatch(red_name):
                    return jsonify({"ok": False, "error": "invalid_red_name"}), 400
                current["redName"] = red_name[:24]
            if blue_name:
                if not DISPLAY_NAME_RE.fullmatch(blue_name):
                    return jsonify({"ok": False, "error": "invalid_blue_name"}), 400
                current["blueName"] = blue_name[:24]
            push_history(current, "NOMBRES ACTUALIZADOS")
        elif action_type == "set_rest_duration":
            if not user or user.get("role") != "admin":
                return jsonify({"ok": False, "error": "forbidden"}), 403

            value = payload.get("seconds")
            try:
                seconds = int(value)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "invalid_rest_duration"}), 400

            if seconds < 10 or seconds > 180:
                return jsonify({"ok": False, "error": "invalid_rest_duration"}), 400

            current["restDuration"] = seconds
            push_history(current, f"DESCANSO CONFIGURADO A {seconds}s")
        else:
            return jsonify({"ok": False, "error": "invalid_action"}), 400

        maybe_record_match(room, current)
        current["revision"] += 1
        rooms[room] = current
        save_room(room)
        return jsonify(current)


@app.route("/match_history")
@require_auth(api=True)
def match_history():
    history = load_match_history()
    return jsonify(history[::-1])


@app.route("/report/<path:room>.pdf")
@require_auth(roles={"admin", "professor"})
def report_pdf(room):
    with get_room_lock(room):
        ensure_room(room)
        state = rooms[room]
        latest = get_latest_match_for_room(room)

    if latest:
        red_name = latest.get("redName", state.get("redName"))
        blue_name = latest.get("blueName", state.get("blueName"))
        red_score = latest.get("redScore", state.get("redScore"))
        blue_score = latest.get("blueScore", state.get("blueScore"))
        red_rounds = latest.get("redRoundsWon", state.get("redRoundsWon"))
        blue_rounds = latest.get("blueRoundsWon", state.get("blueRoundsWon"))
        winner = latest.get("winner", "N/A")
        report_date = latest.get("date", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        timeline = latest.get("timeline", [])
    else:
        red_name = state.get("redName")
        blue_name = state.get("blueName")
        red_score = state.get("redScore")
        blue_score = state.get("blueScore")
        red_rounds = state.get("redRoundsWon")
        blue_rounds = state.get("blueRoundsWon")
        winner = "N/A"
        report_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        timeline = state.get("history", [])

    lines = [
        "REPORTE DE COMBATE WT",
        f"Ring: {room}",
        f"Fecha: {report_date}",
        f"Etapa: {state.get('matchStage', 'SEMIFINAL')}",
        "",
        "Competidores:",
        f"  Rojo: {red_name}",
        f"  Azul: {blue_name}",
        "",
        "Resultado:",
        f"  Puntaje Final: {red_score} - {blue_score}",
        f"  Asaltos Ganados: {red_rounds} - {blue_rounds}",
        f"  Ganador: {winner}",
        "",
        "Timeline de acciones (asalto / reloj / hora):",
    ]

    if timeline:
        for item in timeline:
            if not isinstance(item, dict):
                continue
            round_num = item.get("round", "?")
            clock = item.get("clock", "--:--")
            ts = item.get("ts", "--:--:--")
            text = item.get("text", "")
            lines.append(f"  R{round_num} {clock} [{ts}] {text}")
    else:
        lines.append("  Sin acciones registradas.")

    pdf_bytes = build_simple_pdf(lines)
    filename = f"reporte_{room.replace(' ', '_')}.pdf"
    return app.response_class(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename=\"{filename}\"'},
    )


@app.route("/dashboard")
@require_auth(api=True)
def dashboard():
    history = load_match_history()

    active = 0
    waiting = 0
    finished = 0
    live_penalties = 0
    room_states = []
    total_spectators = 0

    for room_name in rooms.keys():
        ensure_room(room_name)
        st = rooms[room_name]
        status = ring_status(st)
        if status == "combate activo":
            active += 1
        elif status == "terminado":
            finished += 1
        else:
            waiting += 1

        live_penalties += st.get("redPenalties", 0) + st.get("bluePenalties", 0)
        spectators = get_room_viewers(room_name)
        total_spectators += spectators["count"]
        room_states.append({"room": room_name, "status": status, "spectatorCount": spectators["count"]})

    total_matches = len(history)
    avg_points = round(
        (sum(item.get("totalPoints", 0) for item in history) / total_matches) if total_matches else 0,
        2,
    )
    total_penalties = sum(item.get("penalties", 0) for item in history) + live_penalties

    return jsonify(
        {
            "activeRings": active,
            "waitingRings": waiting,
            "finishedRings": finished,
            "totalMatches": total_matches,
            "avgPoints": avg_points,
            "penalties": total_penalties,
            "totalSpectators": total_spectators,
            "rings": room_states,
        }
    )


@app.route("/presence/<path:room>", methods=["POST"])
@require_auth(api=True)
def presence(room):
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    with get_room_lock(room):
        ensure_room(room)
        register_viewer(room, user["username"])
        viewers = get_room_viewers(room)
        return jsonify({"ok": True, "count": viewers["count"]})


@app.route("/bracket_data")
@require_auth(api=True)
def bracket_data():
    history = load_match_history()
    recent = history[-12:]

    quarterfinal = recent[-8:-4] if len(recent) >= 8 else recent[:4]
    semifinal = recent[-4:-2] if len(recent) >= 4 else recent[4:6]
    final = recent[-1:] if recent else []

    return jsonify(
        {
            "quarterfinal": quarterfinal,
            "semifinal": semifinal,
            "final": final,
        }
    )


@app.route("/score/<path:room>")
@require_auth(roles={"admin", "professor"})
def scoreboard(room):
    return send_file("scoreboard.html")


@app.route("/spectator/<path:room>")
@require_auth()
def spectator(room):
    return send_file("spectator.html")


@app.route("/red/<path:room>")
@require_auth(roles={"admin", "professor"})
def red(room):
    return send_file("red.html")


@app.route("/blue/<path:room>")
@require_auth(roles={"admin", "professor"})
def blue(room):
    return send_file("blue.html")


@app.route("/tv/<path:room>")
@require_auth()
def tv(room):
    return send_file("tv.html")


@app.route("/tournament")
@require_auth(roles={"admin", "professor"})
def tournament():
    return send_file("tournament.html")


@app.route("/forbidden")
def forbidden():
    return send_file("forbidden.html"), 403


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1")
