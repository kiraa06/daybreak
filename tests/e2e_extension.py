"""End-to-end check of the real extension in Playwright's Chromium.

Everything is isolated: a throwaway browser profile, and a throwaway helper seeded with the fictional demo data from
docs/make_demo.py on its own port. Your real helper, data and browsers are never touched.

Needs Python Playwright with its Chromium (`pip install playwright && playwright install chromium`):

    .venv/bin/python tests/e2e_extension.py

Checks: native-messaging pairing, the new-tab brief, a meeting pop-up injected into a normal page, the side panel and
its "Brief" button.
"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
PORT = 47898
OUT = Path(tempfile.mkdtemp(prefix="daybreak-e2e-"))
sys.path.insert(0, str(ROOT))
from daybreak import config  # noqa: E402  (only for the extension ID)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def start_helper():
    """Seed a demo data folder and serve it on its own port; returns (process, data folder)."""
    proc = subprocess.Popen([sys.executable, str(ROOT / "docs/make_demo.py"), "--serve", f"--port={PORT}"], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    info = json.loads(proc.stdout.readline())
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1)
            return proc, Path(info["home"])
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise SystemExit("isolated helper did not start")


def main():
    helper, home = start_helper()
    try:
        run(home)
    finally:
        helper.terminate()
    print(f"\nScreenshots in {OUT}")
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


def run(home: Path):
    profile = OUT / "profile"
    hosts = profile / "NativeMessagingHosts"
    hosts.mkdir(parents=True)
    # The test browser's native host answers with the isolated helper's port and token.
    launcher = OUT / "native_host"
    launcher.write_text(f'#!/bin/sh\nexport DAYBREAK_HOME="{home}"\ncd "{ROOT}" || exit 1\n'
                        f'exec "{sys.executable}" -m daybreak.native_host\n')
    launcher.chmod(0o755)
    (hosts / "com.daybreak.helper.json").write_text(json.dumps({
        "name": "com.daybreak.helper", "description": "Daybreak e2e", "path": str(launcher), "type": "stdio",
        "allowed_origins": [f"chrome-extension://{config.EXTENSION_ID}/"]}))

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile), channel="chromium", headless=True, viewport={"width": 1440, "height": 900},
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"])
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
        ext_id = sw.url.split("/")[2]
        check("extension loads with its fixed ID", ext_id == config.EXTENSION_ID, ext_id)

        page = ctx.new_page()
        page.goto(f"chrome-extension://{ext_id}/newtab.html")
        try:
            page.wait_for_selector("#brief:not([hidden]), #empty:not([hidden])", timeout=20000)
        except Exception:
            pass
        paired = page.evaluate("chrome.storage.local.get('pairing').then(r => r.pairing && r.pairing.port)")
        check("pairs with the helper over native messaging", paired == PORT, f"port {paired}")
        brief_visible = page.is_visible("#brief")
        check("new tab shows the brief", brief_visible,
              page.text_content("#title-day") if brief_visible else page.text_content("#empty-title"))
        if brief_visible:
            page.wait_for_function("document.querySelector('#painting').classList.contains('loaded')", timeout=15000)
            page.wait_for_timeout(1200)  # let the painting finish fading in
            sections = page.evaluate("[...document.querySelectorAll('main#brief section')].filter(s => !s.hidden)"
                                     ".map(s => (s.querySelector('h2')||{}).textContent).filter(Boolean)")
            check("brief sections render", len(sections) >= 3, ", ".join(sections))
            page.screenshot(path=str(OUT / "brief.png"), full_page=False)

        # Queue a meeting pop-up due now (in the isolated helper) and wait for the extension to show it on a web page.
        db = sqlite3.connect(str(home / "daybreak.db"))
        nid = "e2e" + uuid.uuid4().hex[:10]
        payload = {"title": "E2E Test Meeting", "attendees": ["Ada Lovelace", "Alan Turing"],
                   "join_url": "https://meet.google.com/e2e-test", "prep_url": ""}
        db.execute("INSERT INTO nudges(id, kind, title, body, payload, dedupe, deliver_at, expires_at) VALUES(?,?,?,?,?,?,?,?)",
                   (nid, "meeting", "E2E Test Meeting", "in 5 min", json.dumps(payload), nid, time.time(), time.time() + 300))
        db.commit()
        web = ctx.new_page()
        web.goto("https://example.com")
        web.bring_to_front()
        found = False
        for _ in range(45):
            found = web.evaluate("!!document.getElementById('daybreak-toast-host')")
            if found:
                break
            time.sleep(2)
        check("meeting pop-up appears on a normal page", found)
        if found:
            text = web.evaluate("document.getElementById('daybreak-toast-host').shadowRoot.textContent.replace(/\\s+/g,' ')")
            check("pop-up shows title, people and Join", "E2E Test Meeting" in text and "Join" in text)
            web.screenshot(path=str(OUT / "popup.png"))

        panel = ctx.new_page()
        panel.goto(f"chrome-extension://{ext_id}/sidepanel.html")
        panel.wait_for_timeout(2500)
        check("side panel loads", panel.is_visible("#input"), panel.text_content("#hello"))
        with ctx.expect_page(timeout=10000) as opened:
            panel.click("#open-brief")
        brief_tab = opened.value
        brief_tab.wait_for_load_state()
        check("side panel's Brief button opens the brief", brief_tab.url.endswith("/newtab.html"))
        panel.screenshot(path=str(OUT / "sidepanel.png"))
        ctx.close()


if __name__ == "__main__":
    main()
