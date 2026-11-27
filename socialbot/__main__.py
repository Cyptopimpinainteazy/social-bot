"""Command-line interface. Run from the project root with python3 -m socialbot."""
import argparse
from contextlib import closing, nullcontext
import http.client
import json
import os
from pathlib import Path
import sqlite3
import sys

from . import bridge, chaturbate, cohost, community, creator_feed, creator_sources, engine, ops, telegram, twitch, youtube

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
                new, draft = engine.record(db, event, rules() if callable(rules) else rules, origin=args.platform + "-live")
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
    dash = cmd.add_parser("dashboard", help="Open the local creator dashboard")
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
    edit = cmd.add_parser("edit", help="Revise a draft and return it to pending review")
    edit.add_argument("id",type=int)
    edit.add_argument("text")
    model = cmd.add_parser("suggest", help="Draft a reply to one unanswered chat using local Ollama")
    model.add_argument("platform", type=engine.platform_name)
    model.add_argument("--channel", required=True)
    model.add_argument("--event-id", required=True)
    model.add_argument("--model", default=os.getenv("OLLAMA_MODEL", "qwen3:8b"))
    for view in (dash,demo):
        view.add_argument("--controls", action="store_true", help="Enable local draft review forms")
        view.add_argument("--edit-rules", type=Path, metavar="PATH", help="Edit a local rules file; create it if absent")
        view.add_argument("--edit-fleet", type=Path, metavar="PATH", help="Edit an existing local fleet configuration")
        view.add_argument("--include-db", action="append", default=[], metavar="NAME=PATH",
                          help="Show totals from an additional account database")
    ops.register(cmd)
    community.register(cmd)
    bridge.register(cmd)
    telegram.register(cmd)
    creator_sources.register(cmd)
    creator_feed.register(cmd)
    cohost.register(cmd)
    cmd.add_parser("platforms", help="Show connector coverage")
    return p

def open_dashboard(args, db_path):
    from .controls import Controls
    from .dashboard import serve_dashboard
    control = None
    if args.controls or args.edit_rules or args.edit_fleet:
        control = Controls(db_path, edit_rules=args.edit_rules, edit_fleet=args.edit_fleet, review=args.controls)
    extra = []
    for label in args.include_db:
        alias, separator, path = label.partition("=")
        if not (alias.strip() and separator and path.strip()):
            raise ValueError("--include-db takes NAME=PATH")
        extra.append((alias.strip(),Path(path)))
    serve_dashboard(db_path,args.port,controls=control,extra_dbs=extra)

def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "platforms":
        modes = {"twitch": "Live chat and gifts; owner replies",
                 "chaturbate": "Live public chat, joins, follows and attributed tips; draft replies",
                 "youtube": "Live chat, Super Chats and Super Stickers; reviewed owner messages"}
        modes["kick"] = "Signed live webhook; ephemeral console suggestions (optional dependency)"
        modes.update({"fanvue":"Signed creator follower webhook; aggregate statistics",
                      "tango":"Creator-authorized signed webhook; aggregate statistics",
                      "tiktok":"Creator-authorized account statistics; stream events via file or creator bridge"})
        for name in creator_feed.SOURCES:
            modes[name] = (modes[name] + "; " if name in modes else "") + "Creator-selected SSN chat feed and history"
        for name in sorted(engine.PLATFORMS):
            print(f"{name:12} {modes.get(name, 'Creator-issued bridge or file import; draft replies')}")
        return 0
    db_path = args.db or Path("data/demo.sqlite3" if args.command == "demo" else "data/socialbot.sqlite3")
    try:
        if args.command in ("fleet","fleet-status"):
            return ops.run(args)
        if args.command == "dashboard":
            if not db_path.is_file():
                raise ValueError("Database not found. Start a listener or run python3 -m socialbot demo --dashboard.")
            open_dashboard(args, db_path)
            return 0
        if args.command == "kick-webhook":
            try:
                from .kick import listen_kick
            except ModuleNotFoundError as exc:
                if exc.name and exc.name.startswith("cryptography"):
                    raise ValueError("Install the optional requirements-kick.txt dependencies first") from None
                raise
            listen_kick(args.channel, engine.LiveRules(args.rules), port=args.port)
            return 0
        with closing(engine.connect(db_path)) as db:
            if args.command == "community":
                return community.run(args, db) or 0
            if args.command == "bridge":
                return bridge.run(args, db, engine.LiveRules(args.rules))
            if args.command == "telegram-welcome":
                return telegram.run(args, db)
            if args.command == "cohost-worker":
                return cohost.work(args,db)
            if args.command in creator_feed.COMMANDS:
                return creator_feed.run(args,db,engine.load_rules(args.rules)) or 0
            if args.command in creator_sources.COMMANDS:
                return creator_sources.run(args, db, engine.load_rules(args.rules)) or 0
            if args.command in ("import", "demo", "listen"):
                rules = engine.load_rules(args.rules)
                if args.command == "listen":
                    stream(db, args, engine.LiveRules(args.rules))
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
            elif args.command == "edit":
                engine.edit_draft(db,args.id,args.text)
                print(f"Draft {args.id} updated and awaiting review")
            elif args.command == "send":
                from .youtube_send import send_youtube_message
                print("Platform message ID:", engine.send(db, args.id, twitch.send_twitch_reply,
                                                        youtube_sender=send_youtube_message))
            elif args.command == "suggest":
                print("Pending draft:", cohost.suggest(db, args.platform, args.channel,
                                                       args.event_id, args.model, persona=engine.load_rules(args.rules)["persona"]))
        if args.command == "demo" and args.dashboard:
            open_dashboard(args, db_path)
        return 0
    except (ValueError, RuntimeError, sqlite3.Error, OSError, http.client.HTTPException) as exc:
        print(f"Social Bot: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

if __name__ == "__main__":
    sys.exit(main())
