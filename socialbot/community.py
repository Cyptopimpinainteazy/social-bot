"""Viewer-requested updates and creator announcements, reviewed locally."""
import json
import os
import re
import time
from urllib.parse import urlsplit

from . import twitch

LIVE = ("twitch", "youtube", "chaturbate")
DAY = 86400
ACTIVE = "('pending','approved')"
SCHEMA = """
CREATE TABLE IF NOT EXISTS community_members (
  platform TEXT, channel TEXT, viewer_key TEXT, name TEXT, active INTEGER,
  cycle INTEGER, opted_at INTEGER, last_seen INTEGER, gifts INTEGER DEFAULT 0,
  PRIMARY KEY(platform,channel,viewer_key)
);
CREATE TABLE IF NOT EXISTS community_followbacks (
  platform TEXT, channel TEXT, viewer_key TEXT, name TEXT, asked_at INTEGER,
  status TEXT, PRIMARY KEY(platform,channel,viewer_key)
);
CREATE TABLE IF NOT EXISTS community_rooms (
  platform TEXT, channel TEXT, interval_s INTEGER, quiet_s INTEGER, fresh_s INTEGER,
  next_at INTEGER, last_campaign INTEGER DEFAULT 0, PRIMARY KEY(platform,channel)
);
CREATE TABLE IF NOT EXISTS community_campaigns (
  id INTEGER PRIMARY KEY, platform TEXT, channel TEXT, body TEXT, status TEXT DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS community_queue (
  id INTEGER PRIMARY KEY, platform TEXT, channel TEXT, kind TEXT,
  viewer_key TEXT, campaign_id INTEGER, cycle INTEGER, body TEXT, created INTEGER,
  expires INTEGER, status TEXT DEFAULT 'pending',
  UNIQUE(platform,channel,viewer_key,cycle,kind),
  FOREIGN KEY(campaign_id) REFERENCES community_campaigns(id)
);
CREATE TABLE IF NOT EXISTS community_deliveries (
  item_id INTEGER PRIMARY KEY REFERENCES community_queue(id), platform TEXT, channel TEXT,
  attempt_at INTEGER, remote_id TEXT
);
CREATE INDEX IF NOT EXISTS community_delivery_lookup
 ON community_deliveries(platform,channel,attempt_at);
CREATE INDEX IF NOT EXISTS community_active_queue
 ON community_queue(platform,channel,kind,status);
"""


def init(db):
    """Install schema on connection setup, outside any event transaction."""
    db.executescript(SCHEMA)


def _withdraw(db, room, key):
    db.execute("UPDATE community_members SET active=0 WHERE platform=? AND channel=? AND viewer_key=?",
               (*room, key))
    db.execute(f"""UPDATE community_queue SET status='revoked' WHERE platform=? AND channel=?
                   AND viewer_key=? AND status IN {ACTIVE}""", (*room, key))


def _personal(db, room, key, cycle, kind, body, now, expires):
    db.execute("""INSERT INTO community_queue(platform,channel,viewer_key,cycle,kind,body,created,expires)
                  VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
               (*room, key, cycle, kind, body, now, expires))


def on_event(db, event, origin, now):
    """Called only for newly recorded events, inside the caller's transaction."""
    platform = event["platform"].strip().lower()
    if platform not in LIVE or origin != platform + "-live":
        return
    room = (platform, event["channel"].strip().casefold())
    name = event["user"].strip()
    uid = event.get("user_id")
    key = "id:" + uid.strip() if uid is not None else "name:" + name.casefold()
    command = event.get("text", "").strip().casefold() if event["kind"] == "chat" else ""
    if command == "!cancelfollowback":
        db.execute("""UPDATE community_followbacks SET status='cancelled' WHERE platform=? AND channel=?
                      AND viewer_key=? AND status='pending'""", (*room, key))
        return
    if command == "!followback":
        db.execute("""INSERT INTO community_followbacks(platform,channel,viewer_key,name,asked_at,status)
            VALUES(?,?,?,?,?,'pending') ON CONFLICT(platform,channel,viewer_key) DO UPDATE SET
            name=excluded.name,asked_at=excluded.asked_at,status='pending'
            WHERE community_followbacks.status IN ('expired','closed','cancelled')
            OR (community_followbacks.status='pending' AND community_followbacks.asked_at<=?)""",
                   (*room, key, name, now, now - 7 * DAY))
        return
    if command == "!optout":
        _withdraw(db, room, key)
        return
    member = db.execute("""SELECT * FROM community_members WHERE platform=? AND channel=?
                           AND viewer_key=?""", (*room, key)).fetchone()
    if command == "!optin" and (member is None or not member["active"]):
        cycle = member["cycle"] + 1 if member else 1
        db.execute("""INSERT INTO community_members(platform,channel,viewer_key,name,active,cycle,opted_at,last_seen)
                      VALUES(?,?,?,?,1,?,?,?) ON CONFLICT(platform,channel,viewer_key) DO UPDATE SET
                      name=excluded.name, active=1, cycle=excluded.cycle, opted_at=excluded.opted_at,
                      last_seen=excluded.last_seen, gifts=0""", (*room, key, name, cycle, now, now))
        _personal(db, room, key, cycle, "welcome",
                  f"Thanks for opting in, {name}! You can type !optout in this room anytime.",
                  now, now + DAY)
    elif member and member["active"]:
        db.execute("""UPDATE community_members SET name=?,last_seen=MAX(last_seen,?),gifts=gifts+?
                      WHERE platform=? AND channel=? AND viewer_key=?""",
                   (name, now, int(event["kind"] == "gift"), *room, key))


def _room_state(db, room, now):
    db.execute("""INSERT INTO community_rooms(platform,channel,interval_s,quiet_s,fresh_s,next_at)
                  VALUES(?,?,900,60,600,?) ON CONFLICT DO NOTHING""", (*room, now + 900))


def _quiet_live(db, room, quiet, fresh, now):
    # A time-bounded window on the engine's receipt-time index. Synthetic
    # imports have a distinct origin and never establish live room activity.
    row = db.execute("""SELECT MAX(received_at) AS latest,
                        MAX(CASE WHEN kind='chat' THEN received_at END) AS chat
                        FROM events WHERE platform=? AND channel=? AND origin=?
                          AND received_at BETWEEN ? AND ?""",
                     (*room, room[0] + "-live", now - fresh, now)).fetchone()
    return row["latest"] is not None and (row["chat"] is None or now - row["chat"] >= quiet)


def tick(db, now=None):
    now = int(time.time()) if now is None else now
    with db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""UPDATE community_queue SET status='uncertain' WHERE status='sending'
            AND id IN (SELECT item_id FROM community_deliveries WHERE attempt_at<=?)""", (now - 600,))
        expired = db.execute(f"""UPDATE community_queue SET status='expired'
                                 WHERE status IN {ACTIVE} AND expires<=?""", (now,)).rowcount
        db.execute("""UPDATE community_followbacks SET status='expired' WHERE status='pending'
            AND asked_at<=?""", (now - 7 * DAY,))
        before = db.total_changes
        members = db.execute("""SELECT m.* FROM community_members AS m WHERE active=1
            AND opted_at<=? AND opted_at>? AND NOT EXISTS (
            SELECT 1 FROM community_queue AS q WHERE q.platform=m.platform AND q.channel=m.channel
            AND q.viewer_key=m.viewer_key AND q.cycle=m.cycle AND q.kind='followup')""",
                             (now - DAY, now - 7 * DAY))
        for m in members:
            if m["gifts"]:
                body = f"Thanks for supporting the stream, {m['name']}! What would you like to see next?"
            elif m["last_seen"] - m["opted_at"] >= DAY:
                body = f"Good seeing you back, {m['name']}! What would you like to see on a future stream?"
            else:
                body = f"Thanks for joining us, {m['name']}! What would you like to see on a future stream?"
            _personal(db, (m["platform"], m["channel"]), m["viewer_key"], m["cycle"], "followup",
                      body, now, m["opted_at"] + 7 * DAY)
        followups = db.total_changes - before
        announcements = 0
        for r in db.execute("SELECT * FROM community_rooms WHERE next_at<=?", (now,)).fetchall():
            room = (r["platform"], r["channel"])
            outstanding = db.execute(f"""SELECT 1 FROM community_queue WHERE platform=? AND channel=?
                AND kind='announcement' AND status IN {ACTIVE} LIMIT 1""", room).fetchone()
            if outstanding or not _quiet_live(db, room, r["quiet_s"], r["fresh_s"], now):
                continue
            campaign = db.execute("""SELECT * FROM community_campaigns WHERE platform=? AND channel=?
                AND status='approved' ORDER BY CASE WHEN id>? THEN 0 ELSE 1 END,id LIMIT 1""",
                                  (*room, r["last_campaign"])).fetchone()
            if campaign:
                db.execute("""INSERT INTO community_queue(platform,channel,kind,campaign_id,body,created,expires)
                    VALUES(?,?,'announcement',?,?,?,?)""",
                           (*room, campaign["id"], campaign["body"], now, now + r["fresh_s"]))
                db.execute("""UPDATE community_rooms SET next_at=?,last_campaign=? WHERE platform=? AND channel=?""",
                           (now + r["interval_s"], campaign["id"], *room))
                announcements += 1
    return {"followups": followups, "announcements": announcements, "expired": expired}


def _campaign_text(text, link, affiliate):
    if not isinstance(text, str) or not text.strip() or any(ord(c) < 32 for c in text):
        raise ValueError("Enter announcement text on one line.")
    text = text.strip()
    if affiliate and not link:
        raise ValueError("Affiliate campaigns require a link.")
    if link:
        url = urlsplit(link)
        if (url.scheme != "https" or not url.hostname or url.username is not None or
                url.password is not None or any(c.isspace() or ord(c) < 32 for c in link)):
            raise ValueError("Provide an HTTPS link without credentials.")
        text += (" Affiliate link: " if affiliate else " ") + link
    if len(text) > 450:
        raise ValueError("Announcement text and link must fit within 450 characters.")
    return text


def _item_action(db, action, identifier, now):
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM community_queue WHERE id=?", (identifier,)).fetchone()
        if not row or row["expires"] <= now or row["status"] not in ("pending", "approved"):
            raise ValueError("No active queue item with that ID.")
        if action == "copy":
            if row["status"] != "approved":
                raise ValueError("Approve the item before copying it.")
            if row["kind"] == "announcement":
                room = (row["platform"], row["channel"])
                policy = db.execute("SELECT * FROM community_rooms WHERE platform=? AND channel=?", room).fetchone()
                if not _quiet_live(db, room, policy["quiet_s"], policy["fresh_s"], now):
                    raise ValueError("The room is busy or recent live activity is missing.")
            status = "copied"
        elif action == "approve" and row["status"] == "pending":
            status = "approved"
        elif action == "dismiss":
            status = "dismissed"
        else:
            raise ValueError("This item is already approved.")
        db.execute("UPDATE community_queue SET status=? WHERE id=?", (status, identifier))
    return row["body"] if action == "copy" else status


def send(db, identifier, sender, now=None, *, watch_channel=None):
    """Reserve and deliver one approved public Twitch campaign item."""
    now = int(time.time()) if now is None else now
    with db:
        db.execute("BEGIN IMMEDIATE")
        q = db.execute("""SELECT q.*, r.quiet_s,r.fresh_s FROM community_queue q
            JOIN community_campaigns c ON c.id=q.campaign_id
            JOIN community_rooms r ON r.platform=q.platform AND r.channel=q.channel
            WHERE q.id=? AND c.status='approved' AND q.expires>?
            AND q.platform='twitch' AND q.kind='announcement'
            AND ((? IS NULL AND q.status='approved') OR
                 (q.channel=? AND q.status IN ('pending','approved') AND NOT EXISTS (
                  SELECT 1 FROM community_queue prev WHERE prev.campaign_id=q.campaign_id
                  AND prev.status IN ('sending','uncertain'))))""",
            (identifier, now, watch_channel, watch_channel)).fetchone()
        if not q:
            raise ValueError("An approved, unexpired Twitch announcement from an active campaign is required.")
        room = (q["platform"], q["channel"])
        if not _quiet_live(db, room, q["quiet_s"], q["fresh_s"], now):
            raise ValueError("The room is busy or recent live activity is missing.")
        attempts = db.execute("""SELECT attempt_at FROM community_deliveries
            WHERE platform=? AND channel=? AND attempt_at>? UNION ALL
            SELECT attempt_at FROM drafts WHERE platform=? AND channel=? AND attempt_at>?""",
            (*room, now - 3600, *room, now - 3600)).fetchall()
        in_flight = db.execute("""SELECT 1 FROM community_queue WHERE platform=? AND channel=?
            AND status='sending' LIMIT 1""", room).fetchone()
        if in_flight or len(attempts) >= 20 or any(a[0] > now - 60 for a in attempts):
            raise ValueError("Channel announcement limit reached or another delivery is in progress.")
        db.execute("UPDATE community_queue SET status='sending' WHERE id=?", (identifier,))
        db.execute("INSERT INTO community_deliveries(item_id,platform,channel,attempt_at) VALUES(?,?,?,?)",
                   (identifier, *room, now))
    try:
        remote_id = sender(q["channel"], q["body"])
        if not remote_id:
            raise twitch.TwitchDeliveryUnknown("Twitch did not return a message ID.")
    except Exception as exc:
        with db:
            if isinstance(exc, twitch.TwitchPreflightError):
                # A concurrently archived campaign must not regain an active queue item.
                db.execute("""UPDATE community_queue SET status=CASE WHEN EXISTS (
                    SELECT 1 FROM community_campaigns WHERE id=community_queue.campaign_id AND status='approved')
                    THEN ? ELSE 'revoked' END WHERE id=?""", (q['status'], identifier))
                db.execute("DELETE FROM community_deliveries WHERE item_id=?", (identifier,))
            else:
                status = "failed" if isinstance(exc, twitch.TwitchError) and not isinstance(exc, twitch.TwitchDeliveryUnknown) else "uncertain"
                db.execute("UPDATE community_queue SET status=? WHERE id=?", (status, identifier))
        raise
    with db:
        db.execute("UPDATE community_queue SET status='sent' WHERE id=?", (identifier,))
        db.execute("UPDATE community_deliveries SET remote_id=? WHERE item_id=?", (remote_id, identifier))
    return remote_id


def watch_once(db, channel=None, *, sender=None, now=None):
    """Run scheduling, then optionally publish one configured room's due campaign."""
    now = int(time.time()) if now is None else now
    summary = tick(db, now)
    if channel is None:
        return summary
    row = db.execute("""SELECT id FROM community_queue WHERE platform='twitch' AND channel=?
        AND kind='announcement' AND status IN ('pending','approved') AND expires>?
        ORDER BY id LIMIT 1""", (channel, now)).fetchone()
    if row is None:
        return summary
    item = row["id"]
    try:
        receipt = send(db, item, sender or twitch.send_twitch_announcement, now, watch_channel=channel)
    except ValueError:
        # Another watcher may have claimed/dismissed it or a room limit is active.
        summary["publish"] = "held"
    except twitch.TwitchPreflightError:
        summary["publish"] = "credential check failed"
    except (OSError, RuntimeError):
        status = db.execute("SELECT status FROM community_queue WHERE id=?", (item,)).fetchone()[0]
        summary["publish"] = status
    else:
        summary.update(publish="sent", item_id=item, remote_id=receipt)
    return summary


def register(subparsers):
    cmd = subparsers.add_parser("community", help="Manage opted-in viewers and reviewed room announcements")
    sub = cmd.add_subparsers(dest="community_action", required=True)

    def room_args(p):
        p.add_argument("platform", choices=LIVE)
        p.add_argument("channel")

    room_args(sub.add_parser("members", help="List active consents and viewer keys in one room"))
    requests = sub.add_parser("followbacks", help="List viewer-requested follow-backs")
    room_args(requests)
    requests.add_argument("--status", choices=("pending", "followed", "closed", "expired", "cancelled"), default="pending")
    marks = sub.add_parser("followback-mark", help="Record a completed or closed manual follow-back request")
    room_args(marks)
    marks.add_argument("viewer_key")
    marks.add_argument("result", choices=("followed", "closed"))
    withdraw = sub.add_parser("revoke", help="Withdraw consent using the key shown by members")
    room_args(withdraw)
    withdraw.add_argument("viewer_key")
    add = sub.add_parser("campaign-add", help="Create a pending announcement")
    room_args(add)
    add.add_argument("text")
    add.add_argument("--link")
    add.add_argument("--affiliate", action="store_true")
    for name in ("campaign-approve", "campaign-archive", "approve", "dismiss", "copy", "send"):
        sub.add_parser(name).add_argument("id", type=int)
    room_args(sub.add_parser("campaigns", help="List configured campaigns"))
    schedule = sub.add_parser("schedule", help="Configure a room's rotation timing")
    room_args(schedule)
    schedule.add_argument("--interval-sec", required=True, type=int)
    schedule.add_argument("--quiet-sec", required=True, type=int)
    schedule.add_argument("--fresh-sec", required=True, type=int)
    items = sub.add_parser("queue", help="List the most recent local queue entries in one room")
    room_args(items)
    items.add_argument("--status", choices=("pending", "approved", "copied", "expired", "dismissed", "revoked", "sending", "sent", "uncertain", "failed"))
    sub.add_parser("tick", help="Generate due items and expire stale entries once")
    watch = sub.add_parser("watch", help="Run ticks until interrupted")
    watch.add_argument("--every-sec", type=int, default=30)
    watch.add_argument("--publish-twitch", metavar="CHANNEL",
                       help="Publish due, approved campaign text to this Twitch room as its owner")


def run(args, db):
    now = int(time.time())
    action = args.community_action
    if hasattr(args, "channel"):
        channel = args.channel.strip().casefold()
        if not channel or len(channel) > 128 or any(ord(c) < 32 for c in channel):
            raise ValueError("Provide a valid channel label.")
        room = (args.platform, channel)
    if action == "tick":
        print(json.dumps(tick(db, now)))
    elif action == "watch":
        if args.every_sec < 1:
            raise ValueError("Set --every-sec to at least 1.")
        target = args.publish_twitch
        if target is not None:
            target = target.strip().lower()
            if not re.fullmatch(r"[a-z0-9_]{1,25}", target):
                raise ValueError("Provide the broadcaster's Twitch login for --publish-twitch.")
            if not os.getenv("TWITCH_OWNER_TOKEN"):
                raise ValueError("Set TWITCH_OWNER_TOKEN for the chosen room before publishing.")
        while True:
            print(json.dumps(watch_once(db, target)), flush=True)
            time.sleep(args.every_sec)
    elif action == "send":
        print(send(db, args.id, twitch.send_twitch_announcement))
    elif action in ("approve", "dismiss", "copy"):
        print(_item_action(db, action, args.id, now))
    elif action in ("members", "campaigns", "queue"):
        if action == "members":
            rows = db.execute("""SELECT viewer_key,name,opted_at,last_seen,gifts FROM community_members
                WHERE platform=? AND channel=? AND active=1 ORDER BY name,viewer_key""", room)
        elif action == "campaigns":
            rows = db.execute("SELECT id,body,status FROM community_campaigns WHERE platform=? AND channel=? ORDER BY id", room)
        else:
            rows = db.execute("""SELECT id,kind,viewer_key,body,status,expires FROM community_queue
                WHERE platform=? AND channel=? AND (? IS NULL OR status=?) ORDER BY id DESC LIMIT 100""",
                              (*room, args.status, args.status))
        for row in rows:
            print(json.dumps(dict(row), ensure_ascii=False))
    elif action == "followbacks":
        rows = db.execute("""SELECT viewer_key,name,asked_at,
            CASE WHEN status='pending' AND asked_at<=? THEN 'expired' ELSE status END AS current_status
            FROM community_followbacks WHERE platform=? AND channel=?
            AND (CASE WHEN status='pending' AND asked_at<=? THEN 'expired' ELSE status END)=?
            ORDER BY asked_at,viewer_key""", (now - 7 * DAY, *room, now - 7 * DAY, args.status))
        for row in rows:
            print(json.dumps(dict(row), ensure_ascii=False))
    elif action == "followback-mark":
        with db:
            update = db.execute("""UPDATE community_followbacks SET status=? WHERE platform=? AND channel=?
                AND viewer_key=? AND status='pending' AND asked_at>?""",
                (args.result, *room, args.viewer_key, now - 7 * DAY))
            if update.rowcount != 1:
                raise ValueError("No current pending follow-back request for that viewer in this room.")
        print(args.result)
    elif action == "revoke":
        with db:
            db.execute("BEGIN IMMEDIATE")
            _withdraw(db, room, args.viewer_key)
        print("Consent withdrawn if active.")
    elif action == "campaign-add":
        body = _campaign_text(args.text, args.link, args.affiliate)
        with db:
            _room_state(db, room, now)
            result = db.execute("INSERT INTO community_campaigns(platform,channel,body) VALUES(?,?,?)", (*room, body))
        print(result.lastrowid)
    elif action in ("campaign-approve", "campaign-archive"):
        with db:
            if action == "campaign-approve":
                result = db.execute("UPDATE community_campaigns SET status='approved' WHERE id=? AND status='pending'", (args.id,))
            else:
                result = db.execute("UPDATE community_campaigns SET status='archived' WHERE id=? AND status!='archived'", (args.id,))
                db.execute(f"UPDATE community_queue SET status='revoked' WHERE campaign_id=? AND status IN {ACTIVE}", (args.id,))
            if result.rowcount != 1:
                raise ValueError("No eligible campaign with that ID.")
        print("approved" if action == "campaign-approve" else "archived")
    elif action == "schedule":
        if not (1 <= args.quiet_sec <= args.fresh_sec <= DAY and 1 <= args.interval_sec <= 30 * DAY):
            raise ValueError("Use 1–86400 seconds for quiet/fresh (quiet <= fresh) and up to 30 days for interval.")
        with db:
            _room_state(db, room, now)
            db.execute("""UPDATE community_rooms SET interval_s=?,quiet_s=?,fresh_s=?,next_at=?
                          WHERE platform=? AND channel=?""",
                       (args.interval_sec, args.quiet_sec, args.fresh_sec, now + args.interval_sec, *room))
        print("Schedule saved.")
