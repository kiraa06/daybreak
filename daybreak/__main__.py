"""Command line: `python -m daybreak <command>`."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config

HOST_NAME = "com.daybreak.helper"


def setup_logging(verbose: bool) -> None:
    config.ensure_dirs()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    fh = logging.handlers.RotatingFileHandler(config.LOG_DIR / "daybreak.log", maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(sh)
    root.addHandler(fh)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def _one_shot(coro_fn):
    from .app import App
    app = App()

    async def go():
        return await coro_fn(app)
    return asyncio.run(go())


def _build_runtime(project: Path) -> str:
    """Install this checkout into its own environment in the data folder and return its Python.

    The login service then runs entirely from the data folder, so it never needs to read the checkout — on macOS a
    background process reading ~/Documents or ~/Desktop is blocked by privacy controls."""
    runtime = config.DATA_DIR / "runtime"
    python = runtime / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    if Path(uv).exists():
        subprocess.run([uv, "venv", "--clear", "--quiet", "--python", sys.executable, str(runtime)], check=True)
        subprocess.run([uv, "pip", "install", "--quiet", "--python", str(python), str(project)], check=True)
    else:
        subprocess.run([sys.executable, "-m", "venv", "--clear", str(runtime)], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "--quiet", str(project)], check=True)
    return str(python)


def cmd_install(args) -> None:
    from . import osutil
    cfg = config.load()
    config.token()
    project = Path(__file__).resolve().parent.parent
    if args.editable:  # development: run straight from the checkout
        python, workdir = sys.executable, project
    else:
        print(f"installing Daybreak into {config.DATA_DIR / 'runtime'} …")
        python, workdir = _build_runtime(project), config.DATA_DIR
    ext = config.DATA_DIR / "extension"
    if (project / "extension").exists():
        shutil.rmtree(ext, ignore_errors=True)
        shutil.copytree(project / "extension", ext)
        # The extension watches this stamp and reloads itself when an install changes it.
        (ext / "build.json").write_text(json.dumps({"build": int(time.time())}))
    launcher = osutil.write_launcher(python, cwd=None if not args.editable else project)
    browsers = osutil.install_native_host(HOST_NAME, launcher, cfg.extension_origins)
    print("native messaging host registered for: " + (", ".join(browsers) or "no Chromium browsers found"))
    env = {"PATH": f"{Path(python).parent}:{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}
    dotenv = config.DATA_DIR / ".env"
    if dotenv.exists():  # optional CLAUDE_CODE_OAUTH_TOKEN for when the login keychain is unavailable
        for line in dotenv.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    print(osutil.install_service(python, workdir, env, config.LOG_DIR))
    print(f"\nLoad the extension: chrome://extensions → Developer mode → Load unpacked → {ext}\n"
          "After pulling new code, run `daybreak install` again and reload the extension.")


def cmd_uninstall(_args) -> None:
    from . import osutil
    print(osutil.uninstall_service())
    removed = osutil.uninstall_native_host(HOST_NAME)
    print("native messaging host removed from: " + (", ".join(removed) or "none"))
    print(f"Data kept in {config.DATA_DIR} (delete it to remove everything).")


def main() -> None:
    p = argparse.ArgumentParser(prog="daybreak")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the helper (server + background loops)")
    sp = sub.add_parser("sense", help="read sources and update workstreams once")
    sp.add_argument("--hours", type=int, default=None, help="look-back window (default: since last run)")
    sub.add_parser("brief", help="generate today's brief now")
    sub.add_parser("calendar", help="refresh meetings from the calendar")
    pp = sub.add_parser("prep", help="build prep notes for a meeting")
    pp.add_argument("meeting", nargs="?", default="next")
    sub.add_parser("status", help="show current state")
    sub.add_parser("pair", help="print the pairing details for the extension")
    ip = sub.add_parser("install", help="install into the data folder, register the native host, start at login")
    ip.add_argument("--editable", action="store_true", help="run the service straight from this checkout (development)")
    sub.add_parser("uninstall", help="remove the native host and login item")
    args = p.parse_args()
    setup_logging(args.verbose)

    if args.cmd == "run":
        from .server import serve
        serve()
    elif args.cmd == "sense":
        print(json.dumps(_one_shot(lambda a: a.sense_cycle("cli", hours=args.hours)), indent=2))
    elif args.cmd == "brief":
        print(json.dumps(_one_shot(lambda a: a.make_brief("cli")), indent=2, default=str))
    elif args.cmd == "calendar":
        async def cal(a):
            await a.refresh_calendar()
            return [{k: m[k] for k in ("time", "title", "attendees", "join_url")} for m in a.meetings.upcoming(36)]
        print(json.dumps(_one_shot(cal), indent=2))
    elif args.cmd == "prep":
        async def prep(a):
            mid = args.meeting
            if mid == "next":
                up = [m for m in a.meetings.upcoming(36)]
                if not up:
                    return "no upcoming meetings"
                mid = up[0]["id"]
            aid = await a.meetings.prepare(mid)
            from . import artifacts
            return artifacts.url_for(a.cfg, aid) if aid else "prep failed"
        print(_one_shot(prep))
    elif args.cmd == "status":
        print(json.dumps(_one_shot(lambda a: asyncio.sleep(0, a.state())), indent=2, default=str))
    elif args.cmd == "pair":
        cfg = config.load()
        print(json.dumps({"port": cfg.port, "token": config.token()}))
    elif args.cmd == "install":
        cmd_install(args)
    elif args.cmd == "uninstall":
        cmd_uninstall(args)


if __name__ == "__main__":
    main()
