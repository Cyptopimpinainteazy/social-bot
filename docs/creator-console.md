# Creator console

## Open and edit the dashboard

From the repository root, start a demo with review controls and a local rules file. The editor seeds the file on first use.

```bash
python3 -m socialbot demo --dashboard --controls --edit-rules data/creator-rules.json
```

Open http://127.0.0.1:8080. Use **Review pending replies** to edit, approve or dismiss a draft. Save edited wording before approving. You can also use `python3 -m socialbot edit ID "Revised text"`; a CLI edit returns the draft to pending review. Approval updates local state. To send an approved eligible reply to Twitch or YouTube, use the separate `send ID` command against the same live database. Demo and bridge events are permanently marked as such. The sample dashboard can demonstrate review without contacting a platform.

To use edited settings live, start the listener with the same rules path:

```bash
python3 -m socialbot --rules data/creator-rules.json listen twitch --channel your_channel
python3 -m socialbot dashboard --controls --edit-rules data/creator-rules.json
```

You can pass `--edit-fleet fleet.json` to edit an existing supervisor configuration. Saving checks its schema. Fleet changes apply at the next supervisor start. Listeners reload reply rules at the next event; the co-host worker reloads its persona when drafting. Invalid edits preserve the last working file in the dashboard, and a listener that detects a broken external edit keeps its last valid settings. Concurrent edits to the same form require refreshing the older tab. Local edit forms check their origin, revision and form token.

The dashboard lists repeat viewers and gift activity by room, alongside the status and heartbeat of workers supervised for that database. For separate installations, repeat `--include-db LABEL=PATH` to compare account totals while keeping their event stores independent.

```bash
python3 -m socialbot --db data/studio.sqlite3 dashboard --include-db weekend=data/weekend.sqlite3
```

## Configure the co-host and gift tiers

`examples/creator-rules.json` shows a persona, alternative phrasings and gift thresholds. You can supply an array of up to twelve creator-written alternatives anywhere a reply template is accepted. A stable event ID picks the same phrase when an event is replayed. The `{user}` placeholder uses the viewer name. Gift thresholds compare amounts within one platform and one native unit. Current native readers attach `bits` for Twitch cheers and `tokens` for attributed Chaturbate tips. The highest matching threshold wins. Gift quantities and gift counts represent different things.

To have local Ollama draft answers to incoming questions, run the worker beside the live listener. It looks for unanswered question marks in chats received directly within three minutes, handles one per polling cycle and creates a pending draft. Use `--once` for a single pass. The default model is `qwen3:8b`, served from loopback. To use an Ollama model on another machine, use the worker on that machine with its own local Ollama service and a co-located listener/database.

```bash
python3 -m socialbot --rules data/creator-rules.json cohost-worker twitch --channel your_channel --model qwen3:8b
```

## Telegram viewer welcome

Create a bot in BotFather and set `SOCIALBOT_WELCOME_BOT_TOKEN`. Use a bot configured for long polling; a bot already using a webhook needs a separate bot or a deliberate transport change. Run the worker on Linux, macOS or WSL. Give viewers the bot's Telegram link so they can start a private conversation. This is a separate credential from the operator alerts bot.

```bash
python3 -m socialbot telegram-welcome --welcome "Welcome to the workshop! Use /stop to opt out."
```

A private `/start` from the chat owner triggers one welcome per opt-in. Repeated starts are suppressed. `/stop` removes the membership entry. A subsequent start is a new request. The store holds a bot-scoped update cursor, opted-in chat IDs and welcome status. The worker claims each update before making a send, and an ambiguous delivery is held to avoid replaying it. Ordinary text and group conversations are ignored. This worker does not pair Telegram identities with viewer names on other platforms. [Telegram getUpdates and sendMessage](https://core.telegram.org/bots/api) · [Viewer-initiated bot links](https://core.telegram.org/bots/features#deep-linking)

## Connect your own producer

The creator bridge accepts normalized JSON events from an integration you control. Configure a separate strong secret per producer and room. The server binds to loopback. It accepts exactly the lowercase platform and normalized channel you configured. HMAC authentication identifies your producer. Platform credentials or consent are not inferred from this path. Events create local viewer records and drafts. Direct platform replies and opt-in commands require the native adapter.

```bash
export SOCIALBOT_BRIDGE_SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
python3 -m socialbot bridge serve --platform custom-studio --channel art
```

In another shell with the same secret, run `python3 -m socialbot bridge submit event.json`. A minimal file:

```json
{"platform":"custom-studio","channel":"art","id":"show-17-msg-103","kind":"chat","user":"River","text":"Hello!"}
```

For your own sender, POST UTF-8 JSON to `http://127.0.0.1:8094/v1/events`. Put the current Unix time in `X-SocialBot-Time`. Set `X-SocialBot-Signature` to the lowercase hex HMAC-SHA256 of the ASCII timestamp, one period, and the exact body bytes, using the shared secret. The receiver accepts 32 KiB per event and five minutes of clock skew. Status 201 means inserted, 200 means previously recorded. Reuse the same event ID for redelivery. The receiver supports listed platform labels plus operator-defined `custom-NAME` labels. Platform labels establish where the creator says the events originated. The bridge does not connect to that platform itself.
