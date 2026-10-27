"""Meeting prep notes and the before-meeting pop-ups."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

from . import artifacts, prompts
from .claude import Claude
from .config import Config
from .db import DB

log = logging.getLogger("daybreak.meetings")

STOP = {"the", "a", "an", "and", "or", "for", "with", "of", "to", "in", "on", "sync", "meeting", "call", "weekly",
        "daily", "1:1", "review", "team", "chat", "catch", "up", "x", "&"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", text.lower()) if w not in STOP}


class Meetings:
    def __init__(self, cfg: Config, db: DB, claude: Claude, me_fn):
        self.cfg, self.db, self.claude, self.me_fn = cfg, db, claude, me_fn

    def fmt_time(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.cfg.tz).strftime("%-I:%M %p")

    def upcoming(self, hours: float = 48) -> list[dict]:
        now = time.time()
        rows = self.db.all("SELECT * FROM meetings WHERE end > ? AND start > ? AND start < ? ORDER BY start",
                           (now, now - 12 * 3600, now + hours * 3600))
        for r in rows:
            r["attendees"] = json.loads(r["attendees"] or "[]")
            r["prep_url"] = artifacts.url_for(self.cfg, r["prep_artifact"]) if r["prep_artifact"] else ""
            r["time"] = self.fmt_time(r["start"])
        return rows

    def _related_context(self, meeting: dict) -> str:
        words = _tokens(meeting["title"] + " " + meeting.get("description", "")) | \
            {a.split("@")[0].split(".")[0].lower() for a in json.loads(meeting["attendees"] or "[]")}
        scored = []
        for ws in self.db.all("SELECT * FROM workstreams WHERE state IN ('active','waiting')"):
            overlap = len(words & _tokens(f"{ws['slug'].replace('-', ' ')} {ws['title']} {ws['overview']}"))
            if overlap:
                scored.append((overlap, ws))
        scored.sort(key=lambda x: -x[0])
        return "\n\n".join(f"## {w['title']} ({w['state']})\n{w['overview']}\nOpen loops: {w['open_loops']}"
                           for _, w in scored[:3]) or "(nothing related on record)"

    async def prepare(self, meeting_id: str) -> str | None:
        m = self.db.one("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
        if not m:
            return None
        self.db.x("UPDATE meetings SET prep_state = 'running' WHERE id = ?", (meeting_id,))
        when = f"{datetime.fromtimestamp(m['start'], self.cfg.tz).strftime('%A %-d %b, %-I:%M %p')} – {self.fmt_time(m['end'])}"
        meeting_text = (f"Title: {m['title']}\nWhen: {when}\nOrganizer: {m['organizer']}\n"
                        f"Attendees: {', '.join(json.loads(m['attendees'] or '[]'))}\nLocation: {m['location']}\n"
                        f"Description: {m['description'] or '(none)'}")
        try:
            prep = await self.claude.complete_json(
                prompts.fill(prompts.SENSOR_SYSTEM, me=self.me_fn()),
                prompts.fill(prompts.MEETING_PREP, me=self.me_fn(), now=self.cfg.now().isoformat(timespec="minutes"),
                             meeting=meeting_text, context=self._related_context(m)),
                connectors=True, max_turns=14, label=f"prep:{m['title'][:30]}")
        except Exception as e:
            log.warning("prep failed for %s: %s", m["title"], e)
            self.db.x("UPDATE meetings SET prep_state = 'failed' WHERE id = ?", (meeting_id,))
            return None
        aid = artifacts.save(self.db, "meetingPrep", m["title"], artifacts.render_prep(dict(m), prep, when),
                             {"meeting_id": meeting_id})
        self.db.x("UPDATE meetings SET prep_artifact = ?, prep_state = 'done' WHERE id = ?", (aid, meeting_id))
        return aid

    def _has_people(self, m: dict) -> bool:
        return bool(m["join_url"]) or len(json.loads(m["attendees"] or "[]")) > 1

    async def tick(self) -> None:
        """Called every minute: build prep notes that are due and queue the pop-ups."""
        now = time.time()
        lead = self.cfg.meeting_prep_lead_minutes * 60
        for m in self.db.all("SELECT * FROM meetings WHERE start > ? AND start < ? ORDER BY start", (now - 600, now + lead)):
            if not self._has_people(m):
                continue
            if not m["prep_state"]:
                await self.prepare(m["id"])
                m = self.db.one("SELECT * FROM meetings WHERE id = ?", (m["id"],))
            self._queue_nudges(m)

    def _queue_nudges(self, m: dict) -> None:
        attendees = json.loads(m["attendees"] or "[]")
        payload = {"meeting_id": m["id"], "title": m["title"], "start": m["start"], "join_url": m["join_url"],
                   "attendees": attendees[:8], "attendee_count": len(attendees),
                   "prep_url": artifacts.url_for(self.cfg, m["prep_artifact"]) if m["prep_artifact"] else ""}
        for lead in self.cfg.meeting_nudges:
            at = m["start"] - lead * 60
            if at < time.time() - 120:
                continue
            label = "now" if lead == 0 else f"in {lead} min"
            self.db.add_nudge("meeting", m["title"], label, payload, deliver_at=at,
                              ttl=(lead * 60 + 240) if lead else 600, dedupe=f"meeting:{m['id']}:{int(m['start'])}:{lead}")
