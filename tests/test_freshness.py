"""Checks that the brief stops repeating things that are finished, expired or simply old.

No network and no Claude: a stand-in model records the prompts it is given and returns canned answers, and everything
lives in a throwaway data folder with made-up people.

    .venv/bin/python tests/test_freshness.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

os.environ["DAYBREAK_HOME"] = tempfile.mkdtemp(prefix="daybreak-freshness-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daybreak import config  # noqa: E402
from daybreak.brief import Brief  # noqa: E402
from daybreak.db import DB, new_id  # noqa: E402
from daybreak.pipeline import Pipeline, last_activity, loop_id  # noqa: E402

DAY = 86400
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


class FakeClaude:
    """Returns the canned answer for a call's label (the part before ':') and remembers the prompt."""

    def __init__(self):
        self.prompts: dict[str, list[str]] = {}
        self.answers: dict[str, object] = {}

    async def complete_json(self, system, prompt, **kw):
        label = kw.get("label", "").split(":")[0]
        self.prompts.setdefault(label, []).append(prompt)
        answer = self.answers.get(label)
        return answer(prompt) if callable(answer) else answer


def event(db, ref, source, kind, text, ago, *, author="Ada Lovelace", target="me", title=""):
    ts = time.time() - ago
    db.x("INSERT INTO events(id, source, ref, kind, author, target, text, url, ts, created, routed, fingerprint) "
         "VALUES(?,?,?,?,?,?,?,?,?,?,1,?)", (new_id(), source, ref, kind, author, target, text, "", ts, ts, new_id()))
    db.x("INSERT INTO items(ref, source, title, state, updated) VALUES(?,?,?,?,?) ON CONFLICT(ref) DO UPDATE SET "
         "updated = excluded.updated", (ref, source, title or ref, "seen", ts))


def workstream(db, slug, title, overview, *, state="active", loops=(), synthesized_ago=0.0, refs=()):
    now = time.time()
    db.x("INSERT INTO workstreams(slug, title, overview, state, open_loops, created, updated) VALUES(?,?,?,?,?,?,?)",
         (slug, title, overview, state, json.dumps(list(loops)), now - 30 * DAY, now - synthesized_ago))
    for ref in refs:
        db.x("INSERT INTO memberships(slug, ref, conf, added) VALUES(?,?,0.9,?)", (slug, ref, now))


def todo(db, day, text, *, done=0, slug="", since="", loop=""):
    tid = new_id()
    db.x("INSERT INTO todos(id, day, text, detail, slug, done, done_at, since, loop) VALUES(?,?,?,?,?,?,?,?,?)",
         (tid, day, text, "", slug, done, time.time() - DAY if done else 0, since, loop))
    return tid


async def main():
    cfg = config.load()
    db = DB()
    claude = FakeClaude()
    today = cfg.now()
    iso = lambda days_ago: (today - timedelta(days=days_ago)).date().isoformat()  # noqa: E731

    # One clash that mattered for a single day three weeks ago, kept "alive" only by a shared sheet being edited.
    event(db, "gmail:clash", "gmail", "context", "Ada is on call today but off sick.", 20 * DAY)
    for ago in (18 * DAY, 9 * DAY, 3 * DAY, 3600):
        event(db, "gdrive:roster", "drive", "context", "Shared roster sheet was edited.", ago, author="Grace Hopper")
    workstream(db, "roster-clash", "Roster Clash Today", "Ada is on call today but off sick; cover is unconfirmed.",
               state="waiting", synthesized_ago=2 * DAY, refs=("gmail:clash", "gdrive:roster"),
               loops=[{"kind": "request", "who": "me", "to": "Grace Hopper", "text": "Confirm who covers on-call today"}])

    # A live effort with something new an hour ago.
    event(db, "jira:BILL-7", "atlassian", "request", "Review the cutover checklist for BILL-7.", 3600)
    cutover = {"kind": "request", "who": "me", "to": "Ada Lovelace", "text": "Review the cutover checklist",
               "since": iso(1)}
    invoice = {"kind": "commitment", "who": "me", "to": "Ada Lovelace", "text": "Send the invoice sample",
               "since": iso(2)}
    workstream(db, "billing-migration", "Billing Migration", "Moving invoices to the new ledger.",
               synthesized_ago=2 * 3600, refs=("jira:BILL-7",), loops=[cutover, invoice])

    # --- workstreams --------------------------------------------------------------------------------------------
    claude.answers["synth"] = {"title": "Billing Migration", "overview": "Cutover checklist is out for review.",
                               "update": "Ada asked for a review of the cutover checklist.", "state": "active",
                               "open_loops": [cutover, invoice]}
    claude.answers["curate"] = [{"kind": "surface", "slugs": ["roster-clash", "billing-migration"]}]
    await Pipeline(cfg, db, claude, "Ada Lovelace").run()

    synth = claude.prompts.get("synth", [])
    check("a sheet being edited doesn't trigger a workstream update", len(synth) == 1 and "BILL-7" in synth[0],
          f"{len(synth)} synthesis call(s)")
    check("last activity ignores document edits",
          abs(last_activity(db, "roster-clash") - (time.time() - 20 * DAY)) < 60)
    state = db.one("SELECT state FROM workstreams WHERE slug = 'roster-clash'")["state"]
    check("a workstream with no real activity for a week goes dormant", state == "dormant", state)
    check("workstream updates are written from dated events, not 'N hours ago'",
          synth and today.strftime("%-d %b") in synth[0] and "h ago" not in synth[0])
    surface = json.loads(db.one("SELECT slugs FROM surface ORDER BY day DESC LIMIT 1")["slugs"])
    check("a dormant workstream is never surfaced", surface == ["billing-migration"], str(surface))

    # --- to-dos -------------------------------------------------------------------------------------------------
    tid = todo(db, iso(1), "Send the invoice sample to Ada", slug="billing-migration", loop=loop_id(invoice["text"]))
    brief = Brief(cfg, db, claude, lambda: "Ada Lovelace")
    brief.mark_todo(tid, True)
    ws = db.one("SELECT open_loops, closed FROM workstreams WHERE slug = 'billing-migration'")
    check("ticking a to-do closes its open loop",
          [l["text"] for l in json.loads(ws["open_loops"])] == [cutover["text"]]
          and [c["text"] for c in json.loads(ws["closed"])] == [invoice["text"]])
    brief.mark_todo(tid, False)
    ws = db.one("SELECT open_loops, closed FROM workstreams WHERE slug = 'billing-migration'")
    check("un-ticking it reopens the loop", len(json.loads(ws["open_loops"])) == 2 and json.loads(ws["closed"]) == [])
    brief.mark_todo(tid, True)

    # --- the brief ----------------------------------------------------------------------------------------------
    db.x("INSERT INTO briefs(day, data, created) VALUES(?, '{}', ?)", (iso(1), time.time() - DAY))
    db.x("INSERT INTO surface(day, slugs, updated) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET slugs = excluded.slugs",
         (today.date().isoformat(), json.dumps(["roster-clash", "billing-migration"]), time.time()))
    todo(db, iso(1), "Chase Grace about the old outage report", since=iso(12))
    fresh = todo(db, iso(1), "Book the cutover rehearsal room", since=iso(2))
    db.put(f"painting:{today.date().isoformat()}", {"file": "x.jpg"})
    (config.PAINTINGS_DIR / f"{today.date().isoformat()}.jpg").write_bytes(b"x")

    claude.answers["verify"] = lambda prompt: {"checks": [
        {"id": line.split("]")[0].strip("- ["), "status": "done", "evidence": "BILL-7 checklist was approved yesterday"}
        for line in prompt.splitlines() if "Review the cutover checklist" in line and line.startswith("- [")]}
    claude.answers["brief"] = lambda prompt: {
        "caption": "A calm day.", "push_forward": [], "updates": [], "your_day": {}, "looking_ahead": {},
        "todos": [{"text": "Send Ada the invoice sample", "detail": "She asked twice."},
                  {"text": "Book the cutover rehearsal room", "detail": "Still open.", "carry": f"T-{fresh[:6]}"},
                  {"text": "Draft the ledger rollback note", "detail": "New today."}]}
    await brief.generate(today)

    prompt = claude.prompts["brief"][-1]
    check("the brief leaves out workstreams that have gone quiet", "roster-clash" not in prompt and "billing-migration" in prompt)
    check("the brief is told when each workstream last moved", "last activity" in prompt)
    check("the brief is told what was already ticked done", "Send the invoice sample to Ada" in prompt.split("ALREADY DONE")[1])
    check("a to-do carried for over a week is dropped", "old outage report" not in prompt)
    check("a recent carried to-do stays, with how long it has been open", "Book the cutover rehearsal room" in prompt
          and "open since" in prompt.split("Book the cutover rehearsal room")[1][:60])
    ws = db.one("SELECT open_loops, closed FROM workstreams WHERE slug = 'billing-migration'")
    check("a loop the re-check found finished is closed before the brief is written",
          json.loads(ws["open_loops"]) == [] and "approved yesterday" in ws["closed"]
          and "approved yesterday" in prompt)
    rows = db.all("SELECT text, since, done FROM todos WHERE day = ? ORDER BY position", (today.date().isoformat(),))
    texts = [r["text"] for r in rows]
    check("a to-do that matches one already ticked done is not raised again",
          texts == ["Book the cutover rehearsal room", "Draft the ledger rollback note"], str(texts))
    check("a carried to-do keeps the day it was first raised",
          rows and rows[0]["since"] == iso(2) and rows[1]["since"] == today.date().isoformat())

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
