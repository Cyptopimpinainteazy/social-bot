"""Welcome a viewer who starts a private conversation with the creator's bot."""
import http.client
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

def register(commands):
    p = commands.add_parser("telegram-welcome", help="Handle viewer-initiated private /start and /stop commands")
    p.add_argument("--welcome", default="Welcome! Thanks for joining us. Use /stop to opt out.")
    p.add_argument("--once", action="store_true", help="Poll and handle one batch")

def api(token, method, params):
    conn = http.client.HTTPSConnection("api.telegram.org", timeout=35)
    try:
        conn.request("POST", "/bot"+token+"/"+method, json.dumps(params), {"Content-Type":"application/json"})
        res = conn.getresponse()
        raw = res.read(1024*1024+1)
        if res.status != 200 or len(raw) > 1024*1024:
            raise RuntimeError(f"Telegram {method} failed (HTTP {res.status})")
        obj = json.loads(raw)
        if not isinstance(obj,dict) or obj.get("ok") is not True:
            raise RuntimeError(f"Telegram {method} was not confirmed")
        return obj["result"]
    finally:
        conn.close()

def consume(db, bot, updates, welcome, send):
    """Claim each welcome before any external call. Never replay an unconfirmed send."""
    for update in updates:
        uid = update.get("update_id") if isinstance(update,dict) else None
        if type(uid) is not int or uid < 0:
            raise ValueError("Telegram returned an invalid update")
        message = update.get("message") or {}
        peer = message.get("chat") or {}
        actor = message.get("from") or {}
        chat_id = peer.get("id")
        own = peer.get("type")=="private" and type(chat_id) is int and actor.get("id")==chat_id and not actor.get("is_bot")
        parts = (message.get("text") or "").split()
        action = parts[0] if own and parts else ""
        should_send = False
        with db:
            db.execute("BEGIN IMMEDIATE")
            prev = db.execute("SELECT cursor FROM telegram_cursor WHERE bot=?", (bot,)).fetchone()
            if prev is not None and uid < prev[0]:
                continue
            db.execute("""INSERT INTO telegram_cursor VALUES(?,?,?) ON CONFLICT(bot) DO UPDATE
                         SET cursor=excluded.cursor, last_update_at=excluded.last_update_at""",(bot,uid+1,int(time.time())))
            if action == "/stop":
                db.execute("DELETE FROM telegram_members WHERE bot=? AND chat_id=?", (bot,chat_id))
            elif action == "/start":
                claimed = db.execute("""INSERT INTO telegram_members(bot,chat_id,welcome_status)
                            VALUES(?,?,'claimed') ON CONFLICT DO NOTHING""", (bot,chat_id))
                should_send = bool(claimed.rowcount)
        if should_send:
            try:
                receipt = send(chat_id, welcome)
                if not isinstance(receipt, dict) or type(receipt.get("message_id")) is not int:
                    raise ValueError("No Telegram receipt")
            except (OSError, ValueError, RuntimeError, http.client.HTTPException):
                status = "uncertain"
            else:
                status = "sent"
            with db:
                db.execute("UPDATE telegram_members SET welcome_status=? WHERE bot=? AND chat_id=?",
                           (status,bot,chat_id))
            print(f"Telegram welcome: {status}", flush=True)

def init(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS telegram_cursor(bot INTEGER PRIMARY KEY, cursor INTEGER,last_update_at INTEGER);
        CREATE TABLE IF NOT EXISTS telegram_members(bot INTEGER, chat_id INTEGER, welcome_status TEXT,
                   PRIMARY KEY(bot,chat_id));
    """)

@contextmanager
def worker_lock(path):
    # A single local poller per database/bot keeps /stop and /start in arrival order.
    import fcntl
    with Path(path).open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("This Telegram bot already has a poller for this database") from None
        yield

def run(args, db):
    token = os.environ.get("SOCIALBOT_WELCOME_BOT_TOKEN","")
    if not re.fullmatch(r"[0-9]{1,20}:[A-Za-z0-9_-]{15,150}", token):
        raise ValueError("Set SOCIALBOT_WELCOME_BOT_TOKEN from BotFather")
    if not args.welcome.strip() or len(args.welcome)>2000:
        raise ValueError("Welcome text must be 1–2000 characters")
    bot = int(token.partition(":")[0])
    init(db)
    path = db.execute("PRAGMA database_list").fetchone()[2]
    with worker_lock(str(path)+f".tg-{bot}.lock"):
        while True:
            # Telegram can choose a new random update ID after a week of inactivity.
            with db:
                db.execute("DELETE FROM telegram_cursor WHERE bot=? AND last_update_at<?", (bot,int(time.time())-7*86400))
            row = db.execute("SELECT cursor FROM telegram_cursor WHERE bot=?", (bot,)).fetchone()
            batch = api(token,"getUpdates",{"offset":row[0] if row else 0,"timeout":0 if args.once else 20,
                         "limit":50,"allowed_updates":["message"]})
            if not isinstance(batch,list):
                raise RuntimeError("Invalid Telegram update batch")
            consume(db, bot, batch, args.welcome, lambda chat_id, text: api(
                token,"sendMessage",{"chat_id":chat_id,"text":text}))
            if args.once:
                return 0
