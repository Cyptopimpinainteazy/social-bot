# BIGO and other creator feeds with Social Stream Ninja

If your broadcast already appears in Social Stream Ninja (SSN), you can bring its public chat into Social Bot for keyword drafts, viewer counts and local review. This works through a broadcaster-selected SSN Chat History export or the dock's Generic POST output. Social Bot does not log in to the streaming platform. The supported source types are `bigo`, `tiktok`, `stripchat`, `whatnot`, `cam4`, `camsoda`, `bongacams`, `fansly`, `myfreecams` and `livejasmin`. [SSN supported sources](https://socialstream.ninja/docs/supported-sites.html)

## Read a history export

In SSN Chat History, filter to your public room, select a time range, choose JSON, and download. Then run the following commands from the project root, using the downloaded filename and your room label.

```sh
python3 -m socialbot creator-import bigo chat_export.json --channel studio
python3 -m socialbot drafts --status pending
```

The importer reads SSN's JSON array and reports added, duplicate, ignored and invalid rows. Exports from the same capture installation can overlap safely. The reader prefers the source message ID. Where that is unavailable, it uses the pair of SSN's row ID and its captured millisecond timestamp. It skips rows without either form of identity. Different installations can assign different IDs to the same upstream message. [SSN history export implementation](https://github.com/steveseguin/social_stream/blob/main/chathistory.js)

Use a single-room export or feed for each room label. When the record contains SSN's optional `sourceName`, you can require an exact match by adding `--source-name 'Studio Live'`. A missing or different source name then causes the row to be skipped. The reader retains attributable public chat text. Private-message markers, event notices, donations, avatars and other metadata stay outside the review inbox. Imported chat has no native-platform posting rights and cannot create viewer opt-ins. [SSN source fields](https://socialstream.ninja/docs/event-reference.html)

## Receive selected messages during the stream

On the same machine as the SSN dock, run the receiver below. Use the printed private destination as the dock's **Generic POST** URL. Keep the token-bearing URL out of shared overlays and logs. The binding is IPv4 loopback. Set `SOCIALBOT_SSN_TOKEN` to a stable private URL-safe value of at least 24 characters if you want the destination to survive a listener restart.

```sh
python3 -m socialbot creator-listen bigo --channel studio --port 8766
```

For a hosted dock, SSN documents the `postserver` dock URL parameter. Set its value to the URL-encoded private destination. Select a public chat message in the dock to forward it. The default allowed browser origin is `https://socialstream.ninja`. A self-hosted SSN dock needs `--origin` set to its exact HTTP or HTTPS origin. Web browsers may also ask permission for the dock to reach localhost. For a dock running on a different computer, use the file export workflow or run Social Bot on the dock's machine. [SSN external service integration](https://socialstream.ninja/docs/commands.html)

A successful POST returns HTTP 200, including on replay. The creator's dock controls which messages it forwards. Posts without stable IDs, out-of-scope platform records and unsupported event types are ignored. See the resulting local records with `python3 -m socialbot events`. Upstream account sessions and live message availability depend on your SSN setup. The automated tests exercise the JSON format and real loopback HTTP using fixtures.

The repository includes `examples/bigo-chat-history.json`, a synthetic export you can replay without connecting an account.
