# Social Bot

Social Bot helps a livestream creator keep up with chat across rooms. It remembers returning viewers, drafts greetings and gift acknowledgments, and answers recurring questions using editable rules. A local dashboard shows events, viewers, drafts, and review status by platform and channel. You can run the demo with Python 3.11+ and no accounts or package installs.

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

## Platform support

| Platform | Event source in this version | Replies |
| --- | --- | --- |
| Twitch | Authenticated IRC chat, Bits cheers, individual gift subscriptions | Local drafts and owner-authorized replies to live chat |
| YouTube Live | Live Chat API text messages, Super Chats and Super Stickers | Local drafts |
| Kick | Signed webhooks for chat, follows and gifts from a registered app | Ephemeral console suggestions |
| Chaturbate | Broadcaster's Events API for public chat, room entry, follows and attributed tips | Local drafts |
| BIGO LIVE, TikTok LIVE, Tango, Chamet, Passes, Whatnot, Fanvue, Stripchat | Creator-supplied events in the JSON Lines format below | Local drafts |

The last row names accepted platform labels. Bringing in data from one of those services requires an export or a separately authorized event source that you convert to this format. BIGO was the original use case. A verified broadcaster chat integration is the next dependency for connecting it directly. [BIGO support and business contacts](https://www.bigo.tv/blog/bigo-live-support)

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

## Delivery scope

This release implements room event intake, viewer history, a local dashboard and reply handling. The project backlog includes authorized BIGO and additional creator-platform integrations, creator-approved announcement scheduling, a viewer opt-in for welcome messages and follow-up, Discord/Telegram alerts, and livestream validation for each connector. A new platform needs documented event access, stable source IDs, replay fixtures and a broadcaster-owned smoke test before its status can move from file import to live-verified. The integration tests can run without creator credentials. Confirm actual event receipt on a broadcaster account before relying on a connector for a show.
