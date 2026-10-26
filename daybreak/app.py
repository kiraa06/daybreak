"""The helper process: wires the parts together and runs the background loops."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

from . import config, osutil, prompts
from .bridge import Bridge
from .brief import Brief
from .chat import ChatManager
from .claude import Claude, scrub_host_env
from .db import DB
from .meetings import Meetings
from .pipeline import Pipeline
from .scheduled import Schedules
from .sensors import Sensors

log = logging.getLogger("daybreak")


class App:
    def __init__(self):
        scrub_host_env()
        self.cfg = config.load()
        self.db = DB()
        self.claude = Claude(self.cfg)
        self.bridge = Bridge(self.db)
        self.sensors = Sensors(self.cfg, self.db, self.claude, self.bridge)
        self.brief = Brief(self.cfg, self.db, self.claude, lambda: self.me)
        self.meetings = Meetings(self.cfg, self.db, self.claude, lambda: self.me)
        self.schedules = Schedules(self.cfg, self.db, self.claude, lambda: self.me)
        self.chat = ChatManager(self)
        self.sense_lock = asyncio.Lock()
        self.brief_lock = asyncio.Lock()
        self.meeting_lock = asyncio.Lock()
        self.activity: dict[str, str] = {}
        self.tasks: list[asyncio.Task] = []

    @property
    def me(self) -> str:
        return self.sensors.me

    def in_work_hours(self, now: datetime | None = None) -> bool:
        now = now or self.cfg.now()
        (sh, sm), (eh, em) = self.cfg.work_hours
        minutes = now.hour * 60 + now.minute
        return sh * 60 + sm <= minutes < eh * 60 + em

    # -- cycles ------------------------------------------------------------------
    async def sense_cycle(self, reason: str = "scheduled", hours: int | None = None) -> dict:
        if self.sense_lock.locked():
            async with self.sense_lock:  # someone else is sensing; wait for them and reuse the result
                return self.db.get("last_sense_result", {})
        async with self.sense_lock:
            run = self.db.run_start(f"sense:{reason}")
            self.activity["sense"] = "Reading your sources"
            started = time.time()
            try:
                last = self.db.get("last_sense", 0) or 0
                # The first run looks back four days so workstreams have something to grow from.
                hours = hours or (96 if not last else max(2, min(24, int((started - last) / 3600) + 2)))
                sources = await self.sensors.run_all(hours=hours)
                if time.time() - (self.db.get("last_calendar", 0) or 0) > 20 * 60:
                    await self.refresh_calendar()
                self.activity["sense"] = "Updating workstreams"
                pipe = await Pipeline(self.cfg, self.db, self.claude, self.me).run()
                await self.proactive(started)
                result = {"sources": sources, **pipe, "seconds": round(time.time() - started)}
                self.db.put("last_sense", started)
                self.db.put("last_sense_result", result)
                self.db.run_end(run, "ok", json.dumps(result))
                log.info("sense cycle (%s): %s", reason, result)
                return result
            except Exception as e:
                log.exception("sense cycle failed")
                self.db.run_end(run, "failed", str(e))
                return {"error": str(e)}
            finally:
                self.activity.pop("sense", None)

    async def refresh_calendar(self) -> None:
        n = await self.sensors.calendar()
        self.db.put("last_calendar", time.time())
        status = self.db.get("source_status", {}) or {}
        status["calendar"] = {"ok": n >= 0, "added": max(n, 0), "at": time.time(),
                              "unavailable": bool(self.db.get("calendar_unavailable"))}
        self.db.put("source_status", status)

    async def make_brief(self, reason: str = "scheduled") -> dict | None:
        if self.brief_lock.locked():
            return None
        async with self.brief_lock:
            run = self.db.run_start(f"brief:{reason}")
            try:
                if time.time() - (self.db.get("last_sense", 0) or 0) > 20 * 60:
                    await self.sense_cycle("brief")
                elif time.time() - (self.db.get("last_calendar", 0) or 0) > 20 * 60:
                    await self.refresh_calendar()
                self.activity["brief"] = "Writing your brief"
                now = self.cfg.now()
                data = await self.brief.generate(now)
                bh, bm = self.cfg.brief_time
                ready_at = max(time.time(), now.replace(hour=bh, minute=bm, second=0).timestamp())
                self.db.add_nudge("brief", data["title"], data.get("caption", "Your brief is ready."),
                                  {"open": "newtab"}, deliver_at=ready_at, ttl=4 * 3600, dedupe=f"brief:{now.date()}")
                self.db.run_end(run, "ok")
                return data
            except Exception as e:
                log.exception("brief failed")
                self.db.run_end(run, "failed", str(e))
                self.db.put("brief_error", {"at": time.time(), "error": str(e)[:500]})
                return None
            finally:
                self.activity.pop("brief", None)

    async def proactive(self, since: float) -> None:
        """Turn the few events that really need the user into pop-ups (rate-limited, work hours only)."""
        if not self.in_work_hours():
            return
        sent_last_hour = self.db.one("SELECT COUNT(*) AS n FROM nudges WHERE kind = 'followup' AND deliver_at > ?",
                                     (time.time() - 3600,))["n"]
        cap = self.cfg.max_proactive_nudges_per_hour - sent_last_hour
        if cap <= 0:
            return
        rows = self.db.all("SELECT e.*, i.title AS item_title FROM events e LEFT JOIN items i ON i.ref = e.ref "
                           "WHERE e.created >= ? AND e.kind IN ('request','blocker','decision') AND e.source != 'chat' "
                           "ORDER BY e.ts DESC LIMIT 40", (since,))
        if not rows:
            return
        lines = "\n".join(f"- {r['id']} · [{r['kind']}] {r['author']}→{r['target'] or '-'}: {r['text'][:200]} · "
                          f"{(r['item_title'] or r['ref'])[:60]}" for r in rows)
        try:
            out = await self.claude.complete_json(
                "You decide which work events deserve an interrupting notification. Output JSON only.",
                prompts.fill(prompts.PROACTIVE_FILTER, me=self.me, cap=min(cap, 2), events=lines),
                fast=True, label="proactive")
        except Exception as e:
            log.warning("proactive filter failed: %s", e)
            return
        by_id = {r["id"]: r for r in rows}
        for n in (out.get("nudges") or [])[:cap] if isinstance(out, dict) else []:
            ev = by_id.get(n.get("event_id"))
            if not ev:
                continue
            self.db.add_nudge("followup", str(n.get("title", "Needs you"))[:90], str(n.get("body", ev["text"]))[:240],
                              {"url": ev["url"], "prompt": n.get("prompt", ""), "ref": ev["ref"]},
                              ttl=3 * 3600, dedupe=f"followup:{ev['ref']}:{ev['kind']}")

    # -- loops -------------------------------------------------------------------
    async def _loop(self, name: str, every: float, fn) -> None:
        await asyncio.sleep(2)
        while True:
            try:
                await fn()
            except Exception:
                log.exception("loop %s failed", name)
            await asyncio.sleep(every)

    async def _sense_tick(self) -> None:
        if not self.in_work_hours() or self.brief_lock.locked():
            return
        if time.time() - (self.db.get("last_sense", 0) or 0) >= self.cfg.sense_every_minutes * 60:
            await self.sense_cycle()

    async def _brief_tick(self) -> None:
        now = self.cfg.now()
        if now.strftime("%a").lower()[:3] not in self.cfg.brief_days:
            return
        bh, bm = self.cfg.brief_time
        start_at = now.replace(hour=bh, minute=bm, second=0, microsecond=0).timestamp() - 25 * 60
        if time.time() < start_at or self.brief.get(now.date().isoformat()):
            return
        err = self.db.get("brief_error") or {}
        if time.time() - err.get("at", 0) < 15 * 60:
            return  # back off after a failure
        await self.make_brief()

    async def _meeting_tick(self) -> None:
        if self.meeting_lock.locked():
            return
        async with self.meeting_lock:
            now = self.cfg.now()
            if 6 <= now.hour < 22 and time.time() - (self.db.get("last_calendar", 0) or 0) > 30 * 60:
                await self.refresh_calendar()
            await self.meetings.tick()

    async def _os_notify_tick(self) -> None:
        """If no browser has the extension running, fall back to a desktop notification so pop-ups aren't lost."""
        if self.bridge.extension_online():
            return
        now = time.time()
        for n in self.db.all("SELECT * FROM nudges WHERE deliver_at <= ? AND expires_at > ? AND delivered_at IS NULL "
                             "AND dismissed_at IS NULL AND os_notified = 0", (now, now)):
            body = n["body"] if n["kind"] != "meeting" else f"Meeting {n['body']}"
            await asyncio.to_thread(osutil.notify, "Daybreak", n["title"], body)
            self.db.x("UPDATE nudges SET os_notified = 1 WHERE id = ?", (n["id"],))

    def start(self) -> None:
        self.tasks = [
            asyncio.create_task(self._loop("sense", 60, self._sense_tick)),
            asyncio.create_task(self._loop("brief", 60, self._brief_tick)),
            asyncio.create_task(self._loop("meetings", 60, self._meeting_tick)),
            asyncio.create_task(self._loop("schedules", 30, self.schedules.tick)),
            asyncio.create_task(self._loop("os-notify", 30, self._os_notify_tick)),
            asyncio.create_task(self._loop("chat-reap", 300, self.chat.reap)),
        ]

    def state(self) -> dict:
        day = self.cfg.now().date().isoformat()
        nxt = self.meetings.upcoming(hours=12)
        return {
            "me": self.me, "day": day, "brief_ready": bool(self.brief.get(day)),
            "activity": list(self.activity.values()), "last_sense": self.db.get("last_sense", 0),
            "sources": self.db.get("source_status", {}), "extension_online": self.bridge.extension_online(),
            "next_meeting": ({k: nxt[0][k] for k in ("id", "title", "time", "start", "join_url", "prep_url")} if nxt else None),
            "workstreams": self.db.one("SELECT COUNT(*) AS n FROM workstreams WHERE state IN ('active','waiting')")["n"],
            "slack_mode": self.cfg.slack, "slack_workspace": self.cfg.slack_workspace,
            "brief_time": datetime.now().replace(hour=self.cfg.brief_time[0], minute=self.cfg.brief_time[1]).strftime("%-I:%M %p"),
            "brief_error": self.db.get("brief_error"), "claude": self.claude.stats,
        }
