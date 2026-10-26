"""Platform specifics: data folder, native-messaging hosts for Chromium browsers, login service, notifications."""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")
SERVICE_NAME = "com.daybreak.helper"


def data_dir() -> Path:
    if os.environ.get("DAYBREAK_HOME"):
        return Path(os.environ["DAYBREAK_HOME"])
    if IS_MAC:
        return HOME / "Library/Application Support/Daybreak"
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA", HOME / "AppData/Local")) / "Daybreak"
    return Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "daybreak"


# Where each Chromium browser looks for per-user native messaging host manifests.
# Only browsers whose profile folder exists get a manifest.
_MAC_BROWSERS = {
    "Google Chrome": "Google/Chrome", "Chrome Beta": "Google/Chrome Beta", "Chrome Dev": "Google/Chrome Dev",
    "Chrome Canary": "Google/Chrome Canary", "Chrome for Testing": "Google/Chrome for Testing", "Chromium": "Chromium",
    "Microsoft Edge": "Microsoft Edge", "Brave": "BraveSoftware/Brave-Browser", "Vivaldi": "Vivaldi",
    "Opera": "com.operasoftware.Opera", "Arc": "Arc/User Data", "Dia": "Dia/User Data", "Helium": "net.imput.helium",
}
_LINUX_BROWSERS = {
    "Google Chrome": "google-chrome", "Chrome Beta": "google-chrome-beta", "Chrome Dev": "google-chrome-unstable",
    "Chromium": "chromium", "Microsoft Edge": "microsoft-edge", "Brave": "BraveSoftware/Brave-Browser",
    "Vivaldi": "vivaldi", "Opera": "opera",
}
_WIN_REGISTRY = {
    "Google Chrome": r"Software\Google\Chrome\NativeMessagingHosts",
    "Chromium": r"Software\Chromium\NativeMessagingHosts",
    "Microsoft Edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
    "Brave": r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts",
    "Vivaldi": r"Software\Vivaldi\NativeMessagingHosts",
}


def browser_host_dirs() -> dict[str, Path]:
    if IS_MAC:
        base, table = HOME / "Library/Application Support", _MAC_BROWSERS
    elif IS_WIN:
        return {}
    else:
        base, table = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")), _LINUX_BROWSERS
    return {name: base / rel / "NativeMessagingHosts" for name, rel in table.items() if (base / rel).exists()}


def write_launcher(python: str, cwd: Path | None = None) -> Path:
    """The manifest must point at an executable; wrap `python -m daybreak.native_host` in a tiny launcher."""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        launcher = d / "native_host.bat"
        cd = f'cd /d "{cwd}"\r\n' if cwd else ""
        launcher.write_text(f'@echo off\r\n{cd}"{python}" -m daybreak.native_host %*\r\n')
    else:
        launcher = d / "native_host"
        cd = f'cd "{cwd}" || exit 1\n' if cwd else ""
        launcher.write_text(f'#!/bin/sh\n{cd}exec "{python}" -m daybreak.native_host\n')
        launcher.chmod(0o755)
    return launcher


def install_native_host(host_name: str, launcher: Path, origins: list[str]) -> list[str]:
    manifest = {"name": host_name, "description": "Daybreak helper pairing", "path": str(launcher), "type": "stdio",
                "allowed_origins": origins}
    done = []
    if IS_WIN:
        import winreg
        path = data_dir() / f"{host_name}.json"
        path.write_text(json.dumps(manifest, indent=2))
        for name, key in _WIN_REGISTRY.items():
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{key}\\{host_name}") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, str(path))
            done.append(name)
        return done
    for name, d in browser_host_dirs().items():
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{host_name}.json").write_text(json.dumps(manifest, indent=2))
        done.append(name)
    return done


def uninstall_native_host(host_name: str) -> list[str]:
    removed = []
    if IS_WIN:
        import winreg
        for name, key in _WIN_REGISTRY.items():
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f"{key}\\{host_name}")
                removed.append(name)
            except OSError:
                pass
        return removed
    for name, d in browser_host_dirs().items():
        p = d / f"{host_name}.json"
        if p.exists():
            p.unlink()
            removed.append(name)
    return removed


# --- login service ------------------------------------------------------------

def install_service(python: str, workdir: Path, env: dict[str, str], log_dir: Path) -> str:
    args = [python, "-m", "daybreak", "run"]
    if IS_MAC:
        plist_path = HOME / "Library/LaunchAgents" / f"{SERVICE_NAME}.plist"
        plist = {"Label": SERVICE_NAME, "ProgramArguments": args, "WorkingDirectory": str(workdir), "RunAtLoad": True,
                 "KeepAlive": True, "EnvironmentVariables": env, "ProcessType": "Background",
                 "StandardOutPath": str(log_dir / "service.out.log"), "StandardErrorPath": str(log_dir / "service.err.log")}
        plist_path.parent.mkdir(exist_ok=True)
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)], capture_output=True)
        plist_path.write_bytes(plistlib.dumps(plist))
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)], capture_output=True, text=True)
        return f"launchd agent {plist_path} ({'started' if r.returncode == 0 else r.stderr.strip()})"
    if IS_WIN:
        return ("Windows: no login service is installed automatically. Add a Task Scheduler task that runs "
                f"`{' '.join(args)}` at log on, or run it manually.")
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "daybreak.service"
    env_lines = "\n".join(f'Environment="{k}={v}"' for k, v in env.items())
    unit.write_text(f"[Unit]\nDescription=Daybreak helper\n\n[Service]\nWorkingDirectory={workdir}\n"
                    f"ExecStart={' '.join(args)}\nRestart=always\n{env_lines}\n\n[Install]\nWantedBy=default.target\n")
    r = subprocess.run(["systemctl", "--user", "enable", "--now", "daybreak.service"], capture_output=True, text=True)
    return f"systemd user unit {unit} ({'started' if r.returncode == 0 else r.stderr.strip()})"


def uninstall_service() -> str:
    if IS_MAC:
        plist_path = HOME / "Library/LaunchAgents" / f"{SERVICE_NAME}.plist"
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)], capture_output=True)
        if plist_path.exists():
            plist_path.unlink()
            return f"removed {plist_path}"
        return "no launchd agent"
    if IS_WIN:
        return "Windows: remove the Task Scheduler task if you created one."
    subprocess.run(["systemctl", "--user", "disable", "--now", "daybreak.service"], capture_output=True)
    unit = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "systemd/user/daybreak.service"
    if unit.exists():
        unit.unlink()
        return f"removed {unit}"
    return "no systemd unit"


# --- notifications --------------------------------------------------------------

def notify(title: str, subtitle: str, body: str) -> None:
    """Fallback desktop notification for when no browser has the extension running."""
    try:
        if IS_MAC:
            script = f"display notification {json.dumps(body)} with title {json.dumps(title)} subtitle {json.dumps(subtitle)}"
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        elif not IS_WIN and shutil.which("notify-send"):
            subprocess.run(["notify-send", "-a", "Daybreak", f"{title}: {subtitle}", body], capture_output=True, timeout=10)
    except Exception:
        pass
