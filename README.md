# Social Bot

Social Bot helps a livestream creator keep up with chat across rooms. It remembers returning viewers, rotates creator-approved announcements, drafts welcome and gift messages, and answers recurring questions using editable rules. A local dashboard shows events, viewers, drafts, and review status by platform and channel. You can run the demo with Python 3.11+ and no accounts or package installs.

## Run the demo

```bash
git clone https://github.com/Cyptopimpinainteazy/social-bot.git
cd social-bot
python3 -m socialbot demo --dashboard
```

Open **http://127.0.0.1:8080** on the machine running the command. Use Ctrl+C to stop. The nine sample events span BIGO, Twitch, Chaturbate, and YouTube. Eight produce drafts. Look for River's second visit to see the returning-viewer greeting. Run the command again to see all nine duplicates skipped.

The demo uses `data/demo.sqlite3`. The live commands use `data/socialbot.sqlite3`. Multiple listeners on the same machine can share that file. To inspect the demo from a second terminal, run:

```bash
python3 -m socialbot --db data/demo.sqlite3 drafts
python3 -m socialbot --db data/demo.sqlite3 events --unanswered
```

## Start with BIGO

Try the creator-feed importer with the included synthetic Social Stream Ninja chat history. Replay the command to see duplicate messages skipped. Then open the dashboard for that same database and review the drafts.

```bash
python3 -m socialbot --db data/bigo-demo.sqlite3 creator-import bigo examples/bigo-chat-history.json --channel studio
python3 -m socialbot --db data/bigo-demo.sqlite3 dashboard --controls --edit-rules data/creator-rules.json
```

For your own BIGO room, use a Chat History JSON export from your Social Stream Ninja installation, or run `creator-listen bigo --channel YOUR_ROOM` and configure the **creator-selected message** POST described in the guide. This gives you a scoped local review inbox. The BIGO adapter has no platform-issued send transport. [Creator feed setup](docs/creator-feed.md)

## Platform support

| Platform | Input | Available actions |
| --- | --- | --- |
| Twitch | Authenticated live chat, Bits, gift subscriptions and an optional public directory query | Reviewed replies, optional keyword auto-replies, reviewed room announcements, category/language discovery |
| YouTube Live | Live Chat API messages, Super Chats and Super Stickers | Reviewed owner messages posted to the verified active broadcast, with returned receipt |
| Chaturbate | Broadcaster Events API for public chat, entries, follows and attributed tips | Local reply drafts plus a separate broadcaster-installed V2 app for native entry greetings, tip acknowledgments and a room-info shortcut |
| Kick | Signed event webhooks | Ephemeral suggestions and session counts |
| Tango | Authorized signed live-chat and follow webhooks | Aggregate counts scoped to the configured creator |
| Fanvue | Authorized signed follow webhook | Deduplicated creator follow-event counts |
| TikTok | Creator-authorized account statistics and Social Stream Ninja public chat inputs | Account metrics, local public-chat drafts |
| BIGO LIVE, Stripchat, Whatnot, CAM4, CamSoda, BongaCams, Fansly, MyFreeCams, LiveJasmin | Social Stream Ninja Chat History export or creator-selected local POST | Local public-chat drafts |
| Telegram | A viewer starts a private conversation with the creator's bot | One welcome per opt-in and `/stop` withdrawal |
| Other named platforms, including Chamet and Passes | Creator-supplied normalized events over JSON Lines or the signed bridge | Local drafts; custom platforms can use a `custom-NAME` label |

Native connectors, creator-forwarded messages, and generic imports record distinct origins. Only native chat from a supported sending platform can authorize an external chat reply. [Platform setup and native Chaturbate app](docs/creator-sources.md) · [YouTube sending](docs/youtube-send.md)

## Creator workflows

| Task | Where to start |
| --- | --- |
| Run supervised room workers, watchdogs and Discord/Telegram operator alerts | `./deploy.sh check` then [fleet operations](docs/operations.md) |
| Manage opt-ins, follow-back requests, follow-ups, affiliate links and quiet-room announcements | `python3 -m socialbot community --help` and [community commands](docs/community.md) |
| Edit reply rules, compare account databases, customize gift tiers or run a local AI co-host | `python3 -m socialbot demo --dashboard --controls --edit-rules data/creator-rules.json` and [creator console](docs/creator-console.md) |
| Connect an approved event producer or enable Telegram welcomes | [Signed bridge and viewer-initiated bot](docs/creator-console.md) |

## Run a live listener

Run one listener per room. Start the dashboard in another terminal after the first listener has initialized the database. To list current capabilities, run `python3 -m socialbot platforms`. Credentials are read from the process environment. You may copy `.env.example` to `.env`, fill in the values locally, and load it into your shell with `set -a; . ./.env; set +a`. Keep the database and credentials on the creator's machine.

### Twitch

Set `TWITCH_BOT_LOGIN` to the login for the reading account and `TWITCH_OAUTH_TOKEN` to its user access token with `chat:read`. The default listener collects events and creates drafts. To send, supply a separate `TWITCH_OWNER_TOKEN` with `user:write:chat` belonging to the broadcaster whose channel you selected. Twitch checks token identity and scope. In Twitch Shared Chat, replies sent using a user access token are visible across the participating channels. [Twitch IRC](https://dev.twitch.tv/docs/chat/irc/) · [Obtaining OAuth tokens](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/) · [Send Chat Message](https://dev.twitch.tv/docs/api/reference/chat/#send-chat-message)

```bash
python3 -m socialbot listen twitch --channel your_channel
python3 -m socialbot dashboard
```

Use the draft ID printed by the listener or the dashboard. Approval changes local state. Sending makes the external request and stores the returned Twitch message ID on confirmation.

```bash
python3 -m socialbot drafts --status pending
python3 -m socialbot approve 1
python3 -m socialbot send 1
```

To automate just the configured keyword replies, start the listener with `--auto-reply`. Greetings, acknowledgments, and Ollama suggestions remain available for local review. The bot ignores messages from the owner and its own reader login. Native Twitch chat IDs tie replies to the incoming message. Imports cannot trigger a live send.

```bash
python3 -m socialbot --rules my-rules.json listen twitch --channel your_channel --auto-reply
```

A reply must be made within ten minutes of receiving the original chat message. Social Bot allows one attempt per channel every 15 seconds, one per viewer in that channel every two minutes and at most 20 per channel per rolling hour. A rate-limited reply remains available for manual sending within its window. The database reserves a send before contacting Twitch. A timeout or unconfirmed response marks it uncertain. An interrupted worker may leave it marked sending. Those states prevent an automatic duplicate. Check Twitch's chat or moderation queue before acting on either. An authorization failure before the POST keeps the draft approved. An automatic sender that hits a platform error continues listening in draft-only mode.

### YouTube Live

Enable the YouTube Data API v3 on your Google Cloud project and set `YOUTUBE_API_KEY`. Get the live chat ID for the active broadcast from `videos.list(part=liveStreamingDetails)` and its `liveStreamingDetails.activeLiveChatId` field. Supply a consistent local channel label for viewer history. The reader skips chat history on startup, follows Google's continuation token and respects its polling interval. Polls consume the project's API quota. [YouTube live chat documentation](https://developers.google.com/youtube/data/live/reference/rest/v3/liveChatMessages/list) · [Video resource](https://developers.google.com/youtube/v3/docs/videos#liveStreamingDetails.activeLiveChatId)

```bash
python3 -m socialbot listen youtube --channel your_channel --live-chat-id YOUR_ACTIVE_CHAT_ID
```

### Chaturbate

Generate an Events API token for your own broadcaster room and set `CHATURBATE_TOKEN`. The adapter follows the authenticated long-poll cursor and keeps upstream IDs to deduplicate repeated deliveries. It drops private messages, anonymous tips, tip notes and broadcaster self-messages before storage. The cursor lives in memory. A disconnect or restart can leave a gap. [Broadcaster Events API](https://chaturbate.com/apps/api/docs/rest.html)

```bash
python3 -m socialbot listen chaturbate --channel your_broadcaster_name
```

### Kick

Create a Kick developer app and enable webhooks. Configure a public HTTPS callback on your own reverse proxy that forwards only the webhook route to `http://127.0.0.1:8091/webhooks/kick`. Subscribe your app to the events you want for your broadcaster, such as `chat.message.sent` or `channel.followed`. Set `KICK_BROADCASTER_ID` to the numeric account ID and give the command your channel slug. The Kick worker presents suggestions in its terminal and keeps live viewer activity out of SQLite and the shared dashboard. It holds a short-lived record of event identifiers in memory to suppress redelivery. The receiver checks signatures against Kick's public key and validates the delivery timestamp and broadcaster identity. These steps require a public callback and a Kick app registration. [Kick event setup](https://github.com/KickEngineering/KickDevDocs/blob/main/events/introduction.md) · [Kick event types](https://github.com/KickEngineering/KickDevDocs/blob/main/events/event-types.md) · [Kick webhook signatures](https://github.com/KickEngineering/KickDevDocs/blob/main/events/webhook-security.md) · [Kick developer terms](https://dev.kick.com/terms-of-service)

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-kick.txt
python -m socialbot kick-webhook --channel your_channel
```

## Bring in creator events

Import one JSON object per line from a file or standard input. Each event needs a stable source ID, platform label, channel, event kind and viewer. For chats, include text. When available, supply a stable platform user ID as a string. Viewer history stays within the platform and channel. The `gift` label counts events such as tips or subscriptions. It has no implied currency or payout value.

```json
{"id":"room-session7-event103","platform":"bigo","channel":"studio","kind":"chat","user":"River","user_id":"3107","text":"When is your next stream?"}
```

```bash
python3 -m socialbot import events.jsonl
python3 -m socialbot dashboard
```

Accepted kinds are `chat`, `join`, `gift`, and `follow`. Re-importing a file is safe across restarts. The unique key is the platform, channel and event ID. Two distinct gifts with different IDs each count, even when they have identical text. If an invalid record stops an import, correct it and rerun the file. Previously committed events are skipped.

## Customize replies and use a local co-host

Copy the bundled rules, edit your actual schedule and links, then pass the edited file before the command. The first matching keyword rule wins. Whole-word matching prevents “hi” from matching “this.” The `{user}` field expands to the viewer's name.

```bash
cp socialbot/rules.json my-rules.json
python3 -m socialbot --rules my-rules.json import events.jsonl
```

For a question your rules didn't answer, Ollama can generate a pending reply on the same machine. Choose an installed model with `--model` or the `OLLAMA_MODEL` environment variable. The default is `qwen3:8b`. Social Bot sends only the selected question to the local Ollama service at `127.0.0.1:11434`. Model drafts require review. [Ollama chat API](https://docs.ollama.com/api/chat)

```bash
python3 -m socialbot --db data/demo.sqlite3 events --unanswered
python3 -m socialbot --db data/demo.sqlite3 suggest twitch --channel studio --event-id t2 --model qwen3:8b
python3 -m socialbot --db data/demo.sqlite3 drafts
```

## Run in Docker or run the tests

On Linux, `docker compose up --build` starts the demo dashboard. Compose uses host networking so its loopback listener is reachable on the same machine at port 8080. A named volume preserves demo state. The native Python command is the portable entry point. The base Docker image includes the core dashboard and the three standard-library listeners. Install the optional Kick dependency in a Python environment for the webhook.

```bash
python3 -m unittest discover -s tests -v
```

Install `requirements-kick.txt` to exercise the signature tests. The suite uses synthetic data, mocked platform responses, real SQLite transactions and loopback HTTP. GitHub Actions runs the suite on Python 3.11–3.13 and smoke-tests the Docker image.

## Verification and live setup

CI runs the Python suite, native JavaScript handler tests, a replay check and Docker smoke tests. Platform receipts in the tests come from fixtures. Broadcaster acceptance requires a live account, the relevant scope or webhook registration, and a test event from the intended room. The source guide identifies where a provider still needs to grant access. Follow-back requests support local review and manual completion. The automation focuses on a creator's own connected rooms; the directory command returns rooms for a human to review.

The [feature coverage table](docs/feature-status.md) maps the original brief to each implementation and identifies the remaining platform-specific dependencies.
