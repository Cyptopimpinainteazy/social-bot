"""Generate a local Ollama suggestion for a single selected chat event."""
import http.client
import json
import time

from .engine import field

def generate(question, model):
    client = http.client.HTTPConnection("127.0.0.1", 11434, timeout=90)
    request = {"model": model, "stream": False, "think": False,
               "options": {"num_predict": 160},
               "messages": [{"role": "system", "content":
                    "Draft one short, friendly reply for a livestream creator to review. "
                    "Use plain text. If the viewer asks about an unknown fact, say so. "
                    "Never invent links, schedules, giveaways or personal information."},
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

def suggest(db, platform, channel, event_id, model, generate_reply=generate):
    key = (platform, channel.casefold(), event_id)
    row = db.execute("""SELECT e.text, d.id FROM events e LEFT JOIN drafts d
        USING(platform,channel,event_id) WHERE e.platform=? AND e.channel=? AND e.event_id=?
        AND e.kind='chat'""", key).fetchone()
    if not row or row["id"] is not None:
        raise ValueError("Select a chat event that has no draft")
    body = generate_reply(row["text"], model)
    with db:
        draft = db.execute("""INSERT INTO drafts(platform,channel,event_id,body,reason,created_at)
            VALUES(?,?,?,?,'cohost',?) ON CONFLICT DO NOTHING""", (*key, body, int(time.time())))
        if not draft.rowcount:
            raise ValueError("Another worker already created a draft for this event")
        return draft.lastrowid
