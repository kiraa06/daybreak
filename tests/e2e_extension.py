"""End-to-end check of the real extension in Playwright's Chromium (a throwaway profile; your browsers are untouched).

Needs: the helper running (`daybreak run`), and Python Playwright with its Chromium (`pip install playwright &&
playwright install chromium`). Run with any Python that has Playwright:

    python tests/e2e_extension.py [path/to/daybreak/.venv/bin/python]

Checks: native-messaging pairing, the new-tab brief, a meeting pop-up injected into a normal page, the side panel.
"""
import json
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
sys.path.insert(0, str(ROOT))
from daybreak import config  # noqa: E402

HELPER_PY = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / ".venv/bin/python")
OUT = Path(tempfile.mkdtemp(prefix="daybreak-e2e-"))
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def main():
    cfg = config.load()
    profile = OUT / "profile"
    # Register the native host inside the throwaway profile only (its own launcher, so an installed one is untouched).
    hosts = profile / "NativeMessagingHosts"
    hosts.mkdir(parents=True)
    launcher = OUT / "native_host"
    launcher.write_text(f'#!/bin/sh\ncd "{ROOT}" || exit 1\nexec "{HELPER_PY}" -m daybreak.native_host\n')
    launcher.chmod(0o755)
    (hosts / "com.daybreak.helper.json").write_text(json.dumps({
        "name": "com.daybreak.helper", "description": "Daybreak e2e", "path": str(launcher), "type": "stdio",
        "allowed_origins": cfg.extension_origins}))

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
        paired = page.evaluate("chrome.storage.local.get('pairing').then(r => !!(r.pairing && r.pairing.token))")
        check("pairs with the helper over native messaging", paired)
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

        # Queue a meeting pop-up due now and wait for the service worker's poll to show it on a normal page.
        db = sqlite3.connect(str(config.DB_PATH))
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
            check("pop-up shows title, people and Join", "E2E Test Meeting" in text and "Join" in text, text[:120])
            web.screenshot(path=str(OUT / "popup.png"))
        db.execute("DELETE FROM nudges WHERE id = ?", (nid,))
        db.commit()

        panel = ctx.new_page()
        panel.goto(f"chrome-extension://{ext_id}/sidepanel.html")
        panel.wait_for_timeout(2500)
        check("side panel loads", panel.is_visible("#input"), panel.text_content("#hello"))
        panel.screenshot(path=str(OUT / "sidepanel.png"))
        ctx.close()

    print(f"\nScreenshots in {OUT}")
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
