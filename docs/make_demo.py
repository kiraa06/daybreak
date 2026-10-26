"""Render the README screenshots from made-up data (never from a real account).

Seeds a throwaway data folder with a fictional person's brief, starts the helper in dev mode against it (no background
loops, so no connector is ever called) and screenshots the pages with Playwright:

    .venv/bin/python docs/make_demo.py            # write docs/screenshots/*
    .venv/bin/python docs/make_demo.py --serve    # just seed and serve (used by tests/e2e_extension.py)
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs/screenshots"
PORT = int(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--port=")), 47899))
HOME = Path(tempfile.mkdtemp(prefix="daybreak-demo-"))
os.environ["DAYBREAK_HOME"] = str(HOME)
sys.path.insert(0, str(ROOT))

from daybreak import config  # noqa: E402  (reads DAYBREAK_HOME at import)

config.ensure_dirs()
config.CONFIG_PATH.write_text(config.DEFAULT_CONFIG.replace(f"port = {config.DEFAULT_PORT}", f"port = {PORT}")
                              .replace('name = ""', 'name = "Robin Okafor"'))

from daybreak.brief import fetch_painting  # noqa: E402
from daybreak.db import DB, new_id  # noqa: E402

TZ = config.load().tz
NOW = datetime.now(TZ)
DAY = NOW.date().isoformat()


def at(h: int, m: int = 0) -> float:
    return NOW.replace(hour=h, minute=m, second=0, microsecond=0).timestamp()


BRIEF = {
    "title": f"The {NOW.strftime('%A')} Brief",
    "caption": "One review that matters at 3, a long quiet stretch after it. Good day to finish things.",
    "push_forward": [
        {"headline": "Walk into the 3:00 pricing review with the churn numbers in hand",
         "body": "Maya asked for September churn split by plan before the review, and the dashboard query still "
                 "double-counts annual upgrades (ANA-212). Fix the filter, export the two charts, drop them in the "
                 "review doc. Done means Maya doesn't have to ask twice.",
         "workstream": "pricing-page-refresh", "prompt": "Help me fix the churn query and prep charts for the pricing review"},
        {"headline": "Unblock the onboarding release by answering Theo's copy question",
         "body": "Theo is holding the v2 build on one decision: whether step three says 'workspace' or 'team'. "
                 "He asked yesterday at 6pm and the build train leaves Thursday.",
         "workstream": "mobile-onboarding-v2", "prompt": "Draft a reply to Theo about the step three copy"},
    ],
    "updates": [
        {"workstream": "pricing-page-refresh", "title": "Pricing Page Refresh", "tag": "Needs you",
         "text": "Maya moved the review to 3pm today and wants churn by plan before then. Design signed off on the new "
                 "tier cards on Friday, so the numbers are the last open piece. Priya is presenting; you're on "
                 "questions.", "source": {"label": "Open ANA-212", "url": "https://example.com/ANA-212"},
         "people": ["Maya Chen", "Priya Raman"]},
        {"workstream": "mobile-onboarding-v2", "title": "Mobile Onboarding v2", "tag": "Active",
         "text": "QA passed 41 of 43 cases overnight; the two failures are the same flaky permissions dialog. Theo wants "
                 "a copy call on step three before he cuts the release branch.",
         "source": {"label": "Open thread in #onboarding", "url": "https://example.com/slack/onboarding"},
         "people": ["Theo Vasquez", "Ines Duarte"]},
        {"workstream": "warehouse-migration", "title": "Warehouse Migration", "tag": "Awaiting reply",
         "text": "You asked platform for a cutover window last Wednesday and haven't heard back. Nothing is blocked yet, "
                 "but the old cluster's contract renews on the 14th.",
         "source": {"label": "Open email to Platform", "url": "https://example.com/mail/123"},
         "people": ["Sam Ortiz"]},
    ],
    "your_day": {"summary": "Four meetings, front-loaded. The 3:00 pricing review is the one to walk into prepared: "
                            "Maya will open with churn, and Priya is counting on you for the follow-ups. After 4:30 the "
                            "afternoon is yours.", "prep_meeting_id": "m3", "notes": {}},
    "looking_ahead": {"intro": "Tomorrow is lighter: just standup and a 1:1 with Sam. The warehouse contract renews in "
                               "two weeks, so it's a good day to chase that cutover window.",
                      "options": [{"label": "Finish the warehouse cutover plan", "detail": "Pull the open questions from "
                                   "email and Jira into one checklist", "prompt": "Help me plan the warehouse cutover"},
                                  {"label": "Ship onboarding v2", "detail": "Release notes, QA status and the last copy fixes",
                                   "prompt": "Get onboarding v2 ready to ship"},
                                  {"label": "Catch up on what I missed", "detail": "Threads and docs from the last two days",
                                   "prompt": "Catch up on what I missed"}]},
    "generated_at": time.time(),
}
TODOS = [
    ("Fix the double-counted upgrades in the churn query", "Maya needs the corrected split before 3:00.",
     {"label": "Open ANA-212", "url": "https://example.com/ANA-212"}),
    ("Reply to Theo about step three copy", "He's holding the release branch on it.",
     {"label": "Open thread in #onboarding", "url": "https://example.com/slack"}),
    ("Nudge platform about the cutover window", "Asked last Wednesday; contract renews on the 14th.",
     {"label": "Open email to Platform", "url": "https://example.com/mail"}),
    ("Review Ines's QA notes on the permissions dialog", "Two flaky cases, same root cause.",
     {"label": "Open QA doc", "url": "https://example.com/doc"}),
]
MEETINGS = [("m1", "Design crit", 9, 30, ["Ines Duarte", "Theo Vasquez", "Robin Okafor"]),
            ("m2", "1:1 with Maya", 11, 0, ["Maya Chen", "Robin Okafor"]),
            ("m3", "Pricing review", 15, 0, ["Maya Chen", "Priya Raman", "Robin Okafor", "Sam Ortiz"]),
            ("m4", "Onboarding standup", 16, 30, ["Theo Vasquez", "Ines Duarte", "Robin Okafor"])]
CHAT = [("user", "What should I focus on today?"),
        ("assistant", "**The pricing review at 3:00 is the day's center of gravity.** Maya wants September churn by plan, "
                      "and the query behind it still double-counts annual upgrades (per ANA-212).\n\n"
                      "1. **Fix the churn filter**, then export the two charts for Priya's deck.\n"
                      "2. **Answer Theo's copy question** — he's holding the onboarding release branch on it.\n"
                      "3. **Nudge platform** about the cutover window; the old cluster renews on the 14th.\n\n"
                      "Want me to draft the reply to Theo while you do the query?")]


def seed() -> None:
    db = DB()
    painting = asyncio.run(fetch_painting(db, DAY))
    db.x("INSERT OR REPLACE INTO briefs(day, data, painting, created) VALUES(?,?,?,?)",
         (DAY, json.dumps(BRIEF), json.dumps(painting), time.time()))
    for i, (text, detail, source) in enumerate(TODOS):
        db.x("INSERT INTO todos(id, day, text, detail, prompt, position, source, done) VALUES(?,?,?,?,?,?,?,?)",
             (new_id(), DAY, text, detail, f"Help me: {text}", i, json.dumps(source), 1 if i == 3 else 0))
    for mid, title, h, m, people in MEETINGS:
        db.x("INSERT INTO meetings(id, title, start, end, attendees, join_url, updated) VALUES(?,?,?,?,?,?,?)",
             (mid, title, at(h, m), at(h, m) + 1800, json.dumps(people), "https://example.com/meet", time.time()))
    db.x("INSERT INTO chats(id, title, created, updated) VALUES('demo', 'What should I focus on today?', ?, ?)",
         (time.time(), time.time()))
    for role, content in CHAT:
        db.x("INSERT INTO chat_messages(chat_id, role, content, created) VALUES('demo', ?, ?, ?)", (role, content, time.time()))
    db.put("me", {"name": "Robin Okafor"})
    db.put("last_sense", time.time() - 600)
    db.put("source_status", {k: {"ok": True, "added": 3, "at": time.time() - 600}
                             for k in ("gmail", "calendar", "atlassian", "drive", "slack")})


def main() -> None:
    seed()
    env = {**os.environ, "DAYBREAK_DEV": "1", "DAYBREAK_NO_LOOPS": "1", "DAYBREAK_HOME": str(HOME)}
    if "--serve" in sys.argv:  # isolated helper for tests: announce where it lives, then serve until killed
        print(json.dumps({"home": str(HOME), "port": PORT}), flush=True)
        os.execve(sys.executable, [sys.executable, "-m", "daybreak", "run"], env)
    server = subprocess.Popen([sys.executable, "-m", "daybreak", "run"], cwd=ROOT, env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        shoot()
    finally:
        server.terminate()
    print(f"screenshots written to {OUT}")


def shoot() -> None:
    from playwright.sync_api import sync_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}/dev"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=2)
        page.goto(f"{base}/newtab.html")
        page.wait_for_function("document.querySelector('#painting').classList.contains('loaded')", timeout=20000)
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / "brief.png"))

        page.evaluate("document.querySelector('#todo-row').scrollIntoView({block: 'start'}); window.scrollBy(0, -40)")
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUT / "todos-and-updates.png"))

        page.evaluate("document.querySelector('#day-row').scrollIntoView({block: 'start'}); window.scrollBy(0, -40)")
        page.wait_for_timeout(300)
        page.evaluate("""import('/dev/toast.js').then(m => m.renderToast({id: 'demo', kind: 'meeting', title: 'Pricing review',
            body: 'in 5 min', payload: {attendees: ['Maya Chen', 'Priya Raman', 'Sam Ortiz'],
            join_url: 'https://example.com/meet', prep_url: 'https://example.com/prep'}}))""")
        page.wait_for_timeout(700)
        page.screenshot(path=str(OUT / "your-day-and-popup.png"))

        panel = browser.new_page(viewport={"width": 420, "height": 760}, device_scale_factor=2)
        panel.goto(f"{base}/sidepanel.html")
        panel.wait_for_timeout(800)
        panel.select_option("#history", "demo")
        panel.wait_for_timeout(800)
        panel.screenshot(path=str(OUT / "side-panel.png"))
        browser.close()


if __name__ == "__main__":
    main()
