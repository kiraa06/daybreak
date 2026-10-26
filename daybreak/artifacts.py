"""Artifacts: prep notes, reports, decks and scheduled-task results saved as HTML and served by the helper."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import time

from . import config
from .db import DB, new_id

PAGE_CSS = """
:root { color-scheme: dark; --bg:#121212; --card:#1d1d1d; --ink:#f2efe6; --muted:#a39f95; --accent:#f6e04b; --line:#2c2c2c; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font: 16px/1.6 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif; }
main { max-width: 860px; margin: 0 auto; padding: 56px 24px 96px; }
.kicker { font: 12px/1 "SF Mono", Menlo, monospace; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); }
h1 { font-family: "Iowan Old Style", "Hoefler Text", Charter, Georgia, serif; font-weight:600; font-size: 44px; line-height:1.1; margin:.35em 0 .3em; color:var(--accent); }
h2 { font-family: "Iowan Old Style", "Hoefler Text", Charter, Georgia, serif; font-style: italic; font-weight:500; font-size: 24px; margin: 40px 0 12px; }
h3 { font-size: 17px; margin: 24px 0 8px; }
p, li { color: #ddd8cc; }
.lede { font-family: "Iowan Old Style", Georgia, serif; font-style: italic; font-size: 20px; color: var(--ink); }
.card { background: var(--card); border:1px solid var(--line); border-radius: 16px; padding: 20px 24px; margin: 16px 0; }
ul { padding-left: 1.2em; } li { margin: 6px 0; }
a { color: var(--accent); }
table { border-collapse: collapse; width: 100%; } td, th { border-bottom:1px solid var(--line); padding: 8px; text-align:left; }
.people li strong { color: var(--ink); }
.meta { color: var(--muted); font-size: 14px; }
"""


def sign(artifact_id: str) -> str:
    return hmac.new(config.token().encode(), artifact_id.encode(), hashlib.sha256).hexdigest()[:20]


def verify(artifact_id: str, sig: str) -> bool:
    return hmac.compare_digest(sign(artifact_id), sig)


def url_for(cfg: config.Config, artifact_id: str) -> str:
    return f"http://127.0.0.1:{cfg.port}/a/{artifact_id}/{sign(artifact_id)}"


def sanitize_fragment(fragment: str) -> str:
    fragment = re.sub(r"<\s*(script|style|iframe|object|embed)[^>]*>.*?<\s*/\s*\1\s*>", "", fragment, flags=re.S | re.I)
    fragment = re.sub(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", fragment, flags=re.I)
    fragment = re.sub(r"javascript:", "", fragment, flags=re.I)
    return fragment


def wrap_page(title: str, kicker: str, body_html: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head><body><main>"
            f"<div class='kicker'>{html.escape(kicker)}</div><h1>{html.escape(title)}</h1>{body_html}</main></body></html>")


def save(db: DB, kind: str, title: str, page_html: str, meta: dict | None = None) -> str:
    aid = new_id()
    path = config.ARTIFACTS_DIR / f"{aid}.html"
    path.write_text(page_html)
    db.x("INSERT INTO artifacts(id, kind, title, path, created, meta) VALUES(?,?,?,?,?,?)",
         (aid, kind, title[:200], str(path), time.time(), json.dumps(meta or {})))
    return aid


def _list(items, cls: str = "") -> str:
    lis = "".join(f"<li>{html.escape(str(i))}</li>" for i in items or [] if str(i).strip())
    return f"<ul class='{cls}'>{lis}</ul>" if lis else ""


def render_prep(meeting: dict, prep: dict, when: str) -> str:
    e = html.escape
    parts = [f"<p class='meta'>{e(when)}"]
    if meeting.get("join_url"):
        parts.append(f" · <a href='{e(meeting['join_url'])}'>Join</a>")
    parts.append("</p>")
    if prep.get("headline"):
        parts.append(f"<p class='lede'>{e(prep['headline'])}</p>")
    if prep.get("why_it_matters"):
        parts.append(f"<div class='card'><p>{e(prep['why_it_matters'])}</p></div>")
    for key, label in (("context", "Context"), ("open_items", "Likely to come up"), ("agenda", "Suggested agenda"),
                       ("ask", "Ask or raise")):
        if prep.get(key):
            parts.append(f"<h2>{label}</h2>{_list(prep[key])}")
    people = prep.get("people") or []
    if people:
        lis = "".join(f"<li><strong>{e(p.get('name', ''))}</strong> — {e(p.get('note', ''))}</li>" for p in people if isinstance(p, dict))
        parts.append(f"<h2>People</h2><ul class='people'>{lis}</ul>")
    links = prep.get("links") or []
    if links:
        lis = "".join(f"<li><a href='{e(l.get('url', ''))}'>{e(l.get('title') or l.get('url', ''))}</a></li>"
                      for l in links if isinstance(l, dict) and str(l.get("url", "")).startswith("http"))
        if lis:
            parts.append(f"<h2>Links</h2><ul>{lis}</ul>")
    return wrap_page(meeting.get("title", "Meeting"), "Meeting prep", "".join(parts))
