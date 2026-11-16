"""The Morning Brief: today's painting plus a grounded summary of where the user's work stands."""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timedelta

import aiohttp

from . import config, prompts
from .claude import Claude, SleptThrough
from .db import DB, new_id
from .pipeline import QUIET_DAYS, day_label, last_activity, loop_id, loop_line, same_task

log = logging.getLogger("daybreak.brief")

AIC = "https://api.artic.edu/api/v1/artworks/search"
AIC_HEADERS = {"AIC-User-Agent": "Daybreak personal morning brief (local app)"}
THEMES = ["landscape", "village", "harbor", "garden", "river", "autumn", "summer", "snow", "seashore", "meadow",
          "mountains", "boats", "farm", "orchard", "city street", "sunrise", "forest", "coast", "picnic", "fields"]
CARRY_DAYS = 7      # an unfinished to-do is repeated this long at most, unless a re-check finds it still open
DONE_DAYS = 14      # how far back "you already did this" reaches
VERIFY_CLAIMS = 12  # how many open loops and carried to-dos are re-checked against their sources before writing
VERIFY_CALLS = 8
VERIFY_THREADS = 10  # Slack threads re-read through the browser for that re-check


async def fetch_painting(db: DB, day: str) -> dict:
    """Pick a wide, public-domain painting from the Art Institute of Chicago (CC0) and cache it for the day."""
    cached = db.get(f"painting:{day}")
    if cached and (config.PAINTINGS_DIR / f"{day}.jpg").exists():
        return cached
    used = set(db.get("paintings_used", []) or [])
    rng = random.Random(day)
    theme = rng.choice(THEMES)
    body = {"q": theme, "limit": 60, "fields": ["id", "title", "artist_title", "date_display", "image_id",
                                                 "medium_display", "thumbnail"],
            "query": {"bool": {"must": [{"term": {"is_public_domain": True}}, {"term": {"artwork_type_id": 1}}]}}}
    try:
        async with aiohttp.ClientSession(headers=AIC_HEADERS, timeout=aiohttp.ClientTimeout(total=40)) as s:
            async with s.post(AIC, json=body) as r:
                data = (await r.json()).get("data", [])
            wide = [a for a in data if a.get("image_id") and a["id"] not in used and a.get("thumbnail")
                    and a["thumbnail"].get("width") and a["thumbnail"].get("height")
                    and a["thumbnail"]["width"] / a["thumbnail"]["height"] >= 1.3]
            if not wide:
                raise RuntimeError(f"no wide paintings for theme {theme!r}")
            art = rng.choice(wide[:20])
            img_url = f"https://www.artic.edu/iiif/2/{art['image_id']}/full/1686,/0/default.jpg"
            async with s.get(img_url) as r:
                if r.status != 200:
                    raise RuntimeError(f"image fetch {r.status}")
                (config.PAINTINGS_DIR / f"{day}.jpg").write_bytes(await r.read())
    except Exception as e:
        log.warning("painting fetch failed: %s", e)
        prev = db.get("painting_last")
        return prev or {}
    meta = {"title": art.get("title", ""), "artist": art.get("artist_title") or "Unknown artist",
            "date": art.get("date_display", ""), "medium": (art.get("medium_display") or "").lower(),
            "source": f"https://www.artic.edu/artworks/{art['id']}", "file": f"{day}.jpg"}
    db.put(f"painting:{day}", meta)
    db.put("painting_last", meta)
    db.put("paintings_used", (list(used) + [art["id"]])[-400:])
    return meta


class Brief:
    def __init__(self, cfg: config.Config, db: DB, claude: Claude, me_fn, bridge=None):
        self.cfg, self.db, self.claude, self.me_fn, self.bridge = cfg, db, claude, me_fn, bridge

    def _day_bounds(self, day: datetime) -> tuple[float, float]:
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()

    def _mine(self, who: str) -> bool:
        who, me = str(who).strip().lower(), self.me_fn().lower()
        return who in ("me", "i", me) or (bool(who) and who == me.split(" ")[0])

    def _surfaced(self, day_iso: str) -> list[dict]:
        """Workstreams worth showing today: surfaced by the curator and still live (something real in the last few days)."""
        row = self.db.one("SELECT slugs FROM surface WHERE day = ?", (day_iso,)) or \
            self.db.one("SELECT slugs FROM surface ORDER BY day DESC LIMIT 1")
        slugs = json.loads(row["slugs"]) if row else []
        slugs += [r["slug"] for r in self.db.all(
            "SELECT slug FROM workstreams WHERE state IN ('active','waiting') AND overview != '' ORDER BY updated DESC LIMIT 6")]
        fresh, out = time.time() - QUIET_DAYS * 86400, []
        for slug in dict.fromkeys(slugs):
            ws = self.db.one("SELECT * FROM workstreams WHERE slug = ?", (slug,))
            if not ws or ws["state"] not in ("active", "waiting"):
                continue
            ws["active"] = last_activity(self.db, slug)
            if ws["active"] < fresh:
                continue
            ws["loops"] = json.loads(ws["open_loops"] or "[]")
            ws["shut"] = json.loads(ws["closed"] or "[]")
            out.append(ws)
        return out[:6]

    def _workstream_block(self, surfaced: list[dict], checks: dict, today: datetime) -> str:
        tz, blocks = self.cfg.tz, []
        for ws in surfaced:
            slug = ws["slug"]
            days = (today.date() - datetime.fromtimestamp(ws["active"], tz).date()).days
            age = "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"
            logs = self.db.all("SELECT ts, text FROM workstream_log WHERE slug = ? ORDER BY ts DESC LIMIT 3", (slug,))
            items = self.db.all("SELECT i.ref, i.title, i.state, i.url, i.updated FROM items i JOIN memberships m "
                                "ON m.ref = i.ref WHERE m.slug = ? ORDER BY i.updated DESC LIMIT 5", (slug,))
            loops = []
            for l in ws["loops"]:
                check = checks.get(f"L-{loop_id(l.get('text', ''))}")
                loops.append(f"- [L-{loop_id(l.get('text', ''))}] {loop_line(l)}"
                             + (f" · re-checked: still open — {check['evidence']}" if check else ""))
            timeline = " | ".join(f"{day_label(l['ts'], tz)}: {l['text']}" for l in reversed(logs))
            blocks.append(
                f"## {slug} — {ws['title']} · {ws['state']} · last activity {day_label(ws['active'], tz)} ({age})\n"
                f"{ws['overview']}\n"
                f"Timeline: {timeline or '-'}\n"
                "Open loops:\n" + ("\n".join(loops) or "(none)") + "\n"
                "Closed loops: " + ("; ".join(f"{c['text']} — closed {c['on']}: {c.get('why', '')}" for c in ws["shut"])
                                    or "(none)") + "\n"
                "Items: " + "; ".join(f"{i['ref']} '{i['title'][:60]}' ({i['state']}, as of {day_label(i['updated'], tz)}) "
                                      f"{i['url']}" for i in items))
        return "\n\n".join(blocks) or "(nothing has moved in the last few days — sources may still be being read)"

    def schedule(self, day: datetime) -> list[dict]:
        s, e = self._day_bounds(day)
        rows = self.db.all("SELECT * FROM meetings WHERE start >= ? AND start < ? ORDER BY start", (s, e))
        out = []
        for m in rows:
            att = json.loads(m["attendees"] or "[]")
            out.append({"id": m["id"], "time": datetime.fromtimestamp(m["start"], self.cfg.tz).strftime("%-I:%M%p").lower()[:-1],
                        "title": m["title"], "start": m["start"], "end": m["end"], "attendees": att,
                        "join_url": m["join_url"], "description": m["description"]})
        return out

    def _meetings_block(self, day: datetime) -> str:
        lines = [f"- {m['time']} · {m['title']} · id={m['id']} · {len(m['attendees'])} attendees: "
                 f"{', '.join(m['attendees'][:6])}{' · ' + m['description'][:160] if m['description'] else ''}"
                 for m in self.schedule(day)]
        if not lines and self.db.get("calendar_unavailable"):
            return "(calendar not connected — connect Google Calendar in Claude to see meetings here)"
        return "\n".join(lines) or "(no meetings)"

    def _carryover(self, day_iso: str) -> list[dict]:
        """Unfinished to-dos from the last brief, each with the day it was first raised."""
        prev = self.db.one("SELECT day FROM briefs WHERE day < ? ORDER BY day DESC LIMIT 1", (day_iso,))
        if not prev:
            return []
        rows = self.db.all("SELECT * FROM todos WHERE day = ? AND done = 0 ORDER BY position", (prev["day"],))
        history = self.db.all("SELECT day, text FROM todos WHERE day < ? ORDER BY day", (prev["day"],))
        for r in rows:
            r["cid"] = f"T-{r['id'][:6]}"
            # To-dos written before `since` was recorded: fall back to the first day the same thing was listed.
            r["since"] = r["since"] or next((h["day"] for h in history if same_task(h["text"], r["text"])), r["day"])
        return rows

    def _done(self, day_iso: str, days: int) -> list[dict]:
        """To-dos the user ticked off recently, newest first, one line per task."""
        cutoff = (datetime.fromisoformat(day_iso) - timedelta(days=days)).date().isoformat()
        out: list[dict] = []
        for r in self.db.all("SELECT text, day FROM todos WHERE done = 1 AND day >= ? ORDER BY day DESC", (cutoff,)):
            if not any(same_task(r["text"], o["text"]) for o in out):
                out.append(r)
        return out[:30]

    def _slack_refs(self, slug: str = "", url: str = "") -> list[str]:
        """The Slack conversations behind a claim: the one its link points at, else its workstream's latest."""
        row = self.db.one("SELECT ref FROM items WHERE source = 'slack' AND url = ?", (url,)) if url else None
        if row:
            return [row["ref"]]
        return [r["ref"] for r in self.db.all(
            "SELECT i.ref FROM items i JOIN memberships m ON m.ref = i.ref WHERE m.slug = ? AND i.source = 'slack' "
            "ORDER BY i.updated DESC LIMIT 2", (slug,))] if slug else []

    async def _slack_threads(self, refs: list[str]) -> dict[str, str]:
        """What those Slack threads say right now, read by the extension through the browser's Slack session.
        Empty when Slack isn't read that way or no browser is open; the claims then go unchecked."""
        if not refs or self.cfg.slack != "browser" or not self.bridge or not self.bridge.extension_online():
            return {}
        result = await self.bridge.request("slack.threads", {"workspace": self.cfg.slack_workspace, "refs": refs}, timeout=75)
        if not result or result.get("error"):
            log.info("slack re-read skipped: %s", (result or {}).get("error", "the extension did not answer"))
        out = {}
        for t in (result or {}).get("threads") or []:
            lines = [f"      {str(m.get('at', ''))[:16]} {m.get('user', '?')}: {str(m.get('text', ''))[:300]}"
                     for m in (t.get("messages") or [])[-12:]]
            if lines:
                out[t.get("ref", "")] = "\n".join(lines)
        return out

    async def _verify(self, surfaced: list[dict], carry: list[dict], today: datetime) -> dict:
        """Re-check open loops owed by the user and carried to-dos against their sources. Returns {claim id: verdict}."""
        claims = []  # (first recorded, claim line, Slack conversations behind it)
        for ws in surfaced:
            items = self.db.all("SELECT i.ref, i.title, i.state, i.url FROM items i JOIN memberships m ON m.ref = i.ref "
                                "WHERE m.slug = ? ORDER BY i.updated DESC LIMIT 4", (ws["slug"],))
            where = "; ".join(f"{i['ref']} '{i['title'][:50]}' ({i['state']}) {i['url']}" for i in items)
            for l in ws["loops"]:
                if self._mine(l.get("who", "")):
                    claims.append((l.get("since") or "", f"- [L-{loop_id(l.get('text', ''))}] {l.get('text', '')} · workstream "
                                   f"\"{ws['title']}\" · first recorded {l.get('since') or 'on an unknown date'} · {where}",
                                   self._slack_refs(ws["slug"])))
        for t in carry:
            url = (json.loads(t["source"] or "{}") or {}).get("url", "")
            claims.append((t["since"], f"- [{t['cid']}] {t['text']} ({t['detail']}) · a to-do carried from earlier "
                           f"briefs · first recorded {t['since']} · {url}", self._slack_refs(t["slug"], url)))
        if not claims:
            return {}
        claims = sorted(claims, key=lambda c: c[0])[:VERIFY_CLAIMS]  # oldest first: those are the likeliest to be stale
        threads = await self._slack_threads(list(dict.fromkeys(r for c in claims for r in c[2]))[:VERIFY_THREADS])
        lines = [c[1] + "".join(f"\n    Slack thread {r} right now:\n{threads[r]}" for r in c[2] if r in threads)
                 for c in claims]
        try:
            data = await self.claude.complete_json(
                prompts.fill(prompts.VERIFY_SYSTEM, me=self.me_fn()),
                prompts.fill(prompts.VERIFY, me=self.me_fn(), now=today.strftime("%A %-d %B %Y, %H:%M"),
                             claims="\n".join(lines), budget=VERIFY_CALLS),
                connectors=True, max_turns=VERIFY_CALLS + 6, fast=True, label="verify", timeout=420)
        except SleptThrough:
            raise
        except Exception as e:
            log.warning("re-check before the brief failed, writing it unchecked: %s", e)
            return {}
        out = {}
        for c in (data.get("checks") or []) if isinstance(data, dict) else []:
            if isinstance(c, dict) and c.get("status") in ("done", "expired", "open"):
                out[str(c.get("id", "")).strip("[] ")] = {"status": c["status"], "evidence": str(c.get("evidence", ""))[:240]}
        return out

    def _close_loop(self, slug: str, loop: dict, why: str, day_iso: str, todo_id: str = "") -> None:
        ws = self.db.one("SELECT open_loops, closed FROM workstreams WHERE slug = ?", (slug,))
        if not ws:
            return
        loops = [l for l in json.loads(ws["open_loops"] or "[]") if loop_id(l.get("text", "")) != loop_id(loop["text"])]
        closed = [c for c in json.loads(ws["closed"] or "[]") if not (todo_id and c.get("todo") == todo_id)]
        closed.append({"text": loop["text"], "on": day_iso, "why": why, "todo": todo_id,
                       "loop": loop if loop.get("kind") else None})
        self.db.x("UPDATE workstreams SET open_loops = ?, closed = ? WHERE slug = ?",
                  (json.dumps(loops), json.dumps(closed[-12:]), slug))

    def mark_todo(self, todo_id: str, done: bool) -> None:
        """Tick or un-tick a to-do. Ticking also closes the open loop behind it, so tomorrow's brief can't raise it again."""
        t = self.db.one("SELECT * FROM todos WHERE id = ?", (todo_id,))
        if not t:
            return
        self.db.x("UPDATE todos SET done = ?, done_at = ? WHERE id = ?", (int(done), time.time() if done else 0, todo_id))
        ws = self.db.one("SELECT open_loops, closed FROM workstreams WHERE slug = ?", (t["slug"],)) if t["slug"] else None
        if not ws:
            return
        loops, closed = json.loads(ws["open_loops"] or "[]"), json.loads(ws["closed"] or "[]")
        if done:
            hit = next((l for l in loops if loop_id(l.get("text", "")) == t["loop"]), None) or \
                next((l for l in loops if same_task(l.get("text", ""), t["text"])), None)
            self._close_loop(t["slug"], hit or {"text": t["text"]}, "ticked done", self.cfg.now().date().isoformat(), todo_id)
        else:
            for c in [c for c in closed if c.get("todo") == todo_id]:
                closed.remove(c)
                if c.get("loop"):
                    loops.append(c["loop"])
            self.db.x("UPDATE workstreams SET open_loops = ?, closed = ? WHERE slug = ?",
                      (json.dumps(loops), json.dumps(closed), t["slug"]))

    def _events_block(self) -> str:
        rows = self.db.all("SELECT e.*, i.title FROM events e LEFT JOIN items i ON i.ref = e.ref WHERE e.ts > ? "
                           "AND e.kind != 'context' ORDER BY e.ts DESC LIMIT 30", (time.time() - 86400,))
        return "\n".join(f"- [{r['kind']}] {r['author']}→{r['target'] or '-'}: {r['text'][:200]} "
                         f"({day_label(r['ts'], self.cfg.tz, clock=True)}, {r['ref']} {r['url']})" for r in rows) or "(none)"

    def _focus(self, day_iso: str) -> str:
        f = self.db.get("tomorrow_focus") or {}
        if f.get("for_day") == day_iso and f.get("label"):
            return f"{f['label']} — {f.get('detail', '')}"
        return "(nothing chosen)"

    async def generate(self, day: datetime | None = None) -> dict:
        day = day or self.cfg.now()
        day_iso = day.date().isoformat()
        tomorrow = day + timedelta(days=1)
        while tomorrow.strftime("%a").lower()[:3] not in self.cfg.brief_days and tomorrow - day < timedelta(days=7):
            tomorrow += timedelta(days=1)
        painting = await fetch_painting(self.db, day_iso)

        # Re-check what the brief is about to nag about, and settle whatever turned out finished or out of date.
        surfaced, carry = self._surfaced(day_iso), self._carryover(day_iso)
        done = self._done(day_iso, DONE_DAYS)
        carry = [t for t in carry if not any(same_task(t["text"], d["text"]) for d in done)]
        checks = await self._verify(surfaced, carry, day)
        settled = []  # found finished or expired this morning
        for ws in surfaced:
            for l in list(ws["loops"]):
                check = checks.get(f"L-{loop_id(l.get('text', ''))}")
                if check and check["status"] != "open":
                    why = f"re-checked {check['status']}: {check['evidence']}".rstrip(": ")
                    self._close_loop(ws["slug"], l, why, day_iso)
                    ws["loops"].remove(l)
                    ws["shut"].append({"text": l["text"], "on": day_iso, "why": why})
                    settled.append(f"{l['text']} ({why})")
        kept = []
        for t in carry:
            check = checks.get(t["cid"]) or {}
            age = (day.date() - datetime.fromisoformat(t["since"]).date()).days
            if check.get("status") in ("done", "expired"):
                settled.append(f"{t['text']} (re-checked {check['status']}: {check['evidence']})")
            elif age <= CARRY_DAYS or check.get("status") == "open":
                kept.append(t)
        open_checks = {k: v for k, v in checks.items() if v["status"] == "open"}
        carry_text = "\n".join(
            f"- [{t['cid']}] {t['text']} · open since {day_label(datetime.fromisoformat(t['since']).timestamp(), self.cfg.tz)}"
            + (f" · re-checked: still open — {open_checks[t['cid']]['evidence']}" if t["cid"] in open_checks
               else " · not re-checked")
            + (f" · {t['detail']}" if t["detail"] else "") for t in kept) or "(none)"
        done_text = "\n".join([f"- {d['text']} (ticked done, {d['day']})" for d in done] + [f"- {x}" for x in settled]) or "(none)"

        data = await self.claude.complete_json(
            "You write a concise, grounded morning brief. Output one JSON object only.",
            prompts.fill(prompts.BRIEF, me=self.me_fn(), weekday=day.strftime("%A"), date=day.strftime("%-d %B %Y"),
                         now=day.strftime("%H:%M"), tz=str(self.cfg.tz),
                         workstreams=self._workstream_block(surfaced, open_checks, day),
                         meetings=self._meetings_block(day), carryover=carry_text, done=done_text,
                         events=self._events_block(), focus=self._focus(day_iso), tomorrow=tomorrow.strftime("%A %-d %B"),
                         tomorrow_meetings=self._meetings_block(tomorrow)),
            label="brief", timeout=600)
        if not isinstance(data, dict):
            raise RuntimeError("brief: model returned no object")
        data["title"] = f"The {day.strftime('%A')} Brief"
        data["generated_at"] = time.time()
        self.db.x("INSERT INTO briefs(day, data, painting, created) VALUES(?,?,?,?) ON CONFLICT(day) DO UPDATE SET "
                  "data = excluded.data, painting = excluded.painting, created = excluded.created",
                  (day_iso, json.dumps(data), json.dumps(painting), time.time()))
        self.db.x("DELETE FROM todos WHERE day = ? AND done = 0", (day_iso,))
        # Last line of defence: whatever the model wrote, a to-do the user already ticked is never listed again.
        finished = [d["text"] for d in self._done(day_iso, CARRY_DAYS)] + [x.split(" (re-checked")[0] for x in settled]
        carried = {t["cid"]: t for t in kept}
        since_of = {f"L-{loop_id(l.get('text', ''))}": l.get("since") for ws in surfaced for l in ws["loops"]}
        position = 0
        for t in data.get("todos") or []:
            if not isinstance(t, dict) or not t.get("text"):
                continue
            text = str(t["text"])[:200]
            if any(same_task(text, f) for f in finished):
                log.info("brief: dropped a to-do that is already done: %s", text)
                continue
            loop = str(t.get("loop") or "").strip("[] ")
            origin = carried.get(str(t.get("carry") or "").strip("[] "))
            since = origin["since"] if origin else since_of.get(loop) or day_iso
            source = t.get("source") if isinstance(t.get("source"), dict) else {}
            if origin and not source.get("url"):  # a carried to-do keeps where it came from, so it can be re-checked
                source = json.loads(origin["source"] or "{}")
            self.db.x("INSERT INTO todos(id, day, text, detail, slug, prompt, position, source, since, loop) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (new_id(), day_iso, text, str(t.get("detail", ""))[:400],
                       str(t.get("workstream") or (origin or {}).get("slug", ""))[:80],
                       str(t.get("prompt", ""))[:400], position, json.dumps(source)[:600], since,
                       (loop.removeprefix("L-") or (origin or {}).get("loop", ""))[:12]))
            position += 1
        return data

    def set_focus(self, day_iso: str, option: dict) -> None:
        """Remember the 'what do you want to work on tomorrow?' answer for the next brief."""
        day = datetime.fromisoformat(day_iso)
        nxt = day + timedelta(days=1)
        while nxt.strftime("%a").lower()[:3] not in self.cfg.brief_days and nxt - day < timedelta(days=7):
            nxt += timedelta(days=1)
        self.db.put("tomorrow_focus", {"for_day": nxt.date().isoformat(), "label": option.get("label", ""),
                                       "detail": option.get("detail", ""), "prompt": option.get("prompt", ""),
                                       "chosen_on": day_iso})

    def get(self, day_iso: str) -> dict | None:
        row = self.db.one("SELECT * FROM briefs WHERE day = ?", (day_iso,))
        if not row:
            return None
        data = json.loads(row["data"])
        data["painting"] = json.loads(row["painting"] or "{}")
        todos = self.db.all("SELECT id, text, detail, slug, prompt, done, source FROM todos WHERE day = ? ORDER BY position",
                            (day_iso,))
        for t in todos:
            t["source"] = json.loads(t["source"] or "{}")
        data["todos"] = todos
        data["schedule"] = self.schedule(datetime.fromisoformat(day_iso).replace(tzinfo=self.cfg.tz))
        focus = self.db.get("tomorrow_focus") or {}
        data["focus_choice"] = focus if focus.get("chosen_on") == day_iso else None
        data["calendar_connected"] = not self.db.get("calendar_unavailable")
        data["day"] = day_iso
        return data
