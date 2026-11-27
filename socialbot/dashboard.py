"""Read-only local dashboard for a Social Bot SQLite database."""

import html
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


STYLES = """
:root{color-scheme:light;font:16px/1.5 "Inter","Segoe UI",sans-serif;color:#172c28;background:#f5f7f4}
*{box-sizing:border-box}
body{margin:0}
.shell{max-width:1200px;margin:auto;padding:0 clamp(14px,4vw,48px)}
header{background:#142f29;color:#f5f9f5}
.bar,.brand,nav,.title,.section-title{display:flex;align-items:center}
.bar{justify-content:space-between;gap:12px;min-height:64px;flex-wrap:wrap;padding-block:10px}
.brand{gap:11px;font-size:1.05rem;font-weight:750;letter-spacing:-.02em}
.wave{display:flex;align-items:center;gap:3px;height:24px}
.wave i{display:block;width:4px;background:#a4e9b6;border-radius:4px;height:10px}
.wave i:nth-child(2){height:22px}.wave i:last-child{height:16px}
nav{gap:16px;flex-wrap:wrap}
nav a{color:#e0f2e7;font-size:.86rem;font-weight:600}
a{color:#23503f;text-underline-offset:3px}
a:focus-visible,[tabindex]:focus-visible{outline:3px solid #efab43;outline-offset:3px}
main{padding-bottom:48px}
.title,.section-title{justify-content:space-between;gap:12px;flex-wrap:wrap}
.title{margin:34px 0 19px}.title a{font-size:.86rem;font-weight:650}
h1,h2,h3,p{margin:0}h1{font-size:clamp(1.85rem,4.6vw,3rem);letter-spacing:-.05em;line-height:1.13}
h2{font-size:1.25rem;letter-spacing:-.035em}h3{font-size:1rem}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.card,.panel{background:#fff;border:1px solid #cfdbd2;border-radius:13px}
.card{padding:16px 20px}.metric{font-size:clamp(1.6rem,4vw,2.25rem);font-weight:720;letter-spacing:-.05em}
.card:first-child{background:#e1f2e4;border-color:#bcd8c3}
.label,.meta,.time,footer{color:#4c6359;font-size:.84rem}
.label{font-weight:600}.metric,.num,.time{font-variant-numeric:tabular-nums}
.panel{margin-top:18px;overflow:hidden}
.section-title{padding:17px 20px;border-bottom:1px solid #dfe6e0}
.region{overflow-x:auto}
table{width:100%;border-collapse:collapse;text-align:start;font-size:.91rem}
th,td{padding:9px 16px;text-align:left;border-bottom:1px solid #e5ebe6;vertical-align:top}
th{background:#f5f8f5;color:#42594e;font-size:.78rem;font-weight:650}
tr:last-child td{border-bottom:0}
.num{text-align:right;white-space:nowrap}
td,.feed li{overflow-wrap:anywhere}
.split{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
.feed{list-style:none;margin:0;padding:0}
.feed li{padding:13px 20px;border-bottom:1px solid #e3eae4}
.feed li:last-child{border:0}
.line{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.badge{display:inline-block;border-radius:6px;padding:2px 7px;background:#edf1ee;color:#384f44;font-size:.75rem;font-weight:700}
.pending,.uncertain{background:#fff0ca;color:#674600}
.approved,.sent{background:#ddf0e1;color:#215737}
.body{white-space:pre-wrap;margin:6px 0}
.empty{padding:18px 20px;color:#52675c}
footer{padding:18px 0}
@media(max-width:750px){.split{grid-template-columns:1fr;gap:0}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.card{padding:12px}.bar{gap:8px}.title{margin-top:24px}th,td{padding:8px 10px}}
"""


# Each row describes one channel. Aggregate before joining so one extra draft
# can never multiply the number of viewers or events in a channel.
CHANNEL_METRICS = """
WITH pieces(platform, channel, events, gifts, viewers, drafts, pending, latest) AS (
  SELECT platform, channel, count(*), sum(kind='gift'), 0, 0, 0, max(received_at)
    FROM events GROUP BY platform, channel
  UNION ALL
  SELECT platform, channel, 0, 0, count(*), 0, 0, max(last_seen)
    FROM viewers GROUP BY platform, channel
  UNION ALL
  SELECT platform, channel, 0, 0, 0, count(*), sum(status='pending'), max(created_at)
    FROM drafts GROUP BY platform, channel
), channels AS (
  SELECT platform, channel, sum(events) AS events, sum(gifts) AS gifts,
         sum(viewers) AS viewers, sum(drafts) AS drafts, sum(pending) AS pending,
         max(latest) AS latest
    FROM pieces GROUP BY platform, channel
)
"""


def _text(value):
    return html.escape(str(value), quote=True) if value is not None else ""


def _stamp(seconds):
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "Unknown"


def _snapshot(db_path):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        totals = db.execute("""
            SELECT (SELECT count(*) FROM events) AS events,
                   (SELECT count(*) FROM events WHERE kind='gift') AS gifts,
                   (SELECT count(*) FROM drafts) AS drafts,
                   (SELECT count(*) FROM drafts WHERE status='pending') AS pending
        """).fetchone()
        platforms = db.execute(CHANNEL_METRICS + """
            SELECT platform, count(*) AS channels, sum(events) AS events,
                   sum(gifts) AS gifts, sum(viewers) AS viewers, sum(drafts) AS drafts,
                   sum(pending) AS pending FROM channels GROUP BY platform
             ORDER BY events DESC, platform LIMIT 100
        """).fetchall()
        channels = db.execute(CHANNEL_METRICS + """
            SELECT * FROM channels ORDER BY latest DESC, platform, channel LIMIT 100
        """).fetchall()
        drafts = db.execute("""
            SELECT id, platform, channel, substr(body, 1, 480) AS body, status,
                   substr(reason, 1, 180) AS reason, created_at FROM drafts
             ORDER BY created_at DESC, id DESC LIMIT 12
        """).fetchall()
        events = db.execute("""
            SELECT platform, channel, kind, substr(viewer, 1, 100) AS viewer,
                   substr(text, 1, 300) AS text, received_at FROM events
             ORDER BY received_at DESC, platform, channel, event_id DESC LIMIT 12
        """).fetchall()
    return totals, platforms, channels, drafts, events


def _table(title, anchor, headers, rows, numeric, caption, empty):
    columns = "".join(
        f'<th scope="col" class="{"num" if index in numeric else ""}">{_text(label)}</th>'
        for index, label in enumerate(headers)
    )
    lines = []
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            if index in numeric:
                cells.append(f'<td class="num">{value:,}</td>')
            else:
                cells.append(f'<td>{_text(value)}</td>')
        lines.append("<tr>" + "".join(cells) + "</tr>")
    if lines:
        content = (f'<div class="region" role="region" aria-labelledby="{anchor}" tabindex="0">'
                   f'<table><thead><tr>{columns}</tr></thead><tbody>{"".join(lines)}</tbody></table></div>')
    else:
        content = f'<p class="empty">{empty}</p>'
    return (f'<section class="panel"><div class="section-title"><h2 id="{anchor}">{title}</h2>'
            f'<span class="meta">{caption}</span></div>{content}</section>')


def _feed(title, anchor, entries, empty):
    items = '<ul class="feed">' + "".join(entries) + '</ul>' if entries else f'<p class="empty">{empty}</p>'
    return (f'<section class="panel"><div class="section-title"><h2 id="{anchor}">{title}</h2>'
            f'<span class="meta">Latest 12</span></div>{items}</section>')


def _draft_line(row):
    status = row["status"]
    css = status if status in {"sent", "approved", "pending", "uncertain"} else ""
    return (f'<li><div class="line"><strong>Draft #{int(row["id"])}</strong>'
            f'<span class="badge {css}">{_text(status)}</span></div>'
            f'<p class="body">{_text(row["body"])}</p><div class="meta">'
            f'{_text(row["platform"])} / {_text(row["channel"])} · {_stamp(row["created_at"])} UTC'
            + (f' · {_text(row["reason"])}' if row["reason"] else "") + '</div></li>')


def _event_line(row):
    body = f'<p class="body">{_text(row["text"])}</p>' if row["text"] else ""
    return (f'<li><div class="line"><strong>{_text(row["viewer"])}</strong>'
            f'<span class="badge">{_text(row["kind"])}</span></div>{body}<div class="meta">'
            f'{_text(row["platform"])} / {_text(row["channel"])} · {_stamp(row["received_at"])} UTC'
            '</div></li>')


def render_dashboard(db_path):
    """Return a consistent HTML snapshot. Opening a missing database raises IOError/SQLite error."""
    totals, platforms, channels, drafts, events = _snapshot(db_path)
    cards = "".join(
        f'<div class="card"><p class="label">{label}</p><p class="metric">{totals[key]:,}</p></div>'
        for label, key in (("Events received", "events"), ("Gift events", "gifts"),
                           ("Reply drafts", "drafts"), ("Pending review", "pending"))
    )
    platforms_table = _table(
        "Platforms", "platforms", ("Platform", "Channels", "Events", "Gifts", "Viewer records", "Drafts", "Pending"),
        platforms, set(range(1, 7)), "Most events first · Up to 100", "Platforms appear after the first event."
    )
    channels_table = _table(
        "Channels", "channels", ("Platform", "Channel", "Events", "Gifts", "Viewers", "Drafts", "Pending"),
        (tuple(row[k] for k in ("platform", "channel", "events", "gifts", "viewers", "drafts", "pending"))
         for row in channels),
        set(range(2, 7)), "Most recent first · Up to 100", "Channel activity will appear here."
    )
    draft_feed = _feed("Recent drafts", "drafts", [_draft_line(row) for row in drafts], "No reply drafts yet.")
    event_feed = _feed("Recent activity", "activity", [_event_line(row) for row in events], "No events received yet.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Creator dashboard | Social Bot</title>
<style>{STYLES}</style></head><body>
<header><div class="shell bar"><div class="brand"><span class="wave" aria-hidden="true"><i></i><i></i><i></i></span>Social Bot</div>
<nav aria-label="Dashboard sections"><a href="#platforms">Platforms</a><a href="#channels">Channels</a><a href="#drafts">Drafts</a><a href="#activity">Activity</a></nav></div></header>
<main class="shell"><div class="title"><h1>Creator dashboard</h1><a href="/">Refresh dashboard</a></div>
<div class="cards">{cards}</div>{platforms_table}{channels_table}<div class="split">{draft_feed}{event_feed}</div>
<footer>All activity times in UTC. Viewer records count a viewer separately in each channel.</footer>
</main></body></html>"""


def _handler(db_path):
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._respond()

        def do_HEAD(self):
            self._respond(headers_only=True)

        def _respond(self, headers_only=False):
            host = self.headers.get("Host", "").partition(":")[0].lower()
            if host not in {"127.0.0.1", "localhost"}:
                self.send_error(403)
                return
            if urlsplit(self.path).path != "/":
                self.send_error(404)
                return
            try:
                data = render_dashboard(db_path).encode("utf-8")
                code = 200
            except (OSError, sqlite3.Error):
                data = b"<html lang='en'><title>Dashboard unavailable</title><p>Database unavailable. Check the database path and initialize the project.</p></html>"
                code = 503
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            if not headers_only:
                self.wfile.write(data)

    return DashboardHandler


def serve_dashboard(db_path, port=8080):
    """Serve a local dashboard until interrupted."""
    with ThreadingHTTPServer(("127.0.0.1", port), _handler(db_path)) as server:
        print(f"Creator dashboard at http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
