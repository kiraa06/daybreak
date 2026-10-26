"""Workstream pipeline: route new events to workstreams, re-synthesize what changed, curate today's surface."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict

from . import prompts
from .claude import Claude
from .config import Config
from .db import DB

log = logging.getLogger("daybreak.pipeline")

ROUTE_FLOOR = 0.75
SURFACE_CAP = 6
SYNTH_PER_CYCLE = 10
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _ago(ts: float) -> str:
    mins = int((time.time() - ts) / 60)
    if mins < 60:
        return f"{mins}m ago"
    if mins < 1440:
        return f"{mins // 60}h ago"
    return f"{mins // 1440}d ago"


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
        if not events and ws["updated"]:
            return False
        log_rows = self.db.all("SELECT ts, text FROM workstream_log WHERE slug = ? ORDER BY ts DESC LIMIT 6", (slug,))
        prior = ("(new workstream)" if not ws["updated"] else
                 f"TITLE: {ws['title']}\nOVERVIEW: {ws['overview']}\nLOG:\n" +
                 "\n".join(f"- {_ago(r['ts'])}: {r['text']}" for r in reversed(log_rows)))
        member_lines = "\n".join(f"- {m['ref']} · {m['title'][:90]} · {m['state']} · {m['url']}" for m in members)
        event_lines = "\n".join(f"- [{e['kind']}] {e['author']}→{e['target'] or '-'}: {e['text']} ({_ago(e['ts'])}, {e['ref']})"
                                for e in events) or "(no new events)"
        try:
            out = await self.claude.complete_json(
                "You maintain one workstream summary. Output one JSON object only.",
                prompts.fill(prompts.SYNTHESIS, me=self.me, now=self.cfg.now().isoformat(timespec="minutes"),
                             prior=prior, members=member_lines, events=event_lines),
                label=f"synth:{slug}")
        except Exception as e:
            log.warning("synthesis %s failed: %s", slug, e)
            return False
        if not isinstance(out, dict):
            return False
        now = time.time()
        state = out.get("state") if out.get("state") in ("active", "waiting", "done") else "active"
        self.db.x("UPDATE workstreams SET title = ?, overview = ?, state = ?, open_loops = ?, updated = ? WHERE slug = ?",
                  (str(out.get("title") or ws["title"] or slug)[:120], str(out.get("overview", ""))[:1500], state,
                   json.dumps(out.get("open_loops") or [])[:6000], now, slug))
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
        if not rows:
            return []
        lines = "\n".join(f"- {r['slug']} · {r['n']} · {r['state']} · {r['overview'][:260]}" for r in rows)
        try:
            directives = await self.claude.complete_json(
                "You curate a workstream graph. Output JSONL only.",
                prompts.fill(prompts.CURATOR, me=self.me, now=self.cfg.now().isoformat(timespec="minutes"),
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
        # Workstreams whose members received events after their last synthesis also need a refresh.
        stale = self.db.all(
            "SELECT DISTINCT m.slug FROM memberships m JOIN events e ON e.ref = m.ref "
            "JOIN workstreams w ON w.slug = m.slug WHERE e.created > w.updated")
        touched |= {r["slug"] for r in stale}
        targets = sorted(touched)[:SYNTH_PER_CYCLE]
        results = await asyncio.gather(*(self.synthesize(s) for s in targets))
        changed = sum(1 for r in results if r)
        day = self.cfg.now().date().isoformat()
        surface = []
        if changed or not self.db.one("SELECT 1 FROM surface WHERE day = ?", (day,)):
            surface = await self.curate()
        # Quiet workstreams fade out of routing after a week.
        self.db.x("UPDATE workstreams SET state = 'dormant' WHERE state != 'dormant' AND updated > 0 AND updated < ?",
                  (time.time() - 7 * 86400,))
        return {"routed_to": len(touched), "synthesized": changed, "surface": surface}
