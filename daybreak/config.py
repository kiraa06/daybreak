"""Configuration and on-disk locations.

Everything Daybreak stores lives in one folder (macOS: ~/Library/Application Support/Daybreak, Linux:
~/.local/share/daybreak, Windows: %LOCALAPPDATA%\\Daybreak): config.toml, daybreak.db, token, artifacts/,
paintings/, logs/.
"""
from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .osutil import data_dir

DATA_DIR = data_dir()
CONFIG_PATH = DATA_DIR / "config.toml"
DB_PATH = DATA_DIR / "daybreak.db"
TOKEN_PATH = DATA_DIR / "token"
ARTIFACTS_DIR = DATA_DIR / "artifacts"
PAINTINGS_DIR = DATA_DIR / "paintings"
LOG_DIR = DATA_DIR / "logs"

DEFAULT_PORT = 47821
EXTENSION_ID = "hkdpeghplcpifmffofnnhlkbiookfefo"  # fixed by the "key" in extension/manifest.json

DEFAULT_CONFIG = f"""\
# Daybreak configuration. Restart the helper after editing.

[user]
name = ""          # how the assistant refers to you; blank = learned from your tools
timezone = ""      # IANA name, e.g. "Asia/Kolkata"; blank = this Mac's timezone

[schedule]
brief_time = "08:30"                              # when the Morning Brief should be ready
brief_days = ["mon", "tue", "wed", "thu", "fri"]
sense_every_minutes = 30                          # how often to refresh workstreams
work_hours = ["08:00", "20:00"]                   # background refreshes only run inside these
meeting_prep_lead_minutes = 45                    # build prep notes this long before a meeting
meeting_nudges = [5, 0]                           # pop-ups this many minutes before start
max_proactive_nudges_per_hour = 3

[sources]
gmail = true
atlassian = true            # Jira + Confluence
drive = true
calendar = true             # claude.ai Google Calendar connector
calendar_ics_url = ""       # optional private ICS feed; used instead of the connector when set
slack = "browser"           # "browser" = your browser's Slack session via the extension, "connector", or "off"
slack_workspace = ""        # e.g. "https://yourcompany.slack.com" (browser mode)
bitbucket_workspace = ""    # optional; token read from env BITBUCKET_API_TOKEN or keychain item "daybreak-bitbucket"
bitbucket_email = ""

[models]
default = ""   # blank = your Claude Code default model
fast = ""      # used for extraction and routing; blank = default

[server]
port = {DEFAULT_PORT}
extra_extension_ids = []    # add the ID of a Chrome Web Store / self-packed build of the extension here
"""


def _hhmm(value: str) -> tuple[int, int]:
    h, m = value.strip().split(":")
    return int(h), int(m)


@dataclass
class Config:
    name: str = ""
    timezone: str = ""
    brief_time: tuple[int, int] = (8, 30)
    brief_days: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    sense_every_minutes: int = 30
    work_hours: tuple[tuple[int, int], tuple[int, int]] = ((8, 0), (20, 0))
    meeting_prep_lead_minutes: int = 45
    meeting_nudges: list[int] = field(default_factory=lambda: [5, 0])
    max_proactive_nudges_per_hour: int = 3
    gmail: bool = True
    atlassian: bool = True
    drive: bool = True
    calendar: bool = True
    calendar_ics_url: str = ""
    slack: str = "browser"
    slack_workspace: str = ""
    bitbucket_workspace: str = ""
    bitbucket_email: str = ""
    model_default: str = ""
    model_fast: str = ""
    port: int = DEFAULT_PORT
    extra_extension_ids: list[str] = field(default_factory=list)

    @property
    def extension_origins(self) -> list[str]:
        return [f"chrome-extension://{i}/" for i in [EXTENSION_ID, *self.extra_extension_ids]]

    @property
    def tz(self) -> ZoneInfo:
        if self.timezone:
            return ZoneInfo(self.timezone)
        local = datetime.now().astimezone().tzinfo
        name = getattr(local, "key", None)
        if name:
            return ZoneInfo(name)
        try:
            return ZoneInfo(os.path.realpath("/etc/localtime").split("zoneinfo/")[-1])
        except Exception:
            return ZoneInfo("UTC")

    def now(self) -> datetime:
        return datetime.now(self.tz)


def ensure_dirs() -> None:
    for d in (DATA_DIR, ARTIFACTS_DIR, PAINTINGS_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load() -> Config:
    ensure_dirs()
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG)
    raw = tomllib.loads(CONFIG_PATH.read_text())
    user, sched, src = raw.get("user", {}), raw.get("schedule", {}), raw.get("sources", {})
    models, server = raw.get("models", {}), raw.get("server", {})
    wh = sched.get("work_hours", ["08:00", "20:00"])
    return Config(
        name=user.get("name", ""),
        timezone=user.get("timezone", ""),
        brief_time=_hhmm(sched.get("brief_time", "08:30")),
        brief_days=[d.lower()[:3] for d in sched.get("brief_days", Config().brief_days)],
        sense_every_minutes=int(sched.get("sense_every_minutes", 30)),
        work_hours=(_hhmm(wh[0]), _hhmm(wh[1])),
        meeting_prep_lead_minutes=int(sched.get("meeting_prep_lead_minutes", 45)),
        meeting_nudges=[int(x) for x in sched.get("meeting_nudges", [5, 0])],
        max_proactive_nudges_per_hour=int(sched.get("max_proactive_nudges_per_hour", 3)),
        gmail=bool(src.get("gmail", True)),
        atlassian=bool(src.get("atlassian", True)),
        drive=bool(src.get("drive", True)),
        calendar=bool(src.get("calendar", True)),
        calendar_ics_url=src.get("calendar_ics_url", ""),
        slack=str(src.get("slack", "browser")),
        slack_workspace=src.get("slack_workspace", "").rstrip("/"),
        bitbucket_workspace=src.get("bitbucket_workspace", ""),
        bitbucket_email=src.get("bitbucket_email", ""),
        model_default=models.get("default", ""),
        model_fast=models.get("fast", ""),
        port=int(server.get("port", DEFAULT_PORT)),
        extra_extension_ids=[str(i) for i in server.get("extra_extension_ids", [])],
    )


def set_value(section: str, key: str, value: str) -> None:
    """Rewrite one `key = "value"` line inside [section] of config.toml, keeping comments."""
    lines = CONFIG_PATH.read_text().splitlines()
    current, out, done = None, [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
        if not done and current == section and stripped.split("=")[0].strip() == key:
            comment = ""
            if "#" in line.split("=", 1)[1]:
                comment = "  #" + line.split("#", 1)[1]
            line = f'{key} = "{value}"{comment}'
            done = True
        out.append(line)
    if not done:
        raise KeyError(f"{section}.{key} not found in config.toml")
    CONFIG_PATH.write_text("\n".join(out) + "\n")


def token() -> str:
    """Shared secret between the helper and the extension (handed over via native messaging)."""
    ensure_dirs()
    if not TOKEN_PATH.exists():
        TOKEN_PATH.write_text(secrets.token_urlsafe(32))
        TOKEN_PATH.chmod(0o600)
    return TOKEN_PATH.read_text().strip()
