"""Transactional event intake, local viewer memory and reply review."""
import json
import re
import sqlite3
import time
from pathlib import Path
from string import Formatter

from .twitch import TwitchDeliveryUnknown, TwitchError, TwitchPreflightError

PLATFORMS = ("bigo", "chaturbate", "chamet", "fanvue", "kick", "passes",
             "stripchat", "tango", "tiktok", "twitch", "whatnot", "youtube")
KINDS = {"join": "joins", "chat": "chats", "follow": "follows", "gift": "gifts"}
DEFAULT_RULES = Path(__file__).with_name("rules.json")
SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  platform TEXT, channel TEXT, event_id TEXT, kind TEXT, viewer TEXT,
  viewer_key TEXT, text TEXT, origin TEXT, received_at INTEGER,
  PRIMARY KEY(platform,channel,event_id)
);
CREATE TABLE IF NOT EXISTS viewers(
  platform TEXT, channel TEXT, viewer_key TEXT, viewer TEXT, joins INTEGER DEFAULT 0,
  chats INTEGER DEFAULT 0, gifts INTEGER DEFAULT 0, follows INTEGER DEFAULT 0,
  last_seen INTEGER, PRIMARY KEY(platform,channel,viewer_key)
);
CREATE TABLE IF NOT EXISTS drafts(
  id INTEGER PRIMARY KEY, platform TEXT, channel TEXT, event_id TEXT, body TEXT,
  status TEXT DEFAULT 'pending', reason TEXT, created_at INTEGER, attempt_at INTEGER,
  remote_id TEXT, UNIQUE(platform,channel,event_id),
  FOREIGN KEY(platform,channel,event_id) REFERENCES events(platform,channel,event_id)
);
CREATE INDEX IF NOT EXISTS events_chronological ON events(received_at);
CREATE INDEX IF NOT EXISTS delivery_window ON drafts(platform,channel,attempt_at);
"""

def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level="IMMEDIATE")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db

def field(value, label, limit, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{label} must be a string" + ("" if empty else " with a value"))
    if len(value) > limit or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError(f"{label} exceeds its length limit or contains control characters")
    return value.strip()

def load_rules(path=DEFAULT_RULES):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or not isinstance(cfg.get("keywords"), list):
        raise ValueError("Rules need a keywords array")
    def template(value):
        value = field(value, "reply template", 400)
        for _, name, spec, conv in Formatter().parse(value):
            if name is not None and (name != "user" or spec or conv):
                raise ValueError("Only the {user} placeholder is supported")
        return value
    rules = {k: template(cfg.get(k)) for k in ("first_join", "return_join", "gift", "follow")}
    compiled = []
    for item in cfg["keywords"]:
        if not isinstance(item, dict) or not isinstance(item.get("terms"), list) or not item["terms"]:
            raise ValueError("Each keyword rule needs at least one term")
        terms = [re.escape(field(t, "keyword", 100)) for t in item["terms"]]
        compiled.append((re.compile(r"(?<!\w)(?:" + "|".join(terms) + r")(?!\w)", re.I),
                         template(item.get("reply"))))
    rules["keywords"] = compiled
    return rules

def record(db, event, rules, *, origin="import", now=None):
    if not isinstance(event, dict):
        raise ValueError("Each event must be a JSON object")
    platform = field(event.get("platform"), "platform", 32).lower()
    kind = field(event.get("kind"), "kind", 12)
    if platform not in PLATFORMS or kind not in KINDS:
        raise ValueError(f"Unknown platform or event kind ({platform}, {kind})")
    channel = field(event.get("channel"), "channel", 128).casefold()
    eid = field(event.get("id"), "id", 160)
    user = field(event.get("user"), "user", 128)
    uid = event.get("user_id")
    key = "name:" + user.casefold() if uid is None else "id:" + field(uid, "user_id", 128)
    message = field(event.get("text", ""), "text", 4096, empty=kind != "chat")
    now = int(time.time()) if now is None else now
    with db:
        inserted = db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                              (platform, channel, eid, kind, user, key, message, origin, now))
        if inserted.rowcount == 0:
            return False, None
        # Column names are selected exclusively from the fixed KINDS mapping.
        counter = KINDS[kind]
        db.execute(f"""INSERT INTO viewers(platform,channel,viewer_key,viewer,{counter},last_seen)
                     VALUES(?,?,?,?,1,?) ON CONFLICT(platform,channel,viewer_key)
                     DO UPDATE SET {counter}={counter}+1, viewer=excluded.viewer,
                                   last_seen=MAX(last_seen,excluded.last_seen)""",
                   (platform, channel, key, user, now))
        reason = kind
        if kind == "chat":
            template = next((reply for regex, reply in rules["keywords"] if regex.search(message)), None)
            reason = "keyword"
        elif kind == "join":
            joins = db.execute("SELECT joins FROM viewers WHERE platform=? AND channel=? AND viewer_key=?",
                               (platform, channel, key)).fetchone()[0]
            reason = "first_join" if joins == 1 else "return_join"
            template = rules[reason]
        else:
            template = rules[kind]
        if template is None:
            return True, None
        body = template.format(user=user)[:500]
        draft = db.execute("""INSERT INTO drafts(platform,channel,event_id,body,reason,created_at)
                            VALUES(?,?,?,?,?,?)""", (platform, channel, eid, body, reason, now))
        return True, draft.lastrowid

def review(db, draft_id, action):
    if action not in ("approve", "dismiss"):
        raise ValueError("Review must approve or dismiss a draft")
    new_status = "approved" if action == "approve" else "dismissed"
    with db:
        row = db.execute("""UPDATE drafts SET status=? WHERE id=? AND
            (status='pending' OR (status='approved' AND ?='dismissed'))""",
                         (new_status, draft_id, new_status))
        if row.rowcount != 1:
            raise ValueError("No eligible draft with that ID")
    return new_status

class SendBlocked(ValueError):
    """No attempt was made."""

def send(db, draft_id, sender, *, now=None):
    now = int(time.time()) if now is None else now
    # Serialize checks and the claim across workers before making the network call.
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT d.*, e.origin, e.kind, e.viewer_key, e.received_at FROM drafts d JOIN events e
                          USING(platform,channel,event_id) WHERE d.id=?""", (draft_id,)).fetchone()
        if not row or row["status"] != "approved":
            raise SendBlocked("Sending requires an approved draft")
        if row["origin"] != "twitch-live" or row["platform"] != "twitch" or row["kind"] != "chat":
            raise SendBlocked("Only a message received directly from Twitch chat can be replied to")
        if now - row["received_at"] > 600:
            raise SendBlocked("Reply window expired (10 minutes)")
        recent = db.execute("""SELECT d.attempt_at, e.viewer_key FROM drafts d JOIN events e
            USING(platform,channel,event_id) WHERE d.platform=? AND d.channel=? AND d.attempt_at>?""",
            (row["platform"], row["channel"], now - 3600)).fetchall()
        if (len(recent) >= 20 or any(r["attempt_at"] > now - 15 for r in recent)
                or any(r["viewer_key"] == row["viewer_key"] and r["attempt_at"] > now - 120 for r in recent)):
            raise SendBlocked("Channel or viewer reply limit reached")
        db.execute("UPDATE drafts SET status='sending', attempt_at=? WHERE id=?", (now, draft_id))
    try:
        remote_id = sender(row["channel"], row["event_id"], row["body"])
        if not remote_id:
            raise RuntimeError("Platform did not confirm a message ID")
    except TwitchPreflightError:
        with db:
            db.execute("UPDATE drafts SET status='approved', attempt_at=NULL WHERE id=?", (draft_id,))
        raise
    except Exception as exc:
        status = "failed" if isinstance(exc, TwitchError) and not isinstance(exc, TwitchDeliveryUnknown) else "uncertain"
        with db:
            db.execute("UPDATE drafts SET status=? WHERE id=?", (status, draft_id))
        raise
    with db:
        db.execute("UPDATE drafts SET status='sent', remote_id=? WHERE id=?", (remote_id, draft_id))
    return remote_id
