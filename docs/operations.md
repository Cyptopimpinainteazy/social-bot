# Running a creator fleet

One supervisor can operate Twitch, YouTube Live and Chaturbate listeners together. A worker uses the same `listen` command, rules and SQLite event store as a standalone listener. The supervisor is for Linux, macOS and WSL. Run signed inbound webhook receivers separately with their documented endpoints.

The project launcher offers an offline demo with `./deploy.sh demo`, a configuration check with `./deploy.sh check`, and native or container fleet startup with `./deploy.sh fleet` or `./deploy.sh docker`.

## Native launch

From the project root, copy the example once. Remove rooms you don't operate, give each room a short local alias and replace the example channel names and YouTube live chat ID. For each listener, export the credentials shown below into the supervisor's environment. The program reads secrets from environment variables; the JSON contains only the names of those variables.

```sh
cp examples/fleet.json fleet.json
python3 -m socialbot fleet --config fleet.json --check
python3 -m socialbot fleet --config fleet.json
```

The check validates the JSON and selected reply rules offline. Starting the fleet verifies required environment variables, launches one worker per room, and begins receiving new activity. `Ctrl+C` or `SIGTERM` stops the supervisor and workers. Configuration edits take effect when you restart the supervisor.

| Listener | Required environment | Room settings |
| --- | --- | --- |
| Twitch | `TWITCH_BOT_LOGIN`, `TWITCH_OAUTH_TOKEN` with chat read permission | `channel` is your broadcaster login. Listening creates local drafts by default. Explicit `auto_reply: true` also requires `TWITCH_OWNER_TOKEN` for the same broadcaster and enables the normal live keyword-reply pipeline. |
| YouTube | `YOUTUBE_API_KEY` | Pick a locally meaningful `channel` label and supply the active broadcast's `live_chat_id`. The listener ends cleanly when YouTube reports the chat has ended. Set a new ID and restart the fleet to follow a new broadcast. |
| Chaturbate | `CHATURBATE_TOKEN` | `channel` is the broadcaster login. Use the broadcaster's Events API token. The example maps this credential to `LOUNGE_EVENTS_TOKEN`. |

Multiple broadcasters can use separate tokens. For example, `"env": {"TWITCH_BOT_LOGIN": "STUDIO_LOGIN", "TWITCH_OAUTH_TOKEN": "STUDIO_READ_TOKEN"}` maps that room's requirements to your environment. Children inherit only their designated credentials and basic runtime settings. Discord and Telegram alert credentials stay in the supervisor.

## Local status and recovery

```sh
python3 -m socialbot fleet-status
python3 -m socialbot dashboard
```

For a custom database supply the same global `--db PATH` before each command. With custom rules, place `--rules PATH` before `fleet`. A second supervisor targeting the same database exits with an error. Keep the database and its adjacent `.fleet` directory on a persistent local disk with owner-only access. They contain room state, recent alert history and the event cursor.

| State | Meaning |
| --- | --- |
| `starting` | Worker started and awaits its first upstream response. |
| `running` | Recent Twitch protocol traffic or a successful API page confirms upstream progress, including empty polls. |
| `backoff` | Worker failed or missed its watchdog interval. Retry delays start at 2 seconds and cap at 120 seconds. A stable connection resets backoff. |
| `ended` | Listener exited normally. A completed broadcast stays stopped. |
| `supervisor stale` | The last supervisor update was over 30 seconds ago. |
| `stopped` | The supervisor shut down the worker. |

The default watchdog allows 600 seconds between upstream responses. Choose a timeout above your provider's quiet-room heartbeat and longest poll interval. If the supervisor dies, children exit when its private pipe closes. The next supervisor can then acquire the database lock. IRC disconnects can still lose messages, and reconnection remains subject to each platform's available history.

## Docker Compose

Use `compose.fleet.yaml` as a standalone Compose file. Copy `examples/fleet.json` to `fleet.json` in the project root and edit it. Copy `.env.example` to `.env` if you need a starting point, then add the room-specific variables used in your JSON. Use permissions such as `chmod 600 .env`. Operators with Docker daemon access can inspect the container environment.

```sh
docker compose -f compose.fleet.yaml build
docker compose -f compose.fleet.yaml run --rm listeners fleet --config /app/fleet.json --check
docker compose -f compose.fleet.yaml up -d
docker compose -f compose.fleet.yaml exec listeners python -m socialbot fleet-status
docker compose -f compose.fleet.yaml logs --tail 40
docker compose -f compose.fleet.yaml down
```

The volume keeps the event store and fleet state across container replacements. The configuration is mounted read-only and the supervisor uses outbound connections without publishing a host port. After editing a bind-mounted JSON file or changing credentials run `docker compose -f compose.fleet.yaml up -d --force-recreate`.

## Optional operator notifications

Enable recipients and events explicitly in `fleet.json`. Leaving both lists empty disables notifications.

```json
"alerts": {
  "events": ["gift", "error"],
  "destinations": ["telegram", "discord"]
}
```

For Telegram, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for an operator conversation where your bot can post. For Discord, use `DISCORD_WEBHOOK_URL` for the operator's destination channel. Notifications contain only an event category, configured room alias and platform. Use a neutral alias if you want to keep the broadcaster's public identity out of notifications. Gift notifications read only newly committed live events for configured rooms. Imported samples, viewer names, gift amounts, message bodies and credential values are excluded.

One incident alert is created per continuous worker outage. Each committed gift has a stable deduplication key for each enabled destination. The alert sender runs separately from ingestion. `fleet-status` shows counts for `pending`, `attempting`, `sent`, `expired`, `failed` and `uncertain` deliveries when present. A rate-limit response can delay a retry up to five times. Ambiguous deliveries, including a process interruption during a send, stay `uncertain` to prevent accidental repeats. Pending alerts survive restarts with unchanged subscriptions. Changing subscribed rooms, recipient credentials or alert categories expires the prior pending queue. First enablement starts at the current end of the event store.

The provider APIs describe Telegram's protected messages, Discord webhook delivery receipts and allowed mentions. [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage) · [Discord Execute Webhook](https://docs.discord.com/developers/resources/webhook#execute-webhook)

Add `returning_supporter` to the configured alert events to flag a live room entry after a prior attributed gift by the same viewer in that room. The operator notice identifies the room only.
