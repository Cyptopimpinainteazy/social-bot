# Viewer opt-ins and room campaigns

## Welcome and follow-up

Tell viewers what opting in means before asking them to type a command. For example, “Type !optin if you'd like a welcome and one follow-up from this room. Type !optout any time.” The listener accepts the full chat message, ignoring capitalization and surrounding spaces. It attributes the command to its sender's platform identity in that room. Verified Twitch, YouTube and Chaturbate inputs can establish consent. Replaying a file cannot opt someone in.

A new opt-in creates a pending welcome. After 24 hours the scheduler can create one follow-up for that consent period. Its wording acknowledges support or a later visit when observed. Welcomes expire after one day, and follow-ups expire seven days after opt-in. A new opt-in following withdrawal starts a new period. Opting out or a creator revocation closes pending and approved personal items in the affected room. Historical queue entries retain their audit state.

From the repository root, inspect the local queue and approve a message for copying. Replace the example queue ID or viewer key with the one printed by your installation.

```sh
python3 -m socialbot community members twitch mychannel
python3 -m socialbot community queue twitch mychannel --status pending
python3 -m socialbot community approve 17
python3 -m socialbot community copy 17
python3 -m socialbot community revoke twitch mychannel id:12345
```

`copy` prints the text to stdout and marks the item copied. Personal welcomes and follow-ups are for the creator to handle directly. The bot sends no direct messages through this workflow.

## Schedule room announcements

Campaigns belong to a single room. Create content, inspect its ID, then approve it for rotation. The second example appends a visible affiliate disclosure and HTTPS link to the public text. Replace the example URL with your own link.

```sh
python3 -m socialbot community campaign-add twitch mychannel 'See the channel bio for our schedule.'
python3 -m socialbot community campaign-add twitch mychannel 'Here is the equipment list we mentioned.' --affiliate --link https://example.org/creator
python3 -m socialbot community campaigns twitch mychannel
python3 -m socialbot community campaign-approve 1
python3 -m socialbot community campaign-approve 2
python3 -m socialbot community schedule twitch mychannel --interval-sec 900 --quiet-sec 60 --fresh-sec 600
python3 -m socialbot community watch --every-sec 30
```

Run the watcher alongside your listener using the same SQLite database. For a single scheduler pass, run `python3 -m socialbot community tick`. The default slot opens every 15 minutes. A slot requires an event received from the room's live connector within the last 10 minutes and 60 seconds since the most recent live chat message. Recent receipt is a signal of activity, not an independent check of broadcast status. A busy room retains its due slot until it is quiet. Each eligible slot selects the next approved campaign in rotation. One outstanding queue entry per room prevents a buildup while you are away. The watcher also expires stale entries.

Each generated announcement has its own queue ID and approval. Select it for manual copying on any connected platform. To send a public chat message to Twitch, inspect and approve the item, then send it explicitly.

```sh
python3 -m socialbot community queue twitch mychannel --status pending
python3 -m socialbot community approve 21
python3 -m socialbot community send 21
```

`send` requires the matching broadcaster's `TWITCH_OWNER_TOKEN` with `user:write:chat`. The room must still be quiet and recently active, the campaign approved, and the item unexpired. The sender claims the item before its network request and records Twitch's message ID on confirmation. This form of announcement appears as ordinary chat text. **During Twitch Shared Chat, messages sent with this user token reach all participating channels.** [Twitch Send Chat Message](https://dev.twitch.tv/docs/api/reference/chat/#send-chat-message)

Announcement sends allow at least 60 seconds between chat attempts and count against the room's 20 attempts per hour. A rejected credential check releases the reservation. A missing acknowledgement marks the item uncertain. A scheduler pass marks any send still in progress after 10 minutes uncertain. These items cannot be retried by ID. Inspect the room before deciding what to do next.

```sh
python3 -m socialbot community dismiss 22
python3 -m socialbot community campaign-archive 2
python3 -m socialbot community queue twitch mychannel --status uncertain
```

Archiving a campaign removes it from future rotations and revokes its outstanding pending or approved announcement. An in-flight network send can already have reached Twitch.

## Viewer-requested follow-backs

A viewer can type `!followback` in a connected room and withdraw the pending request with `!cancelfollowback`. This request applies only to that viewer and room, separately from welcome and follow-up consent. An unanswered request expires after seven days. Read the list, decide whether to follow the viewer in the platform, then record the outcome locally. A member of the creator's team can also close an unfulfilled request. The `followback-mark` command only changes the local record.

```sh
python3 -m socialbot community followbacks twitch mychannel
python3 -m socialbot community followback-mark twitch mychannel id:12345 followed
python3 -m socialbot community followbacks twitch mychannel --status followed
python3 -m socialbot community followback-mark twitch mychannel id:54321 closed
```

## Publish timed announcements to one Twitch room

Once you have reviewed and approved the campaigns to rotate, you can authorize the watcher to publish their due entries automatically as the broadcaster. Start the listener, set the matching `TWITCH_OWNER_TOKEN`, then run the watcher with the specific Twitch login.

```sh
python3 -m socialbot community watch --every-sec 30 --publish-twitch mychannel
```

This option authorizes individual scheduled entries from already-approved campaigns for this run. The watcher can pick up an existing pending announcement for that room. It reserves each eligible entry in the database before sending it, checks for recent live activity and a quiet chat, and honors the same channel attempt limits as a manual send. Welcome and follow-up messages stay in the local review queue. A dismissed entry stays dismissed. Future scheduled occurrences can still be generated until you archive the campaign.

Stop the watcher with Ctrl+C to end automatic publishing. Stopping does not retract a sent or in-flight message. If delivery is uncertain, the watcher holds further automatic sends for that campaign. Check Twitch's room history before archiving or replacing that campaign. Shared Chat can distribute each message to participating channels. Without `--publish-twitch`, the watcher only schedules local review items.
