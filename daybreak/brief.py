"""The Morning Brief: today's painting plus a grounded summary of where the user's work stands."""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timedelta

import aiohttp

from . import config, prompts
from .claude import Claude
from .db import DB, new_id

log = logging.getLogger("daybreak.brief")

AIC = "https://api.artic.edu/api/v1/artworks/search"
AIC_HEADERS = {"AIC-User-Agent": "Daybreak personal morning brief (local app)"}
THEMES = ["landscape", "village", "harbor", "garden", "river", "autumn", "summer", "snow", "seashore", "meadow",
          "mountains", "boats", "farm", "orchard", "city street", "sunrise", "forest", "coast", "picnic", "fields"]


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
    def __init__(self, cfg: config.Config, db: DB, claude: Claude, me_fn):
        self.cfg, self.db, self.claude, self.me_fn = cfg, db, claude, me_fn

    def _day_bounds(self, day: datetime) -> tuple[float, float]:
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()

    def _workstream_block(self, day_iso: str) -> str:
        row = self.db.one("SELECT slugs FROM surface WHERE day = ?", (day_iso,)) or \
            self.db.one("SELECT slugs FROM surface ORDER BY day DESC LIMIT 1")
        slugs = json.loads(row["slugs"]) if row else []
        if not slugs:
            slugs = [r["slug"] for r in self.db.all(
                "SELECT slug FROM workstreams WHERE state IN ('active','waiting') AND overview != '' ORDER BY updated DESC LIMIT 6")]
        blocks = []
        for slug in slugs:
            ws = self.db.one("SELECT * FROM workstreams WHERE slug = ?", (slug,))
            if not ws:
                continue
            logs = self.db.all("SELECT text FROM workstream_log WHERE slug = ? ORDER BY ts DESC LIMIT 3", (slug,))
            loops = json.loads(ws["open_loops"] or "[]")
            items = self.db.all("SELECT i.ref, i.title, i.state, i.url FROM items i JOIN memberships m ON m.ref = i.ref "
                                "WHERE m.slug = ? ORDER BY i.updated DESC LIMIT 5", (slug,))
            blocks.append(f"## {slug} — {ws['title']} · {ws['state']}\n{ws['overview']}\n"
                          f"Recent: {' | '.join(l['text'] for l in logs) or '-'}\n"
                          f"Open loops: {json.dumps(loops)[:900]}\n"
                          "Items: " + "; ".join(f"{i['ref']} '{i['title'][:60]}' ({i['state']}) {i['url']}" for i in items))
        return "\n\n".join(blocks) or "(no workstreams yet — sources are still being read)"

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

    def _carryover(self, day_iso: str) -> tuple[str, list[dict]]:
        prev = self.db.one("SELECT day FROM briefs WHERE day < ? ORDER BY day DESC LIMIT 1", (day_iso,))
        if not prev:
            return "(none)", []
        rows = self.db.all("SELECT * FROM todos WHERE day = ? AND done = 0", (prev["day"],))
        return ("\n".join(f"- {r['text']} ({r['detail']})" for r in rows) or "(all done)"), rows

    def _events_block(self) -> str:
        rows = self.db.all("SELECT e.*, i.title FROM events e LEFT JOIN items i ON i.ref = e.ref WHERE e.ts > ? "
                           "AND e.kind != 'context' ORDER BY e.ts DESC LIMIT 30", (time.time() - 86400,))
        return "\n".join(f"- [{r['kind']}] {r['author']}→{r['target'] or '-'}: {r['text'][:200]} ({r['ref']} {r['url']})"
                         for r in rows) or "(none)"

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
        carry_text, _ = self._carryover(day_iso)
        data = await self.claude.complete_json(
            "You write a concise, grounded morning brief. Output one JSON object only.",
            prompts.fill(prompts.BRIEF, me=self.me_fn(), weekday=day.strftime("%A"), date=day.strftime("%-d %B %Y"),
                         now=day.strftime("%H:%M"), tz=str(self.cfg.tz), workstreams=self._workstream_block(day_iso),
                         meetings=self._meetings_block(day), carryover=carry_text, events=self._events_block(),
                         focus=self._focus(day_iso), tomorrow=tomorrow.strftime("%A %-d %B"),
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
        for i, t in enumerate(data.get("todos") or []):
            if isinstance(t, dict) and t.get("text"):
                self.db.x("INSERT INTO todos(id, day, text, detail, slug, prompt, position, source) VALUES(?,?,?,?,?,?,?,?)",
                          (new_id(), day_iso, str(t["text"])[:200], str(t.get("detail", ""))[:400],
                           str(t.get("workstream", ""))[:80], str(t.get("prompt", ""))[:400], i,
                           json.dumps(t.get("source") or {})[:600]))
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
