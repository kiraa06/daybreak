"""Sensors: pull recent activity from each source and store it as typed events.

Connector sources (Gmail, Atlassian, Drive, Calendar, Slack connector) run one bounded Claude call each.
Slack in "browser" mode is fetched by the extension through the user's browser session (see bridge.py) and only the
extraction step runs here. Bitbucket and ICS calendars are fetched directly.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta

import aiohttp

from . import prompts
from .bridge import Bridge
from .claude import Claude
from .config import Config
from .db import DB, new_id

log = logging.getLogger("daybreak.sensors")

KINDS = {"request", "commitment", "decision", "blocker", "context"}


def to_epoch(value, default: float | None = None) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        v = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(v).timestamp()
        except ValueError:
            pass
    return default if default is not None else time.time()


class Sensors:
    def __init__(self, cfg: Config, db: DB, claude: Claude, bridge: Bridge):
        self.cfg, self.db, self.claude, self.bridge = cfg, db, claude, bridge

    # -- identity ------------------------------------------------------------
    @property
    def me(self) -> str:
        return self.cfg.name or (self.db.get("me") or {}).get("name") or "the user"

    async def learn_identity(self) -> None:
        if self.cfg.name or self.db.get("me"):
            return
        try:
            data = await self.claude.complete_json(
                prompts.SENSOR_SYSTEM.replace("{{me}}", "the user"),
                "Find out who the signed-in user is with ONE call: atlassianUserInfo (Atlassian) or, if unavailable, the "
                'Gmail profile. Return only {"name": "<full name>", "email": "<email>"}.',
                connectors=True, max_turns=5, label="whoami")
            if isinstance(data, dict) and data.get("name"):
                self.db.put("me", {"name": data["name"], "email": data.get("email", "")})
        except Exception as e:
            log.warning("could not learn identity: %s", e)

    # -- ledger --------------------------------------------------------------
    def captured(self, prefixes: tuple[str, ...]) -> str:
        rows = self.db.all("SELECT ref, state, title FROM items WHERE updated > ? ORDER BY updated DESC LIMIT 80",
                           (time.time() - 3 * 86400,))
        lines = [f"- {r['ref']} → {r['state'] or 'seen'} ({r['title'][:60]})" for r in rows if r["ref"].startswith(prefixes)]
        return "\n".join(lines) or "(nothing yet)"

    def ingest(self, source: str, events: list[dict]) -> int:
        added = 0
        now = time.time()
        for ev in events or []:
            if not isinstance(ev, dict):
                continue
            kind = str(ev.get("kind", "")).lower()
            ref = str(ev.get("ref", "")).strip()
            summary = str(ev.get("summary", "")).strip()
            if kind not in KINDS or not ref or not summary:
                continue
            fp = hashlib.sha1(f"{ref}|{kind}|{summary.lower()}".encode()).hexdigest()
            try:
                self.db.x(
                    "INSERT INTO events(id, source, ref, kind, author, target, text, url, ts, created, fingerprint) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id(), source, ref, kind, str(ev.get("author", ""))[:80], str(ev.get("target", ""))[:80],
                     summary[:600], str(ev.get("url", ""))[:500], to_epoch(ev.get("ts")), now, fp))
                added += 1
            except Exception:
                continue  # duplicate fingerprint
            self.db.x(
                "INSERT INTO items(ref, source, title, state, url, summary, updated) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(ref) DO UPDATE SET title = COALESCE(NULLIF(excluded.title, ''), items.title), "
                "state = COALESCE(NULLIF(excluded.state, ''), items.state), "
                "url = COALESCE(NULLIF(excluded.url, ''), items.url), summary = excluded.summary, updated = excluded.updated",
                (ref, source, str(ev.get("title", ""))[:200], str(ev.get("state", ""))[:80], str(ev.get("url", ""))[:500],
                 summary[:600], now))
        return added

    # -- connector sensors -----------------------------------------------------
    async def _connector_sensor(self, source: str, template: str, prefixes: tuple[str, ...], hours: int) -> int:
        prompt = prompts.fill(template, me=self.me, hours=hours, now=self.cfg.now().isoformat(timespec="minutes"),
                              captured=self.captured(prefixes))
        try:
            data = await self.claude.complete_json(prompts.fill(prompts.SENSOR_SYSTEM, me=self.me), prompt,
                                                   connectors=True, max_turns=12, fast=True, label=f"sense:{source}")
        except Exception as e:
            log.warning("sensor %s failed: %s", source, e)
            return -1
        events = data.get("events", []) if isinstance(data, dict) else []
        return self.ingest(source, events)

    async def gmail(self, hours: int) -> int:
        return await self._connector_sensor("gmail", prompts.GMAIL_SENSOR, ("gmail:",), hours)

    async def atlassian(self, hours: int) -> int:
        return await self._connector_sensor("atlassian", prompts.ATLASSIAN_SENSOR, ("jira:", "confluence:"), hours)

    async def drive(self, hours: int) -> int:
        return await self._connector_sensor("drive", prompts.DRIVE_SENSOR, ("gdrive:",), hours)

    async def slack(self, hours: int) -> int:
        if self.cfg.slack == "connector":
            return await self._connector_sensor("slack", prompts.SLACK_CONNECTOR_SENSOR, ("slack:",), hours)
        if self.cfg.slack != "browser":
            return 0
        if not self.bridge.extension_online():
            log.info("slack: extension offline; skipping")
            return -1
        now = time.time()
        cursors = self.db.get("slack_cursors", {}) or {}
        params = {"workspace": self.cfg.slack_workspace, "oldest": now - hours * 3600,
                  "oldest_new": now - max(hours, 24) * 3600,  # a workspace seen for the first time looks back a day
                  "oldest_by_workspace": {url: max(ts, now - hours * 3600) for url, ts in cursors.items()}}
        result = await self.bridge.request("slack.fetch", params, timeout=150)
        if not result or result.get("error"):
            log.warning("slack bridge: %s", (result or {}).get("error", "timed out"))
            if result and result.get("workspaces"):
                self.db.put("slack_workspaces", result["workspaces"])
            return -1
        convos = result.get("conversations") or []
        self.db.put("slack_workspaces", result.get("workspaces") or [])
        for w in result.get("workspaces") or []:
            if w.get("ok"):
                cursors[w["url"]] = now - 600
        self.db.put("slack_cursors", cursors)
        log.info("slack: %s", ", ".join(f"{w['name']}: {w.get('conversations', 0)} convos {w.get('diag')}"
                                        for w in result.get("workspaces") or []))
        if not convos:
            return 0
        payload = json.dumps(convos, ensure_ascii=False)[:60000]
        try:
            data = await self.claude.complete_json(
                "You turn Slack conversations into typed work events. Output JSON only.",
                prompts.fill(prompts.SLACK_EXTRACT, me=self.me, now=self.cfg.now().isoformat(timespec="minutes"),
                             slack_me=result.get("me", ""), conversations=payload),
                fast=True, label="sense:slack")
        except Exception as e:
            log.warning("slack extraction failed: %s", e)
            return -1
        return self.ingest("slack", data.get("events", []) if isinstance(data, dict) else [])

    async def bitbucket(self, hours: int) -> int:
        ws, email = self.cfg.bitbucket_workspace, self.cfg.bitbucket_email
        if not ws or not email:
            return 0
        token = os.environ.get("BITBUCKET_API_TOKEN") or _keychain("daybreak-bitbucket")
        if not token:
            log.info("bitbucket: no token (env BITBUCKET_API_TOKEN or keychain 'daybreak-bitbucket')")
            return -1
        auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        cutoff = time.time() - hours * 3600
        events = []
        async with aiohttp.ClientSession(headers={"Authorization": auth}, timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get("https://api.bitbucket.org/2.0/user") as r:
                if r.status != 200:
                    log.warning("bitbucket auth failed: %s", r.status)
                    return -1
                me = await r.json()
            url = f"https://api.bitbucket.org/2.0/workspaces/{ws}/pullrequests/{me['uuid']}?state=OPEN&state=MERGED&state=DECLINED&pagelen=30"
            async with s.get(url) as r:
                prs = (await r.json()).get("values", []) if r.status == 200 else []
        for pr in prs:
            updated = to_epoch(pr.get("updated_on"))
            if updated < cutoff:
                continue
            repo = pr.get("destination", {}).get("repository", {}).get("full_name", "")
            state = pr.get("state", "")
            kind = "decision" if state in ("MERGED", "DECLINED") else "context"
            events.append({"ref": f"bitbucket:{repo}#{pr['id']}", "title": pr.get("title", ""), "state": state.lower(),
                           "url": pr.get("links", {}).get("html", {}).get("href", ""), "kind": kind, "author": "me",
                           "target": "", "summary": f"PR #{pr['id']} in {repo} is {state.lower()}: {pr.get('title', '')}",
                           "ts": pr.get("updated_on")})
        return self.ingest("bitbucket", events)

    # -- calendar --------------------------------------------------------------
    async def calendar(self) -> int:
        now = self.cfg.now()
        start, end = now - timedelta(hours=2), now + timedelta(hours=40)
        if self.cfg.calendar_ics_url:
            meetings = await self._ics_meetings(start, end)
        elif self.cfg.calendar:
            try:
                data = await self.claude.complete_json(
                    prompts.fill(prompts.SENSOR_SYSTEM, me=self.me),
                    prompts.fill(prompts.CALENDAR_SENSOR, me=self.me, start=start.isoformat(timespec="minutes"),
                                 end=end.isoformat(timespec="minutes"), tz=str(self.cfg.tz),
                                 now=now.isoformat(timespec="minutes")),
                    connectors=True, max_turns=8, fast=True, label="sense:calendar")
            except Exception as e:
                log.warning("calendar sensor failed: %s", e)
                return -1
            if isinstance(data, dict) and data.get("unavailable"):
                self.db.put("calendar_unavailable", True)
                return -1
            meetings = data.get("meetings", []) if isinstance(data, dict) else []
        else:
            return 0
        self.db.put("calendar_unavailable", False)
        seen = set()
        for m in meetings:
            mid = str(m.get("id") or hashlib.sha1(f"{m.get('title')}{m.get('start')}".encode()).hexdigest()[:16])
            s, e = to_epoch(m.get("start"), 0), to_epoch(m.get("end"), 0)
            if not s:
                continue
            if not e or e <= s or e - s > 12 * 3600:  # e.g. a recurring series' last date read as the end time
                e = s + 3600
            seen.add(mid)
            self.db.x(
                "INSERT INTO meetings(id, title, start, end, attendees, organizer, location, join_url, description, updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title = excluded.title, start = excluded.start, "
                "end = excluded.end, attendees = excluded.attendees, organizer = excluded.organizer, "
                "location = excluded.location, join_url = excluded.join_url, description = excluded.description, "
                "updated = excluded.updated",
                (mid, str(m.get("title", "Untitled"))[:200], s, e or s + 1800, json.dumps(m.get("attendees") or [])[:4000],
                 str(m.get("organizer", ""))[:120], str(m.get("location", ""))[:200], str(m.get("join_url", ""))[:500],
                 str(m.get("description", ""))[:1500], time.time()))
        # Meetings that disappeared from the window were cancelled or declined; anything older than a day is history.
        for row in self.db.all("SELECT id FROM meetings WHERE start BETWEEN ? AND ?", (start.timestamp(), end.timestamp())):
            if row["id"] not in seen:
                self.db.x("DELETE FROM meetings WHERE id = ?", (row["id"],))
        self.db.x("DELETE FROM meetings WHERE start < ? AND prep_artifact = ''", (time.time() - 86400,))
        self.db.x("UPDATE meetings SET end = start + 3600 WHERE end - start > 43200 OR end <= start")
        return len(seen)

    async def _ics_meetings(self, start: datetime, end: datetime) -> list[dict]:
        import icalendar
        import recurring_ical_events
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(self.cfg.calendar_ics_url) as r:
                raw = await r.read()
        cal = icalendar.Calendar.from_ical(raw)
        out = []
        for ev in recurring_ical_events.of(cal).between(start, end):
            dtstart, dtend = ev.get("DTSTART").dt, ev.get("DTEND").dt if ev.get("DTEND") else None
            if not isinstance(dtstart, datetime):
                continue  # all-day
            desc = str(ev.get("DESCRIPTION", ""))
            join = next((w for w in desc.split() if w.startswith(("https://meet.google", "https://zoom", "https://teams"))), "")
            attendees = [str(a).replace("mailto:", "") for a in (ev.get("ATTENDEE") or [])] if isinstance(ev.get("ATTENDEE"), list) else []
            out.append({"id": f"{ev.get('UID')}-{dtstart.isoformat()}", "title": str(ev.get("SUMMARY", "Untitled")),
                        "start": dtstart.isoformat(), "end": (dtend or dtstart).isoformat(), "attendees": attendees,
                        "organizer": str(ev.get("ORGANIZER", "")).replace("mailto:", ""),
                        "location": str(ev.get("LOCATION", "")), "join_url": join or str(ev.get("LOCATION", "")),
                        "description": desc[:400]})
        return out

    # -- all -------------------------------------------------------------------
    async def run_all(self, hours: int = 12) -> dict[str, int]:
        await self.learn_identity()
        jobs = {}
        if self.cfg.gmail:
            jobs["gmail"] = self.gmail(hours)
        if self.cfg.atlassian:
            jobs["atlassian"] = self.atlassian(hours)
        if self.cfg.drive:
            jobs["drive"] = self.drive(hours)
        if self.cfg.slack != "off":
            jobs["slack"] = self.slack(hours)
        if self.cfg.bitbucket_workspace:
            jobs["bitbucket"] = self.bitbucket(hours)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        out = {k: (r if isinstance(r, int) else -1) for k, r in zip(jobs, results)}
        for k, r in zip(jobs, results):
            if isinstance(r, Exception):
                log.warning("sensor %s crashed: %s", k, r)
        status = self.db.get("source_status", {}) or {}
        for k, v in out.items():
            status[k] = {"ok": v >= 0, "added": max(v, 0), "at": time.time()}
        self.db.put("source_status", status)
        return out


def _keychain(service: str) -> str:
    try:
        return subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:
        return ""
