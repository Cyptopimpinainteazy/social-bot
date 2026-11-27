"""Generate a local Ollama suggestion for a single selected chat event."""
import http.client
import json
import os
import sys
import time

from .engine import field

def generate(question, model, persona=""):
    client = http.client.HTTPConnection("127.0.0.1", 11434, timeout=90)
    request = {"model": model, "stream": False, "think": False,
               "options": {"num_predict": 160},
               "messages": [{"role": "system", "content":
                    "Draft one short, friendly reply for a livestream creator to review. "
                    "Use plain text. If the viewer asks about an unknown fact, say so. "
                    "Never invent links, schedules, giveaways or personal information. " + persona},
                    {"role": "user", "content": question}]}
    try:
        client.request("POST", "/api/chat", body=json.dumps(request),
                       headers={"Content-Type": "application/json"})
        response = client.getresponse()
        if response.status != 200:
            raise ValueError(f"Ollama returned HTTP {response.status}. Check the local model name.")
        data = response.read(65537)
        if len(data) > 65536:
            raise ValueError("Ollama response too large")
        result = json.loads(data)
    finally:
        client.close()
    message = result.get("message") if isinstance(result, dict) else None
    body = message.get("content") if isinstance(message, dict) else None
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Ollama returned an empty reply")
    return field(" ".join(body.split())[:500], "model reply", 500)

def suggest(db, platform, channel, event_id, model, generate_reply=generate, *, persona=""):
    key = (platform, channel.casefold(), event_id)
    row = db.execute("""SELECT e.text, d.id FROM events e LEFT JOIN drafts d
        USING(platform,channel,event_id) WHERE e.platform=? AND e.channel=? AND e.event_id=?
        AND e.kind='chat'""", key).fetchone()
    if not row or row["id"] is not None:
        raise ValueError("Select a chat event that has no draft")
    body = generate_reply(row["text"], model, persona) if persona else generate_reply(row["text"], model)
    with db:
        draft = db.execute("""INSERT INTO drafts(platform,channel,event_id,body,reason,created_at)
            VALUES(?,?,?,?,'cohost',?) ON CONFLICT DO NOTHING""", (*key, body, int(time.time())))
        if not draft.rowcount:
            raise ValueError("Another worker already created a draft for this event")
        return draft.lastrowid


def register(cmd):
    p = cmd.add_parser("cohost-worker", help="Generate local Ollama drafts for new unanswered viewer questions")
    p.add_argument("platform", choices=("twitch","chaturbate","youtube"))
    p.add_argument("--channel", required=True)
    p.add_argument("--model", default=os.getenv("OLLAMA_MODEL","qwen3:8b"))
    p.add_argument("--every-sec", type=int, default=15)
    p.add_argument("--once", action="store_true")

def work(args, db):
    from .engine import LiveRules
    rules = LiveRules(args.rules)
    channel = args.channel.strip().casefold()
    if not channel or not 1 <= args.every_sec <= 3600:
        raise ValueError("Supply a channel and a polling interval from 1 through 3600 seconds")
    while True:
        now = int(time.time())
        event = db.execute("""SELECT e.event_id FROM events e LEFT JOIN drafts d USING(platform,channel,event_id)
            WHERE e.platform=? AND e.channel=? AND e.origin=? AND e.kind='chat' AND d.id IS NULL
            AND instr(e.text,'?')>0 AND e.received_at BETWEEN ? AND ? ORDER BY e.received_at,e.rowid LIMIT 1""",
            (args.platform,channel,args.platform+"-live",now-180,now)).fetchone()
        if event:
            try:
                draft = suggest(db,args.platform,channel,event[0],args.model,persona=rules()["persona"])
                print(f"Co-host draft {draft} ready for review", flush=True)
            except (OSError, ValueError, http.client.HTTPException) as exc:
                if args.once:
                    raise
                print(f"Co-host: {exc}",file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.every_sec)
