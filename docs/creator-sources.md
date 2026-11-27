# Creator sources

| Source | What the operator sees | How it connects |
| --- | --- | --- |
| Fanvue | Cumulative unique follow events since the receiver was installed | Signed creator-authorized webhook |
| Tango | Cumulative new follows and viewer-authored live stream chat message counts | Signed webhook for an approved creator |
| TikTok | Creator's current follower, following, like and public video counts | OAuth-authenticated Display API |
| Twitch discovery | Live room name, title, viewers and link in the selected category and language | Helix stream directory |
| Chaturbate V2 | A viewer clicks **Room info** and receives the creator's configured notice | Native shortcut installed by the broadcaster |

These integrations have local fixtures and tests. Production delivery requires creator credentials or installation in the platform. The Fanvue and Tango totals reflect events this receiver actually received. They cannot reconcile activity while it was offline or recover an account's current follower total after unfollows.

## Fanvue

1. Register the app in Fanvue's Developer Area. Authorize it as the intended creator through Fanvue's OAuth consent screen with the **read:creator** permission.
2. In the app's Events tab add your public HTTPS destination, select **creator.follow.created**, and copy the signing secret from **View signing secret** to your server's secret store. Provide it through `FANVUE_WEBHOOK_SECRET`. Provide the authorized account UUID through `FANVUE_CREATOR_UUID`.
3. Run the receiver below. Route the public destination's `/webhooks/fanvue` path to the same path on `127.0.0.1:8092`. Use the Events tab's Test action. The console acknowledges a valid signed test and excludes it from the follow count.
4. Use the stats command in another terminal with the same SQLite file and creator UUID.

```bash
python3 -m socialbot fanvue-webhook
python3 -m socialbot fanvue-stats
```

You may also use an existing API-created subscription. Set the per-subscription signing secret for that route. The receiver acknowledges Fanvue's unsigned verification probe without changing a count. Signed follows use the raw JSON body and signature timestamp. Duplicate delivery remains idempotent across restarts with the same database. Receipt rows contain only a digest of the authorized creator UUID and a digest of the platform event ID. The incoming follower, tracking and message fields are excluded from storage and console output. [Registration and testing](https://api.fanvue.com/docs/webhooks) · [Payload reference](https://api.fanvue.com/docs/creator/engagement) · [Signature verification](https://api.fanvue.com/docs/webhooks/signature-verification) · [Retries](https://api.fanvue.com/docs/webhooks/delivery-and-idempotency)

## Tango

1. Connect the intended creator in Tango Agent. The creator approves the relationship inside Tango. Obtain the creator's encrypted ID from the account connection and put it in `TANGO_CREATOR_ID`.
2. Register a public HTTPS base URL ending in `/webhooks/tango` in the Tango Agent Webhooks page, selecting **follower.added** and **stream.chat.messages.received**. Tango probes the base URL with `OPTIONS` before saving the destination. The proxy must forward both this base path and `/webhooks/tango/<creator-id>` to loopback port 8093. If you are registering a new destination and do not yet have its secret, start the receiver with a temporary randomly generated local secret to answer the setup probe. Save the issued Tango signing secret to `TANGO_WEBHOOK_SECRET` and restart the receiver immediately.
3. Run the commands below. The callback verifies Tango's signature and checks the creator ID in the signed body against the configured account. It counts each non-system item in a live chat batch once, accepts follow events and ignores direct messages. Only creator and event ID digests, event kind and count enter SQLite.

```bash
python3 -m socialbot tango-webhook
python3 -m socialbot tango-stats
```

Both listeners accept `--port` and `--creator`. Expose the intended callback paths through your HTTPS reverse proxy. The dashboard can remain local. [Tango for Business developer guide, Webhooks](https://business.tango.me/developers/docs) · [Creator approval and capabilities](https://www.tango.me/blog/tango-trends/tango-new-features-august-2026)

## TikTok

Use TikTok Login Kit to obtain an access token for the account that authorized `user.info.stats`. Supply it through `TIKTOK_ACCESS_TOKEN` and run the following command. The single GET requests only four counts from that account. A missing field, failed consent, expired token or upstream error produces an error rather than a fabricated zero.

```bash
python3 -m socialbot tiktok-stats
```

LIVE chat uses a different product surface. This command reads account metrics. [Get User Info](https://developers.tiktok.com/docs/en/tiktok-api-v2-get-user-info) · [Scopes](https://developers.tiktok.com/docs/en/tiktok-api-scopes)

## Twitch room discovery

Set `TWITCH_CLIENT_ID` and a matching app or user access token in `TWITCH_DISCOVERY_TOKEN`. The directory endpoint requires no chat or send permission. The following request returns one page of up to ten live rooms in Twitch category 509658 with an English-language broadcast. Review the returned links manually. Use `--limit` between 1 and 100 or omit `--language` to include all languages.

```bash
python3 -m socialbot discover-rooms --game-id 509658 --language en --limit 10
```

The room list is ordered by Twitch's current viewer counts and can change between requests. The command issues one directory request and does not enter any room. [Get Streams](https://dev.twitch.tv/docs/api/reference/streams) · [Getting an app token](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/)

## Chaturbate V2 room information

In the Chaturbate Developer Portal, create a V2 app and add a Shortcut named `room-info` with the label **Room info**. Put the contents of `integrations/chaturbate-v2/room-info.js` in the shortcut's **Code** handler. Edit the notice to state the actual schedule or link location, then click the shortcut as a testbed viewer. The expected result is a single notice visible to that viewer. The broadcaster installs the tested app in the intended room. The shortcut runs entirely inside Chaturbate and uses no Python process, API token or external call. The Python Events API listener is separate. [Shortcut handler and notice targeting](https://devportal.cb.dev/wiki/shortcuts) · [App lifecycle and testbed](https://devportal.cb.dev/wiki/app-lifecycle)

## Remaining live adapters

BIGO's public materials document its streamer message toolbar. A broadcaster-accessible chat event and send API for an independently registered app still needs verification. [BIGO Streamer](https://www.bigo.tv/bigo-streamer/connector/) · [BIGO integration inquiries](https://www.bigo.tv/blog/bigo-live-support)

Stripchat announced a beta extension platform with a selected developer group. Access to its extension SDK and event contract is needed to implement a native adapter here. The research also did not establish an official creator chat event interface for Streamate suitable for this project. [Stripchat's beta announcement](https://t.me/s/stripchatnews) · [Streamate performer resources](https://streamatehelp.com/)

## Chaturbate V2 automatic room notices

Install these additional handlers in the same V2 app as the Room info shortcut. Paste `integrations/chaturbate-v2/app-start.js` into **App Start**, `user-enter.js` into **User Enter**, and `tip-received.js` into **Tip Received**. Create the following setting names in the portal so the broadcaster can change the notices. A blank greeting or blank thanks disables that feature.

| Setting name | Type | Example value |
| --- | --- | --- |
| `greetingText` | Text | Welcome! You'll find the schedule in this room's bio. |
| `regularUsernames` | Text | A comma-separated list of names supplied by the broadcaster |
| `regularGreeting` | Text | Welcome back. Glad you could make it today! |
| `greetingCooldownSeconds` | Number | 15 |
| `tipThanksText` | Text | Thank you for supporting the stream! |
| `tipCooldownSeconds` | Number | 30 |

Entry notices are visible only to the viewer who entered. Regulars use the alternate text when their login matches a name the broadcaster explicitly configured. The tip notice appears in the room and gives no name, amount or personal details. Each handler has its own room-wide cooldown. During a burst of joins or tips, the next notice waits for the corresponding cooldown to elapse. Only two room timestamps enter the app's KV store. These handlers give every tip the same treatment. The public Events API has amounts, but the native V2 tip amount and anonymous flag were not independently verified here.

Run **App Start** first on the testbed. Enter once as a configured regular, then as an unlisted viewer after the greeting cooldown. Trigger two tips during the tip cooldown. Expect one room-wide acknowledgement. The local tests exercise these JavaScript handlers with platform objects supplied by a Node VM. [V2 app lifecycle](https://devportal.cb.dev/wiki/app-lifecycle) · [Native app settings](https://devportal.cb.dev/wiki/) · [KV read/write examples](https://devportal.cb.dev/wiki/api/kv-clear) · [Viewer-targeted notices](https://devportal.cb.dev/wiki/shortcuts)
