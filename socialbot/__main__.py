"""Command-line interface. Run from the project root with python3 -m socialbot."""
import argparse
from contextlib import closing, nullcontext
import http.client
import json
import os
from pathlib import Path
import sqlite3
import sys

from . import chaturbate, cohost, engine, twitch, youtube

def import_events(db, lines, rules):
    added = duplicates = drafts = 0
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            new, draft = engine.record(db, event, rules)
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"Line {number}: {exc} (earlier valid lines were committed)") from None
        added += int(new)
        duplicates += int(not new)
        drafts += int(draft is not None)
    print(f"Events added: {added} | duplicates: {duplicates} | drafts created: {drafts}")

def stream(db, args, rules):
    if args.auto_reply and args.platform != "twitch":
        raise ValueError("--auto-reply is available on Twitch")
    if args.auto_reply and not os.getenv("TWITCH_OWNER_TOKEN"):
        raise ValueError("Set TWITCH_OWNER_TOKEN to enable live replies")
    if args.platform == "twitch":
        live = twitch.iter_twitch_events(args.channel)
    elif args.platform == "chaturbate":
        live = chaturbate.iter_events(args.channel, os.getenv("CHATURBATE_TOKEN", ""))
    else:
        if not args.live_chat_id:
            raise ValueError("YouTube needs --live-chat-id for an active broadcast")
        live = youtube.iter_events(args.channel, args.live_chat_id, os.getenv("YOUTUBE_API_KEY", ""))
    mode = "automatic Twitch keyword replies" if args.auto_reply else "drafts only"
    print(f"Starting {args.platform} listener for {args.channel} ({mode}).", flush=True)
    auto_reply = args.auto_reply
    with closing(live):
        for event in live:
            try:
                new, draft = engine.record(db, event, rules, origin=args.platform + "-live")
            except ValueError as exc:
                print(f"Skipped invalid platform event: {exc}", file=sys.stderr)
                continue
            if new:
                print(json.dumps({"event": event["id"], "kind": event["kind"], "draft": draft}), flush=True)
            if draft and auto_reply and event["kind"] == "chat":
                engine.review(db, draft, "approve")
                try:
                    engine.send(db, draft, twitch.send_twitch_reply)
                except engine.SendBlocked as exc:
                    print(f"Draft {draft} held: {exc}", file=sys.stderr)
                except twitch.TwitchError as exc:
                    auto_reply = False
                    print(f"Draft {draft}: {exc} Listener switched to drafts only.", file=sys.stderr)

def parser():
    p = argparse.ArgumentParser(prog="python3 -m socialbot", description="Creator room event and reply console")
    p.add_argument("--db", type=Path, help="SQLite file (default data/socialbot.sqlite3; demo uses data/demo.sqlite3)")
    p.add_argument("--rules", type=Path, default=engine.DEFAULT_RULES, help="Reply rules JSON")
    cmd = p.add_subparsers(dest="command", required=True)
    demo = cmd.add_parser("demo", help="Load the included sample rooms")
    demo.add_argument("--dashboard", action="store_true")
    demo.add_argument("--port", type=int, default=8080)
    dash = cmd.add_parser("dashboard", help="Open a read-only dashboard on localhost")
    dash.add_argument("--port", type=int, default=8080)
    ingest = cmd.add_parser("import", help="Import normalized NDJSON events")
    ingest.add_argument("file", help="File path or - for stdin")
    live = cmd.add_parser("listen", help="Listen to a live connector")
    live.add_argument("platform", choices=["twitch", "chaturbate", "youtube"])
    live.add_argument("--channel", required=True)
    live.add_argument("--live-chat-id", help="YouTube broadcast liveChatId")
    live.add_argument("--auto-reply", action="store_true", help="Send matching Twitch rule replies as the owner")
    events = cmd.add_parser("events", help="List the latest 50 events as JSON, including source IDs")
    events.add_argument("--unanswered", action="store_true", help="Show chat events with no draft")
    kick = cmd.add_parser("kick-webhook", help="Receive signed Kick events via your HTTPS reverse proxy")
    kick.add_argument("--channel", required=True, help="Kick channel slug")
    kick.add_argument("--port", type=int, default=8091)
    drafts = cmd.add_parser("drafts", help="List the latest 50 drafts as JSON")
    drafts.add_argument("--status", choices=("pending","approved","dismissed","sent","sending","uncertain","failed"))
    for action in ("approve", "dismiss", "send"):
        item = cmd.add_parser(action, help=f"{action.capitalize()} one reply draft by ID")
        item.add_argument("id", type=int)
    model = cmd.add_parser("suggest", help="Draft a reply to one unanswered chat using local Ollama")
    model.add_argument("platform", choices=engine.PLATFORMS)
    model.add_argument("--channel", required=True)
    model.add_argument("--event-id", required=True)
    model.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "qwen3:8b"))
    cmd.add_parser("platforms", help="Show connector coverage")
    return p

def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "platforms":
        modes = {"twitch": "Live chat and gifts; owner replies",
                 "chaturbate": "Live public chat, joins, follows and attributed tips; draft replies",
                 "youtube": "Live chat, Super Chats and Super Stickers; draft replies"}
        modes["kick"] = "Signed live webhook; ephemeral console suggestions (optional dependency)"
        for name in engine.PLATFORMS:
            print(f"{name:12} {modes.get(name, 'File import; draft replies')}")
        return 0
    db_path = args.db or Path("data/demo.sqlite3" if args.command == "demo" else "data/socialbot.sqlite3")
    try:
        if args.command == "dashboard":
            from .dashboard import serve_dashboard
            if not db_path.is_file():
                raise ValueError("Database not found. Start a listener or run python3 -m socialbot demo --dashboard.")
            serve_dashboard(db_path, args.port)
            return 0
        if args.command == "kick-webhook":
            try:
                from .kick import listen_kick
            except ModuleNotFoundError as exc:
                if exc.name and exc.name.startswith("cryptography"):
                    raise ValueError("Install the optional requirements-kick.txt dependencies first") from None
                raise
            listen_kick(args.channel, engine.load_rules(args.rules), port=args.port)
            return 0
        with closing(engine.connect(db_path)) as db:
            if args.command in ("import", "demo", "listen"):
                rules = engine.load_rules(args.rules)
                if args.command == "listen":
                    stream(db, args, rules)
                else:
                    path = Path(__file__).with_name("demo.jsonl") if args.command == "demo" else args.file
                    source = nullcontext(sys.stdin) if path == "-" else open(path, encoding="utf-8")
                    with source as lines:
                        import_events(db, lines, rules)
            elif args.command == "events":
                query = """SELECT e.platform,e.channel,e.event_id,e.kind,e.viewer,e.text,d.id AS draft
                           FROM events e LEFT JOIN drafts d USING(platform,channel,event_id)"""
                if args.unanswered:
                    query += " WHERE e.kind='chat' AND d.id IS NULL"
                for row in db.execute(query + " ORDER BY e.received_at DESC,e.rowid DESC LIMIT 50"):
                    print(json.dumps(dict(row), ensure_ascii=False))
            elif args.command == "drafts":
                query = "SELECT id,status,platform,channel,event_id,reason,body,remote_id FROM drafts"
                params = ()
                if args.status:
                    query += " WHERE status=?"
                    params = (args.status,)
                for row in db.execute(query + " ORDER BY id DESC LIMIT 50", params):
                    print(json.dumps(dict(row), ensure_ascii=False))
            elif args.command in ("approve", "dismiss"):
                print(f"Draft {args.id}: {engine.review(db, args.id, args.command)}")
            elif args.command == "send":
                print("Twitch message ID:", engine.send(db, args.id, twitch.send_twitch_reply))
            elif args.command == "suggest":
                print("Pending draft:", cohost.suggest(db, args.platform, args.channel,
                                                       args.event_id, args.model))
        if args.command == "demo" and args.dashboard:
            from .dashboard import serve_dashboard
            serve_dashboard(db_path, args.port)
        return 0
    except (ValueError, RuntimeError, sqlite3.Error, OSError, http.client.HTTPException) as exc:
        print(f"Social Bot: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

if __name__ == "__main__":
    sys.exit(main())
