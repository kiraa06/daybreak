"""Workstream pipeline: route new events to workstreams, re-synthesize what changed, curate today's surface."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime

from . import prompts
from .claude import Claude
from .config import Config
from .db import DB

log = logging.getLogger("daybreak.pipeline")

ROUTE_FLOOR = 0.75
SURFACE_CAP = 6
SYNTH_PER_CYCLE = 10
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DORMANT_DAYS = 7   # nothing real for this long: the workstream drops out of routing
QUIET_DAYS = 5     # nothing real for this long: not worth putting in front of the user (covers a long weekend)
CLOSED_KEEP_DAYS = 21
# A shared document merely being edited says nothing about the work. It can sit in a workstream, but it is not news.
AMBIENT = "(e.source = 'drive' AND e.kind = 'context')"
_FILLER = {"the", "a", "an", "to", "for", "of", "on", "in", "with", "and", "your", "you", "about", "from", "at", "is",
           "it", "this", "that", "status", "still", "today", "now"}


def day_label(ts: float, tz, clock: bool = False) -> str:
    """'Mon 3 Mar' (or with the time). Everything the model reads is dated, so "today" can't outlive its day."""
    return datetime.fromtimestamp(ts, tz).strftime("%a %-d %b %H:%M" if clock else "%a %-d %b")


def day_iso(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, tz).date().isoformat()


def last_activity(db: DB, slug: str) -> float:
    """When something last really happened in a workstream (see AMBIENT for what doesn't count)."""
    row = db.one(f"SELECT MAX(MIN(e.ts, e.created)) AS t FROM events e JOIN memberships m ON m.ref = e.ref "
                 f"WHERE m.slug = ? AND NOT {AMBIENT}", (slug,))
    if row and row["t"]:
        return row["t"]
    ws = db.one("SELECT created FROM workstreams WHERE slug = ?", (slug,))
    return ws["created"] if ws else 0


def _words(text: str) -> set[str]:
    words = (w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _FILLER and len(w) > 1)
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}  # "reservations" and "reservation" match


def loop_id(text: str) -> str:
    """Short stable id for an open loop, so a to-do can point back at the loop it came from."""
    return hashlib.sha1(" ".join(sorted(_words(text))).encode()).hexdigest()[:6]


def same_task(a: str, b: str) -> bool:
    """Whether two short task descriptions are the same thing worded differently."""
    x, y = _words(a), _words(b)
    if not x or not y:
        return False
    common = len(x & y)
    return common / len(x | y) >= 0.6 or (min(len(x), len(y)) >= 3 and common / min(len(x), len(y)) >= 0.8)


def loop_line(loop: dict) -> str:
    return (f"{loop.get('kind', 'request')}: {loop.get('who', '?')} owes {loop.get('to', '?')}: {loop.get('text', '')} "
            f"(open since {loop.get('since') or 'an unknown date'})")


class Pipeline:
    def __init__(self, cfg: Config, db: DB, claude: Claude, me: str):
        self.cfg, self.db, self.claude, self.me = cfg, db, claude, me

    # -- routing ---------------------------------------------------------------
    def _workstream_lines(self, include_dormant: bool = False) -> str:
        rows = self.db.all(
            "SELECT w.slug, w.state, w.overview, w.title, COUNT(m.ref) AS n FROM workstreams w "
            "LEFT JOIN memberships m ON m.slug = w.slug WHERE w.state != 'dormant' OR ? GROUP BY w.slug "
            "ORDER BY w.updated DESC LIMIT 60", (int(include_dormant),))
        return "\n".join(f"- {r['slug']} · {r['state']} · {r['n']} members · {(r['overview'] or r['title'])[:220]}"
                         for r in rows) or "(none yet)"

    async def route(self) -> set[str]:
        """Attach unrouted events' refs to workstreams. Returns slugs that got new material."""
        pending = self.db.all("SELECT * FROM events WHERE routed = 0 ORDER BY ts DESC LIMIT 300")
        if not pending:
            return set()
        by_ref: dict[str, list[dict]] = defaultdict(list)
        for ev in pending:
            by_ref[ev["ref"]].append(ev)

        touched: set[str] = set()
        unresolved = []
        for ref in by_ref:
            slugs = [r["slug"] for r in self.db.all("SELECT slug FROM memberships WHERE ref = ?", (ref,))]
            if slugs:
                touched.update(slugs)  # deterministic: known artifact, known home
            else:
                unresolved.append(ref)

        for chunk_start in range(0, len(unresolved), 40):
            chunk = unresolved[chunk_start:chunk_start + 40]
            items = []
            for ref in chunk:
                item = self.db.one("SELECT title, source FROM items WHERE ref = ?", (ref,)) or {"title": "", "source": ""}
                evs = by_ref[ref][:3]
                lines = "; ".join(f"[{e['kind']}] {e['author']}→{e['target'] or '-'}: {e['text'][:160]}" for e in evs)
                items.append(f"- {ref} · {item['source']} · {item['title'][:80]} — {lines}")
            proposals = self.db.all("SELECT slug, COUNT(*) AS n FROM proposals WHERE created > ? GROUP BY slug",
                                    (time.time() - 6 * 3600,))
            try:
                directives = await self.claude.complete_json(
                    "You route work activity to workstreams. Output JSONL only.",
                    prompts.fill(prompts.ROUTER, workstreams=self._workstream_lines(),
                                 proposals="\n".join(f"- {p['slug']} ({p['n']} backer)" for p in proposals) or "(none)",
                                 items="\n".join(items)),
                    jsonl=True, fast=True, label="route")
            except Exception as e:
                log.warning("routing failed: %s", e)
                return touched  # leave events unrouted; retry next cycle
            touched |= self._apply_routes(directives, set(chunk))

        self.db.x("UPDATE events SET routed = 1 WHERE routed = 0 AND id IN (%s)" % ",".join("?" * len(pending)),
                  [e["id"] for e in pending])
        return touched

    def _apply_routes(self, directives: list[dict], valid_refs: set[str]) -> set[str]:
        now = time.time()
        existing = {r["slug"]: r for r in self.db.all(
            "SELECT w.slug, COUNT(m.ref) AS n FROM workstreams w LEFT JOIN memberships m ON m.slug = w.slug GROUP BY w.slug")}
        touched: set[str] = set()
        for d in directives:
            ref, slug = str(d.get("ref", "")), str(d.get("workstream", "")).strip().lower()
            try:
                conf = float(d.get("conf", 0))
            except (TypeError, ValueError):
                continue
            if ref not in valid_refs or not SLUG_RE.match(slug) or conf < ROUTE_FLOOR:
                continue
            if slug in existing:
                if existing[slug]["n"] >= 8 and conf < 0.85:
                    continue
                self.db.x("INSERT OR IGNORE INTO memberships(slug, ref, conf, added) VALUES(?,?,?,?)", (slug, ref, conf, now))
                touched.add(slug)
            else:
                self.db.x("INSERT OR IGNORE INTO proposals(slug, ref, conf, created) VALUES(?,?,?,?)", (slug, ref, conf, now))

        # Admit proposals with two backers, or one very confident backer carrying several events.
        for p in self.db.all("SELECT slug, COUNT(*) AS n, MAX(conf) AS c, GROUP_CONCAT(ref, '\x1f') AS refs "
                             "FROM proposals WHERE created > ? GROUP BY slug", (now - 6 * 3600,)):
            refs = p["refs"].split("\x1f")
            strong_single = p["n"] == 1 and p["c"] >= 0.9 and (
                self.db.one("SELECT COUNT(*) AS n FROM events WHERE ref = ?", (refs[0],))["n"] >= 2)
            if p["n"] >= 2 or strong_single:
                self.db.x("INSERT OR IGNORE INTO workstreams(slug, created, updated) VALUES(?,?,0)", (p["slug"], now))
                for ref in refs:
                    self.db.x("INSERT OR IGNORE INTO memberships(slug, ref, conf, added) VALUES(?,?,?,?)",
                              (p["slug"], ref, p["c"], now))
                self.db.x("DELETE FROM proposals WHERE slug = ?", (p["slug"],))
                touched.add(p["slug"])
        self.db.x("DELETE FROM proposals WHERE created < ?", (now - 24 * 3600,))
        return touched

    # -- synthesis -------------------------------------------------------------
    async def synthesize(self, slug: str) -> bool:
        ws = self.db.one("SELECT * FROM workstreams WHERE slug = ?", (slug,))
        if not ws:
            return False
        members = self.db.all("SELECT i.* FROM items i JOIN memberships m ON m.ref = i.ref WHERE m.slug = ? "
                              "ORDER BY i.updated DESC LIMIT 25", (slug,))
        refs = [m["ref"] for m in members]
        if not refs:
            return False
        events = self.db.all(
            "SELECT * FROM events WHERE ref IN (%s) AND created > ? ORDER BY ts LIMIT 60" % ",".join("?" * len(refs)),
            refs + [ws["updated"]])
        ambient = lambda e: e["source"] == "drive" and e["kind"] == "context"  # noqa: E731
        if ws["updated"] and all(ambient(e) for e in events):
            return False  # nothing but documents being edited (or nothing at all)
        tz, now = self.cfg.tz, time.time()
        today = self.cfg.now().date().isoformat()
        prior_loops = json.loads(ws["open_loops"] or "[]")
        closed = [c for c in json.loads(ws.get("closed") or "[]") if c.get("on", "") >= day_iso(now - CLOSED_KEEP_DAYS * 86400, tz)]
        log_rows = self.db.all("SELECT ts, text FROM workstream_log WHERE slug = ? ORDER BY ts DESC LIMIT 6", (slug,))
        prior = ("(new workstream)" if not ws["updated"] else
                 f"TITLE: {ws['title']}\nOVERVIEW (written {day_label(ws['updated'], tz)}): {ws['overview']}\nLOG:\n" +
                 "\n".join(f"- {day_label(r['ts'], tz)}: {r['text']}" for r in reversed(log_rows)) +
                 "\nOPEN LOOPS:\n" + ("\n".join(f"- {loop_line(l)}" for l in prior_loops) or "(none)") +
                 "\nCLOSED LOOPS:\n" + ("\n".join(f"- {c['text']} — closed {c['on']}: {c.get('why', '')}" for c in closed)
                                        or "(none)"))
        member_lines = "\n".join(f"- {m['ref']} · {m['title'][:90]} · {m['state']} · {m['url']}" for m in members)
        event_lines = "\n".join(
            f"- [{'document edited' if ambient(e) else e['kind']}] {e['author']}→{e['target'] or '-'}: {e['text']} "
            f"({day_label(min(e['ts'], e['created']), tz, clock=True)}, {e['ref']})" for e in events) or "(no new events)"
        try:
            out = await self.claude.complete_json(
                "You maintain one workstream summary. Output one JSON object only.",
                prompts.fill(prompts.SYNTHESIS, me=self.me, now=self.cfg.now().strftime("%A %-d %B %Y, %H:%M"),
                             today=today, prior=prior, members=member_lines, events=event_lines),
                label=f"synth:{slug}")
        except Exception as e:
            log.warning("synthesis %s failed: %s", slug, e)
            return False
        if not isinstance(out, dict):
            return False
        now = time.time()
        state = out.get("state") if out.get("state") in ("active", "waiting", "done") else "active"
        known = {loop_id(l.get("text", "")): l for l in prior_loops}
        loops = []
        for l in out.get("open_loops") or []:
            if not isinstance(l, dict) or not str(l.get("text", "")).strip():
                continue
            text = str(l["text"]).strip()[:300]
            since = str(l.get("since") or "")[:10]
            if not DATE_RE.match(since) or since > today:  # a loop keeps its date; a brand-new one starts today
                since = known[loop_id(text)].get("since", "") if loop_id(text) in known else today
            loops.append({"kind": str(l.get("kind", "request"))[:20], "who": str(l.get("who", ""))[:80],
                          "to": str(l.get("to", ""))[:80], "text": text, "since": since})
        self.db.x("UPDATE workstreams SET title = ?, overview = ?, state = ?, open_loops = ?, closed = ?, updated = ? "
                  "WHERE slug = ?",
                  (str(out.get("title") or ws["title"] or slug)[:120], str(out.get("overview", ""))[:1500], state,
                   json.dumps(loops[:12]), json.dumps(closed), now, slug))
        update = str(out.get("update", "")).strip()
        if update:
            self.db.x("INSERT INTO workstream_log(slug, ts, text) VALUES(?,?,?)", (slug, now, update[:500]))
        return True

    # -- curation --------------------------------------------------------------
    async def curate(self) -> list[str]:
        rows = self.db.all(
            "SELECT w.slug, w.state, w.overview, COUNT(m.ref) AS n FROM workstreams w "
            "LEFT JOIN memberships m ON m.slug = w.slug WHERE w.state IN ('active', 'waiting') AND w.overview != '' "
            "GROUP BY w.slug ORDER BY w.updated DESC LIMIT 40")
        fresh = time.time() - QUIET_DAYS * 86400
        for r in rows:
            r["active"] = last_activity(self.db, r["slug"])
        rows = [r for r in rows if r["active"] >= fresh]
        if not rows:
            return []
        lines = "\n".join(f"- {r['slug']} · {r['n']} · {r['state']} · last activity {day_label(r['active'], self.cfg.tz)} · "
                          f"{r['overview'][:260]}" for r in rows)
        try:
            directives = await self.claude.complete_json(
                "You curate a workstream graph. Output JSONL only.",
                prompts.fill(prompts.CURATOR, me=self.me, now=self.cfg.now().strftime("%A %-d %B %Y, %H:%M"),
                             workstreams=lines, cap=SURFACE_CAP),
                jsonl=True, fast=True, label="curate")
        except Exception as e:
            log.warning("curation failed: %s", e)
            return []
        valid = {r["slug"] for r in rows}
        now = time.time()
        surface: list[str] = []
        for d in directives:
            if d.get("kind") == "edge":
                src, dst = d.get("from"), d.get("to")
                try:
                    strength = float(d.get("strength", 0))
                except (TypeError, ValueError):
                    continue
                if src in valid and dst in valid and src != dst and strength >= 0.75:
                    self.db.x("INSERT INTO edges(src, dst, kind, strength, updated) VALUES(?,?,?,?,?) "
                              "ON CONFLICT(src, dst, kind) DO UPDATE SET strength = excluded.strength, updated = excluded.updated",
                              (src, dst, str(d.get("edge", "references"))[:20], strength, now))
            elif d.get("kind") == "surface":
                surface = [s for s in d.get("slugs", []) if s in valid][:SURFACE_CAP]
        if not surface:  # fall back to most recently active
            surface = [r["slug"] for r in rows[:SURFACE_CAP]]
        day = self.cfg.now().date().isoformat()
        self.db.x("INSERT INTO surface(day, slugs, updated) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET "
                  "slugs = excluded.slugs, updated = excluded.updated", (day, json.dumps(surface), now))
        return surface

    # -- one cycle -------------------------------------------------------------
    async def run(self) -> dict:
        touched = await self.route()
        # Refresh every workstream whose members have real news since its last synthesis, oldest wait first.
        stale = self.db.all(
            "SELECT m.slug, MIN(e.created) AS first FROM memberships m JOIN events e ON e.ref = m.ref "
            f"JOIN workstreams w ON w.slug = m.slug WHERE e.created > w.updated AND (w.updated = 0 OR NOT {AMBIENT}) "
            "GROUP BY m.slug ORDER BY first")
        targets = [r["slug"] for r in stale][:SYNTH_PER_CYCLE]
        results = await asyncio.gather(*(self.synthesize(s) for s in targets))
        changed = sum(1 for r in results if r)
        # Quiet workstreams fade out of routing after a week without anything real happening in them.
        cutoff = time.time() - DORMANT_DAYS * 86400
        for w in self.db.all("SELECT slug FROM workstreams WHERE state != 'dormant' AND updated > 0"):
            if last_activity(self.db, w["slug"]) < cutoff:
                self.db.x("UPDATE workstreams SET state = 'dormant' WHERE slug = ?", (w["slug"],))
        day = self.cfg.now().date().isoformat()
        surface = []
        if changed or not self.db.one("SELECT 1 FROM surface WHERE day = ?", (day,)):
            surface = await self.curate()
        return {"routed_to": len(touched), "synthesized": changed, "surface": surface}
