"""Supervise creator-owned listeners and deliver optional operator notifications."""
from contextlib import closing
from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from . import engine

CREDENTIALS = {
    "twitch": ("TWITCH_BOT_LOGIN", "TWITCH_OAUTH_TOKEN"),
    "youtube": ("YOUTUBE_API_KEY",),
    "chaturbate": ("CHATURBATE_TOKEN",),
}
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,95}")
last_pulse = None


def pulse():
    """Record upstream progress at most once per three seconds in a fleet worker."""
    global last_pulse
    target = os.getenv("SOCIALBOT_HEARTBEAT")
    now = time.monotonic()
    if target and (last_pulse is None or now - last_pulse >= 3):
        Path(target).touch(mode=0o600)
        last_pulse = now


def register(subparsers):
    fleet = subparsers.add_parser("fleet", help="Supervise creator room listeners")
    fleet.add_argument("--config", type=Path, default=Path("fleet.json"))
    fleet.add_argument("--check", action="store_true", help="Validate config without connecting or starting workers")
    subparsers.add_parser("fleet-status", help="Show local worker and alert delivery status")


def load_config(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) - {"rooms", "alerts", "watchdog_seconds"}:
        raise ValueError("Unknown fleet setting. Expected rooms, alerts and watchdog_seconds.")
    rooms = raw.get("rooms")
    if not isinstance(rooms, list) or not 1 <= len(rooms) <= 32:
        raise ValueError("Fleet needs between 1 and 32 rooms.")
    seen_names, seen_rooms = set(), set()
    for room in rooms:
        if not isinstance(room, dict) or set(room) - {"name", "platform", "channel", "live_chat_id", "env", "auto_reply"}:
            raise ValueError("Invalid fleet room fields.")
        name, platform, channel = (room.get(k) for k in ("name", "platform", "channel"))
        if not isinstance(name, str) or not ALIAS.fullmatch(name):
            raise ValueError("Give every room a short alphanumeric alias.")
        if not isinstance(platform, str) or platform not in CREDENTIALS:
            raise ValueError(f"Unsupported listener for {name}.")
        channel = engine.field(channel, "channel", 128)
        room["channel"] = channel
        if platform != "youtube" and not re.fullmatch(r"[A-Za-z0-9_]+",channel):
            raise ValueError(f"Invalid channel login for {name}.")
        if channel.startswith("-"):
            raise ValueError(f"Invalid channel label for {name}.")
        if name.casefold() in seen_names or (platform, channel.casefold()) in seen_rooms:
            raise ValueError("Duplicate fleet alias or platform/channel.")
        seen_names.add(name.casefold())
        seen_rooms.add((platform, channel.casefold()))
        if platform == "youtube":
            room["live_chat_id"] = engine.field(room.get("live_chat_id"), "live_chat_id", 512)
        elif "live_chat_id" in room:
            raise ValueError("Only YouTube accepts live_chat_id.")
        auto = room.get("auto_reply", False)
        if type(auto) is not bool or (auto and platform != "twitch"):
            raise ValueError("auto_reply takes a boolean and is supported on Twitch only.")
        required = CREDENTIALS[platform] + (("TWITCH_OWNER_TOKEN",) if auto else ())
        mapping = room.get("env", {})
        if not isinstance(mapping, dict) or set(mapping) - set(required):
            raise ValueError(f"Unknown credential names in {name}.")
        if any(not isinstance(v,str) or not ENV_NAME.fullmatch(v) for v in mapping.values()):
            raise ValueError("Use environment variable names in fleet.json, never their values.")
    watchdog = raw.get("watchdog_seconds", 600)
    if type(watchdog) is not int or not 120 <= watchdog <= 86400:
        raise ValueError("watchdog_seconds must be an integer from 120 to 86400.")
    alerts = raw.get("alerts", {})
    if not isinstance(alerts, dict) or set(alerts) - {"destinations","events"}:
        raise ValueError("Invalid alerts settings.")
    for key, options in (("destinations", ("discord","telegram")), ("events", ("gift","returning_supporter","error"))):
        value = alerts.get(key, [])
        if not isinstance(value, list) or any(v not in options for v in value) or len(set(value)) != len(value):
            raise ValueError(f"Invalid alerts {key}.")
    if bool(alerts.get("destinations")) != bool(alerts.get("events")):
        raise ValueError("Configure both alert events and destinations, or leave both empty.")
    return rooms, watchdog, alerts


def worker_env(room):
    # Forward only runtime basics and the credentials selected for this room.
    result = {k: os.environ[k] for k in ("HOME","PATH","LANG","LC_ALL","SYSTEMROOT",
               "SSL_CERT_FILE","SSL_CERT_DIR","PYTHONUTF8","PYTHONDONTWRITEBYTECODE") if k in os.environ}
    required = CREDENTIALS[room["platform"]] + (("TWITCH_OWNER_TOKEN",) if room.get("auto_reply") else ())
    for key in required:
        alias = room.get("env", {}).get(key, key)
        if not os.getenv(alias):
            raise ValueError(f"{room['name']} needs environment variable {alias}.")
        result[key] = os.environ[alias]
    return result


def _destination(name):
    if name == "telegram":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat = os.getenv("TELEGRAM_CHAT_ID", "")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(r"-?[0-9]+|@[A-Za-z0-9_]+", chat):
            raise ValueError("Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return "api.telegram.org", f"/bot{token}/sendMessage", {"chat_id":chat,"protect_content":True}
    try:
        url = urlsplit(os.getenv("DISCORD_WEBHOOK_URL", ""))
    except ValueError:
        raise ValueError("Supply a valid DISCORD_WEBHOOK_URL.") from None
    if (url.scheme != "https" or url.netloc != "discord.com" or url.query or url.fragment or
            not re.fullmatch(r"/api/(?:v[0-9]+/)?webhooks/[0-9]+/[A-Za-z0-9._-]+", url.path)):
        raise ValueError("Supply an HTTPS discord.com webhook URL in DISCORD_WEBHOOK_URL.")
    return "discord.com", url.path + "?wait=true", {"allowed_mentions":{"parse":[]}}


def _deliver(destination, message):
    host, path, body = _destination(destination)
    body["content" if destination == "discord" else "text"] = message
    client = http.client.HTTPSConnection(host, timeout=5)
    try:
        client.request("POST", path, json.dumps(body), {"Content-Type":"application/json", "User-Agent":"SocialBot/1"})
        response = client.getresponse()
        if response.status == 429:
            try:
                details = json.loads(response.read(65537))
                delay = (details.get("parameters", {}).get("retry_after") if destination == "telegram"
                         else details.get("retry_after"))
                if type(delay) not in (int, float):
                    delay = 30
                return max(1, min(86400, int(delay) + 1))
            except (ValueError, OverflowError, TypeError, AttributeError):
                return 30
        if response.status != 200:
            return "failed" if 400 <= response.status < 500 else "uncertain"
        receipt = json.loads(response.read(65537))
        if destination == "telegram":
            ok = isinstance(receipt, dict) and receipt.get("ok") is True
        else:
            ok = isinstance(receipt, dict) and bool(receipt.get("id"))
        return "sent" if ok else "uncertain"
    except (OSError, ValueError, RecursionError, http.client.HTTPException):
        return "uncertain"
    finally:
        client.close()


def state_dir(db_path):
    return Path(str(Path(db_path).resolve()) + ".fleet")


def _open_state(path):
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    return db


def _init_state(path):
    db = _open_state(path)
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS workers(name TEXT PRIMARY KEY, platform TEXT, state TEXT,
            pid INTEGER, restarts INTEGER, heartbeat_at INTEGER, updated_at INTEGER);
        CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, destination TEXT, key TEXT,
            message TEXT, state TEXT DEFAULT 'pending', ready_at INTEGER DEFAULT 0,
            retries INTEGER DEFAULT 0, UNIQUE(destination,key));
        CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT);
        CREATE TABLE IF NOT EXISTS incidents(name TEXT PRIMARY KEY, incident TEXT);
        CREATE TABLE IF NOT EXISTS cursor(id INTEGER PRIMARY KEY CHECK(id=1), last_event INTEGER);
    """)
    return db


def dashboard_status(db_path):
    path = state_dir(db_path) / "status.sqlite3"
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=1)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workers'").fetchone():
            return []
        rows = [dict(r) for r in db.execute("SELECT * FROM workers ORDER BY name")]
    for row in rows:
        if row["state"] not in ("stopped","ended") and time.time()-row["updated_at"] > 30:
            row.update(state="supervisor stale", pid=None)
    return rows


def _queue_alert(db, alerts, kind, identity, room):
    if kind not in alerts.get("events", ()):
        return
    key = hashlib.sha256(json.dumps([kind,*identity], separators=(",",":")).encode()).hexdigest()
    label = {"gift":"Gift received","returning_supporter":"Returning supporter entered",
             "error":"Listener interrupted"}[kind]
    message = f"{label} · {room['name']} ({room['platform']})"
    db.executemany("INSERT OR IGNORE INTO alerts(destination,key,message) VALUES(?,?,?)",
                   ((dest,key,message) for dest in alerts["destinations"]))


def _resume_alerts(state, events, rooms, alerts):
    # Preserve pending work only for the same rooms, events, and recipients. Store
    # a digest only; a changed webhook or chat ID must not receive an old queue.
    config = {"rooms":sorted((r["name"],r["platform"],r["channel"].casefold()) for r in rooms),
              "events":sorted(alerts.get("events", ())),
              "targets":[_destination(d) for d in sorted(alerts.get("destinations", ()))]}
    fingerprint = hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()
    prior = state.execute("SELECT fingerprint FROM subscriptions WHERE id=1").fetchone()
    with state:
        state.execute("UPDATE alerts SET state='uncertain' WHERE state='attempting'")
        if prior is None or prior[0] != fingerprint:
            latest = events.execute("SELECT coalesce(max(rowid),0) FROM events").fetchone()[0]
            state.execute("INSERT OR REPLACE INTO cursor VALUES(1,?)",(latest,))
            state.execute("INSERT OR REPLACE INTO subscriptions VALUES(1,?)",(fingerprint,))
            state.execute("UPDATE alerts SET state='expired' WHERE state='pending'")
            state.execute("DELETE FROM incidents")


def _capture_gifts(state, events, rooms, alerts):
    index = {(r["platform"],r["channel"].casefold()):r for r in rooms}
    with state:
        cursor = state.execute("SELECT last_event FROM cursor WHERE id=1").fetchone()[0]
        batch = events.execute("""SELECT rowid,platform,channel,event_id,kind,origin FROM events
                                  WHERE rowid>? ORDER BY rowid LIMIT 500""",(cursor,)).fetchall()
        for row in batch:
            rid, platform, channel, eid, kind, origin = row
            room = index.get((platform,channel))
            if room and kind == "gift" and origin == platform+"-live":
                _queue_alert(state, alerts, "gift", (platform,channel,eid), room)
            if (room and kind == "join" and origin == platform+"-live"
                    and "returning_supporter" in alerts.get("events",())):
                earlier = events.execute("""SELECT 1 FROM events j JOIN events g
                  ON g.platform=j.platform AND g.channel=j.channel AND g.viewer_key=j.viewer_key
                  WHERE j.rowid=? AND g.kind='gift' AND g.origin=j.origin AND g.rowid<j.rowid LIMIT 1""",
                  (rid,)).fetchone()
                if earlier:
                    _queue_alert(state, alerts, "returning_supporter", (platform,channel,eid), room)
        if batch:
            state.execute("UPDATE cursor SET last_event=? WHERE id=1", (batch[-1][0],))


def _dispatch_one(db, destinations, deliver=_deliver):
    if not destinations:
        return False
    with db:
        db.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in destinations)
        row = db.execute(f"SELECT id,destination,message FROM alerts WHERE state='pending' AND ready_at<=? AND destination IN ({placeholders}) ORDER BY id LIMIT 1", (int(time.time()),*destinations)).fetchone()
        if row is None:
            return False
        db.execute("UPDATE alerts SET state='attempting' WHERE id=?", (row[0],))
    status = deliver(row[1], row[2])
    with db:
        if type(status) is int:
            db.execute("""UPDATE alerts SET state=CASE WHEN retries<5 THEN 'pending' ELSE 'failed' END,
                       ready_at=?, retries=retries+1 WHERE id=?""",(int(time.time())+status,row[0]))
        else:
            db.execute("UPDATE alerts SET state=? WHERE id=?", (status,row[0]))
    return True


def _notify_loop(path, alerts, stopping):
    with closing(_open_state(path)) as db:
        while not stopping.is_set():
            try:
                did_work = _dispatch_one(db, alerts.get("destinations", []))
            except (sqlite3.Error, ValueError, OSError):
                did_work = False
            stopping.wait(1.1 if did_work else 1)


@dataclass
class Worker:
    room: dict
    env: dict
    heartbeat: Path
    process: subprocess.Popen | None = None
    state: str = "ready"
    restarts: int = 0
    failures: int = 0
    started: float = 0
    retry_at: float = 0
    seen: int | None = None
    episode: bool = False

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None


def _advance(w, db, args, alerts, read_fd, now):
    if w.state == "ended":
        return
    if w.process is None:
        if now < w.retry_at:
            return
        w.heartbeat.unlink(missing_ok=True)
        w.seen = None
        room = w.room
        cmd = [sys.executable,"-m","socialbot.ops",str(read_fd),"--db",str(args.db),
               "--rules",str(args.rules),"listen",room["platform"],"--channel",room["channel"]]
        if room.get("live_chat_id"):
            cmd.append("--live-chat-id=" + room["live_chat_id"])
        if room.get("auto_reply"):
            cmd.append("--auto-reply")
        w.process = subprocess.Popen(cmd, env={**w.env,"SOCIALBOT_HEARTBEAT":str(w.heartbeat)},
                                     pass_fds=(read_fd,), stdin=subprocess.DEVNULL)
        w.started = now
        w.state = "starting"
        return
    code = w.process.poll()
    try:
        w.seen = int(w.heartbeat.stat().st_mtime)
    except FileNotFoundError:
        pass
    if code == 0:
        w.stop()
        w.state = "ended"
        return
    if code is None:
        if w.seen is not None:
            w.state = "running"
        age = time.time()-w.seen if w.seen is not None else now-w.started
        if age <= args.watchdog:
            if w.seen is not None and now - w.started > 300 and age < 30 and w.episode:
                w.failures = 0
                w.episode = False
                with db:
                    db.execute("DELETE FROM incidents WHERE name=?",(w.room["name"],))
            return
    w.stop()
    w.state = "backoff"
    if not w.episode:
        with db:
            db.execute("INSERT OR IGNORE INTO incidents VALUES(?,?)",(w.room["name"],str(time.time_ns())))
            incident = db.execute("SELECT incident FROM incidents WHERE name=?",(w.room["name"],)).fetchone()[0]
            _queue_alert(db, alerts, "error", (w.room["name"],incident), w.room)
        w.episode = True
    w.failures += 1
    w.restarts += 1
    w.retry_at = now + min(120, 2**min(w.failures,7))


def _publish(db, workers):
    stamp = int(time.time())
    with db:
        db.executemany("INSERT OR REPLACE INTO workers VALUES(?,?,?,?,?,?,?)", (
            (w.room["name"], w.room["platform"], w.state, w.process.pid if w.process else None,
             w.restarts,w.seen,stamp) for w in workers))


def run(args):
    args.db = Path(args.db or "data/socialbot.sqlite3").resolve()
    directory = state_dir(args.db)
    path = directory / "status.sqlite3"
    if args.command == "fleet-status":
        print(json.dumps({"workers":dashboard_status(args.db),"alerts":_alert_counts(path)}, indent=2))
        return 0
    rooms, args.watchdog, alerts = load_config(args.config)
    engine.load_rules(args.rules)
    if args.check:
        print(f"Valid fleet configuration for {len(rooms)} room(s).")
        return 0
    if os.name != "posix":
        raise ValueError("Run the fleet under Linux, macOS or WSL.")
    import fcntl
    for dest in alerts.get("destinations",[]):
        _destination(dest)
    workers = [Worker(room,worker_env(room),directory/(room["name"]+".pulse")) for room in rooms]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory/"supervisor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another supervisor is running for this database.") from None
        with closing(engine.connect(args.db)) as events, closing(_init_state(path)) as db:
            with db:
                db.execute("DELETE FROM workers")
            _resume_alerts(db, events, rooms, alerts)
            for worker in workers:
                worker.episode = db.execute("SELECT 1 FROM incidents WHERE name=?",(worker.room["name"],)).fetchone() is not None
            stopping = threading.Event()
            thread = None
            if alerts.get("destinations"):
                thread = threading.Thread(target=_notify_loop,args=(path,alerts,stopping),daemon=True)
                thread.start()
            read_fd, write_fd = os.pipe()
            previous = signal.signal(signal.SIGTERM, lambda *_: stopping.set())
            try:
                print(f"Supervising {len(workers)} room(s).",flush=True)
                while not stopping.is_set():
                    for worker in workers:
                        _advance(worker,db,args,alerts,read_fd,time.monotonic())
                    _publish(db,workers)
                    if "gift" in alerts.get("events",[]):
                        _capture_gifts(db,events,rooms,alerts)
                    stopping.wait(1)
            finally:
                signal.signal(signal.SIGTERM, previous)
                stopping.set()
                os.close(write_fd)
                os.close(read_fd)
                for worker in workers:
                    worker.stop()
                    worker.state = "stopped"
                _publish(db,workers)
                if thread:
                    thread.join(6)
    return 0


def _alert_counts(path):
    if not path.is_file():
        return {}
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alerts'").fetchone():
            return {}
        return dict(db.execute("SELECT state,count(*) FROM alerts GROUP BY state"))


def _exit_with_parent(fd):
    # Only the supervisor holds the pipe's write end. A hard kill or poweroff
    # cannot leave detached listeners running alongside their replacements.
    try:
        os.read(fd,1)
    finally:
        os._exit(0)


if __name__ == "__main__":
    from .__main__ import main
    fd = int(sys.argv[1])
    threading.Thread(target=_exit_with_parent,args=(fd,),daemon=True).start()
    sys.exit(main(sys.argv[2:]))
