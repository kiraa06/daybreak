"""Scheduled prompts: recurring tasks the user asked for ("every weekday at 5:30, recap my PR reviews")."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

from . import artifacts, prompts
from .claude import Claude
from .config import Config
from .db import DB, new_id

log = logging.getLogger("daybreak.scheduled")

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def next_run(spec: str, now: datetime) -> datetime:
    """Supported specs: 'daily 09:00', 'weekdays 17:30', 'mon,thu 10:00', 'every 2h', 'every 30m', 'once 2026-11-02 09:00'."""
    s = spec.strip().lower()
    m = re.fullmatch(r"every\s+(\d+)\s*(m|min|minutes|h|hours?)", s)
    if m:
        n = int(m.group(1))
        minutes = n * 60 if m.group(2).startswith("h") else max(n, 15)
        return now + timedelta(minutes=minutes)
    m = re.fullmatch(r"once\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})", s)
    if m:
        d = datetime.fromisoformat(m.group(1)).replace(hour=int(m.group(2)), minute=int(m.group(3)), tzinfo=now.tzinfo)
        return d if d > now else datetime.max.replace(tzinfo=now.tzinfo)
    m = re.fullmatch(r"(daily|weekdays|weekends|[a-z,\s]+)\s+(\d{1,2}):(\d{2})", s)
    if not m:
        raise ValueError(f"unrecognised schedule {spec!r}")
    which, hh, mm = m.group(1).strip(), int(m.group(2)), int(m.group(3))
    if which == "daily":
        days = set(range(7))
    elif which == "weekdays":
        days = set(range(5))
    elif which == "weekends":
        days = {5, 6}
    else:
        days = {DAYS.index(d.strip()[:3]) for d in which.split(",") if d.strip()[:3] in DAYS}
        if not days:
            raise ValueError(f"unrecognised days in {spec!r}")
    for add in range(0, 8):
        cand = (now + timedelta(days=add)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand > now and cand.weekday() in days:
            return cand
    raise ValueError(f"could not schedule {spec!r}")


class Schedules:
    def __init__(self, cfg: Config, db: DB, claude: Claude, me_fn):
        self.cfg, self.db, self.claude, self.me_fn = cfg, db, claude, me_fn

    def create(self, name: str, prompt: str, spec: str) -> dict:
        nr = next_run(spec, self.cfg.now())
        sid = new_id()
        self.db.x("INSERT INTO schedules(id, name, prompt, spec, next_run, created) VALUES(?,?,?,?,?,?)",
                  (sid, name[:120], prompt[:4000], spec, nr.timestamp(), time.time()))
        return {"id": sid, "name": name, "spec": spec, "next_run": nr.isoformat(timespec="minutes")}

    def list(self) -> list[dict]:
        rows = self.db.all("SELECT * FROM schedules ORDER BY created")
        for r in rows:
            r["next_run_text"] = datetime.fromtimestamp(r["next_run"], self.cfg.tz).strftime("%a %-d %b %-I:%M %p") \
                if r["next_run"] < 4e9 else "never"
        return rows

    def delete(self, sid: str) -> bool:
        return self.db.x("DELETE FROM schedules WHERE id = ?", (sid,)).rowcount > 0

    async def run(self, row: dict) -> None:
        now = self.cfg.now()
        try:
            fragment = await self.claude.complete(
                prompts.fill(prompts.SENSOR_SYSTEM, me=self.me_fn()),
                prompts.fill(prompts.SCHEDULED, me=self.me_fn(), now=now.isoformat(timespec="minutes"), task=row["prompt"]),
                connectors=True, max_turns=16, label=f"scheduled:{row['name'][:30]}")
            fragment = artifacts.sanitize_fragment(fragment.strip().removeprefix("```html").removesuffix("```"))
            aid = artifacts.save(self.db, "scheduled", f"{row['name']} — {now.strftime('%-d %b')}",
                                 artifacts.wrap_page(row["name"], f"Scheduled · {now.strftime('%A %-d %B, %-I:%M %p')}", fragment),
                                 {"schedule_id": row["id"]})
            self.db.add_nudge("artifact", row["name"], "Your scheduled task is ready.",
                              {"url": artifacts.url_for(self.cfg, aid)}, ttl=6 * 3600, dedupe=f"sched:{row['id']}:{int(time.time())}")
            status = "ok"
        except Exception as e:
            log.warning("scheduled %s failed: %s", row["name"], e)
            status = f"failed: {e}"[:300]
        try:
            nr = next_run(row["spec"], self.cfg.now()).timestamp()
        except ValueError:
            nr = 4e9
        self.db.x("UPDATE schedules SET last_run = ?, last_status = ?, next_run = ? WHERE id = ?",
                  (time.time(), status, min(nr, 4e9), row["id"]))

    async def tick(self) -> None:
        for row in self.db.all("SELECT * FROM schedules WHERE enabled = 1 AND next_run <= ?", (time.time(),)):
            # Push next_run forward first so a slow run is not picked up twice.
            self.db.x("UPDATE schedules SET next_run = ? WHERE id = ?", (time.time() + 3600, row["id"]))
            await self.run(row)
