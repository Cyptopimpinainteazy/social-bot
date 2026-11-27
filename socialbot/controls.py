"""Local dashboard forms for editing one selected configuration and reviewing drafts."""
from contextlib import closing
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
from threading import Lock

from . import engine, ops

def revision(data):
    return hashlib.sha256(data).hexdigest()

class Controls:
    def __init__(self, db_path, *, edit_rules=None, edit_fleet=None, review=False):
        self.db_path = Path(db_path)
        self.files = {key:Path(value) for key, value in (("rules",edit_rules),("fleet",edit_fleet)) if value}
        self.review = review
        self.token = secrets.token_urlsafe(32)
        self.lock = Lock()
        if len({p.resolve() for p in self.files.values()})!=len(self.files):
            raise ValueError("Use separate rules and fleet configuration files")
        for kind,path in self.files.items():
            if not path.exists() and kind == "rules":
                path.parent.mkdir(parents=True,exist_ok=True)
                with path.open("xb") as new:
                    new.write(engine.DEFAULT_RULES.read_bytes())
            self.content(kind)
            (engine.load_rules if kind=="rules" else ops.load_config)(path)

    def content(self, kind):
        with self.files[kind].open("rb") as handle:
            source=handle.read(65537)
        if len(source)>65536:
            raise ValueError("Configuration exceeds 64 KiB")
        return source.decode("utf-8")

    def save(self, kind, body, version):
        if kind not in self.files or len(body.encode())>65536:
            raise ValueError("Unsupported editor or oversized configuration")
        path = self.files[kind]
        with self.lock:
            # Prevent two Social Bot processes from overwriting concurrent form edits.
            import fcntl
            with path.with_name(path.name+".lock").open("a") as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                if revision(self.content(kind).encode()) != version:
                    raise ValueError("Configuration changed since this page was opened. Refresh before saving.")
                raw=body.replace("\r\n","\n").encode()
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staged:
                    temp=Path(staged.name)
                try:
                    temp.write_bytes(raw)
                    (engine.load_rules if kind=="rules" else ops.load_config)(temp)
                    os.replace(temp,path)
                finally:
                    temp.unlink(missing_ok=True)

    def form(self, kind):
        from .dashboard import _text, STYLES
        fields = f'<input type="hidden" name="token" value="{self.token}">'
        if kind=="review" and self.review:
            uri = self.db_path.resolve().as_uri()+"?mode=ro"
            with closing(sqlite3.connect(uri,uri=True)) as db:
                rows=db.execute("""SELECT id,platform,channel,body FROM drafts WHERE status='pending'
                                  ORDER BY id DESC LIMIT 50""").fetchall()
            blocks = [f'<form method="post" class="panel card">{fields}<input type="hidden" name="id" value="{identifier}">'
                      f'<h2>Draft {identifier}</h2><div class="meta">{_text(platform)} / {_text(channel)}</div>'
                      f'<label>Reply<textarea class="reply" name="body" maxlength="500">{_text(body)}</textarea></label>'
                      f'<input type="hidden" name="draft_version" value="{revision(body.encode())}">'
                      f'<button name="action" value="edit">Save edit</button> <button name="action" value="approve">Approve</button> '
                      f'<button name="action" value="dismiss">Dismiss</button></form>'
                      for identifier,platform,channel,body in rows]
            title, contents = "Review pending replies", "".join(blocks) or "<p>No pending replies.</p>"
        elif kind in self.files:
            title = "Reply rules" if kind=="rules" else "Room listeners"
            source = self.content(kind)
            detail = ("Listeners read saved rules on their next event." if kind=="rules" else
                      "Restart the fleet to apply room changes. Reference credential environment variables by name.")
            contents = (f'<form method="post">{fields}<input type="hidden" name="version" value="{revision(source.encode())}">'
                        f'<label for="settings">{detail}</label><textarea id="settings" name="settings" required>'
                        f'{_text(source)}</textarea><button>Save configuration</button></form>')
        else:
            raise KeyError(kind)
        return (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{title} | Social Bot</title><style>{STYLES} textarea{{display:block;width:100%;min-height:58vh;'
                'font: .9rem monospace;margin:1rem 0;padding:1rem}.reply{min-height:4rem}button{font:inherit;padding:.4rem .8rem}</style>'
                f'<main class="shell"><div class="title"><h1>{title}</h1><a href="/">Dashboard</a></div>{contents}</main></html>')

    def submit(self, kind, values):
        if not hmac.compare_digest(self.token.encode(), values.get("token","").encode()):
            raise PermissionError("Reload the form and try again")
        if kind=="review" and self.review:
            with closing(engine.connect(self.db_path)) as db:
                action, identifier = values["action"],int(values["id"])
                # An approve click must refer to the content actually reviewed.
                if action in ("edit","approve") and "draft_version" in values:
                    with db:
                        db.execute("BEGIN IMMEDIATE")
                        item = db.execute("SELECT body,status FROM drafts WHERE id=?", (identifier,)).fetchone()
                        if (not item or item["status"] != "pending" or
                                revision(item["body"].encode()) != values["draft_version"]):
                            raise ValueError("Draft changed. Refresh before editing or approving.")
                        if action=="edit":
                            target = db.execute("SELECT platform FROM drafts WHERE id=?",(identifier,)).fetchone()[0]
                            body = engine.field(values["body"],"reply",200 if target=="youtube" else 500)
                            db.execute("UPDATE drafts SET body=? WHERE id=?",(body,identifier))
                        else:
                            if values.get("body") != item["body"]:
                                raise ValueError("Save the edited reply before approving it")
                            db.execute("UPDATE drafts SET status='approved' WHERE id=?",(identifier,))
                elif action=="dismiss":
                    engine.review(db,identifier,action)
                else:
                    raise ValueError("Reload the review form")
        elif kind in self.files:
            self.save(kind,values["settings"],values["version"])
        else:
            raise KeyError(kind)
