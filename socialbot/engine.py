"""Transactional event intake, local viewer memory and reply review."""
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
import sys
from string import Formatter

from .twitch import TwitchDeliveryUnknown, TwitchError, TwitchPreflightError

PLATFORMS = ("bigo", "chaturbate", "chamet", "fanvue", "kick", "passes",
             "stripchat", "tango", "tiktok", "twitch", "whatnot", "youtube",
             "cam4", "camsoda", "bongacams", "flirt4free", "livejasmin", "myfreecams",
             "streamate", "fansly", "onlyfans", "manyvids", "amazonlive", "spoon", "trovo", "patreon")
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
CREATE TABLE IF NOT EXISTS event_routes(platform TEXT, channel TEXT, event_id TEXT, live_chat_id TEXT NOT NULL,
    PRIMARY KEY(platform,channel,event_id), FOREIGN KEY(platform,channel,event_id) REFERENCES events(platform,channel,event_id));
CREATE INDEX IF NOT EXISTS event_viewer_activity ON events(platform,channel,viewer_key,kind);
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
    from . import community
    community.init(db)
    return db

def field(value, label, limit, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{label} must be a string" + ("" if empty else " with a value"))
    if len(value) > limit or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError(f"{label} exceeds its length limit or contains control characters")
    return value.strip()

def platform_name(value):
    platform = field(value, "platform", 32).lower()
    if platform not in PLATFORMS and not re.fullmatch(r"custom-[a-z0-9][a-z0-9-]{0,24}", platform):
        raise ValueError(f"Unknown platform ({platform}); use custom-NAME for your own source")
    return platform

def load_rules(path=DEFAULT_RULES):
    with Path(path).open("rb") as handle:
        source = handle.read(65537)
    if len(source)>65536:
        raise ValueError("Rules file exceeds 64 KiB")
    return compile_rules(json.loads(source.decode("utf-8")))

def compile_rules(cfg):
    if not isinstance(cfg, dict) or not isinstance(cfg.get("keywords"), list):
        raise ValueError("Rules need a keywords array")
    def template(value):
        if isinstance(value, list):
            if not 1 <= len(value) <= 12 or not all(isinstance(v, str) for v in value):
                raise ValueError("Supply one to twelve text alternatives")
            return tuple(template(v) for v in value)
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
    tiers = cfg.get("gift_tiers", [])
    if not isinstance(tiers, list) or len(tiers)>100:
        raise ValueError("gift_tiers must be an array with at most 100 entries")
    rules["gift_tiers"] = []
    for item in tiers:
        if not isinstance(item,dict) or type(item.get("minimum")) is not int or item["minimum"] <= 0:
            raise ValueError("Every gift tier needs a positive integer minimum")
        platform = platform_name(item.get("platform"))
        unit = field(item.get("unit"), "gift unit", 32).lower()
        rules["gift_tiers"].append((platform, unit, item["minimum"], template(item.get("reply"))))
    rules["gift_tiers"].sort(key=lambda item: -item[2])
    rules["persona"] = field(cfg.get("persona", ""), "persona", 1200, empty=True)
    return rules

def select_template(template, identity):
    if isinstance(template, tuple):
        index = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big")
        return template[index % len(template)]
    return template

class LiveRules:
    """Reload a replaced or edited file on demand; keep the last valid configuration."""
    def __init__(self, path):
        self.path = Path(path)
        self.current = load_rules(self.path)
        self.last = None

    def __call__(self):
        try:
            stat = self.path.stat()
            stamp = stat.st_ino, stat.st_mtime_ns, stat.st_size
            if stamp != self.last:
                self.last = stamp
                self.current = load_rules(self.path)
        except (OSError, ValueError, RecursionError) as exc:
            if self.last != "missing" or not isinstance(exc,OSError):
                print(f"Rules reload: {exc}; using the last valid settings", file=sys.stderr)
            if isinstance(exc,OSError):
                self.last = "missing"
        return self.current

def record(db, event, rules, *, origin="import", now=None):
    if not isinstance(event, dict):
        raise ValueError("Each event must be a JSON object")
    platform = platform_name(event.get("platform"))
    kind = field(event.get("kind"), "kind", 12)
    if kind not in KINDS:
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
        if platform=="youtube" and origin=="youtube-live" and event.get("live_chat_id"):
            target = field(event["live_chat_id"], "live_chat_id", 512)
            db.execute("INSERT INTO event_routes VALUES(?,?,?,?)",(platform,channel,eid,target))
        # Column names are selected exclusively from the fixed KINDS mapping.
        counter = KINDS[kind]
        db.execute(f"""INSERT INTO viewers(platform,channel,viewer_key,viewer,{counter},last_seen)
                     VALUES(?,?,?,?,1,?) ON CONFLICT(platform,channel,viewer_key)
                     DO UPDATE SET {counter}={counter}+1, viewer=excluded.viewer,
                                   last_seen=MAX(last_seen,excluded.last_seen)""",
                   (platform, channel, key, user, now))
        from . import community
        community.on_event(db, event, origin, now)
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
            if kind == "gift" and type(event.get("gift_amount")) is int:
                for site, unit, minimum, alternate in rules.get("gift_tiers", ()):
                    if site == platform and unit == event.get("gift_unit") and event["gift_amount"] >= minimum:
                        template, reason = alternate, "gift_tier"
                        break
        if template is None:
            return True, None
        body = select_template(template, f"{platform}/{channel}/{eid}").format(user=user)[:500]
        draft = db.execute("""INSERT INTO drafts(platform,channel,event_id,body,reason,created_at)
                            VALUES(?,?,?,?,?,?)""", (platform, channel, eid, body, reason, now))
        return True, draft.lastrowid

def edit_draft(db, draft_id, body):
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT platform,status FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not row or row["status"] not in ("pending", "approved"):
            raise ValueError("Only a pending or approved draft can be edited")
        body = field(body,"reply",200 if row["platform"]=="youtube" else 500)
        db.execute("UPDATE drafts SET body=?,status='pending' WHERE id=?", (body,draft_id))

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

def send(db, draft_id, sender, *, youtube_sender=None, now=None):
    now = int(time.time()) if now is None else now
    # Serialize checks and the claim across workers before making the network call.
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT d.*, e.origin, e.kind, e.viewer_key, e.received_at FROM drafts d JOIN events e
                          USING(platform,channel,event_id) WHERE d.id=?""", (draft_id,)).fetchone()
        if not row or row["status"] != "approved":
            raise SendBlocked("Sending requires an approved draft")
        platform = row["platform"]
        if platform not in ("twitch","youtube") or row["origin"] != platform+"-live" or row["kind"] != "chat":
            raise SendBlocked("Select a chat message received directly from Twitch or YouTube")
        route = None
        if platform=="youtube":
            route = db.execute("SELECT live_chat_id FROM event_routes WHERE platform=? AND channel=? AND event_id=?",
                               (platform,row["channel"],row["event_id"])).fetchone()
            if not route or youtube_sender is None:
                raise SendBlocked("This draft has no verified YouTube broadcast route")
            if len(row["body"])>200:
                raise SendBlocked("YouTube messages must fit the 200-character limit; shorten your reply rules")
        if now - row["received_at"] > 600:
            raise SendBlocked("Reply window expired (10 minutes)")
        recent = db.execute("""SELECT d.attempt_at, e.viewer_key FROM drafts d JOIN events e
            USING(platform,channel,event_id) WHERE d.platform=? AND d.channel=? AND d.attempt_at>?""",
            (row["platform"], row["channel"], now - 3600)).fetchall()
        announcements = db.execute("""SELECT attempt_at FROM community_deliveries
                              WHERE platform=? AND channel=? AND attempt_at>?""",
                              (row["platform"],row["channel"],now-3600)).fetchall()
        if (len(recent) + len(announcements) >= 20
                or any(r["attempt_at"] > now - 15 for r in recent + announcements)
                or any(r["viewer_key"] == row["viewer_key"] and r["attempt_at"] > now - 120 for r in recent)):
            raise SendBlocked("Channel or viewer reply limit reached")
        db.execute("UPDATE drafts SET status='sending', attempt_at=? WHERE id=?", (now, draft_id))
    try:
        if platform=="youtube":
            remote_id = youtube_sender(route["live_chat_id"],row["body"])
        else:
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
