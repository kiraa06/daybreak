"""Native messaging host: hands the extension the helper's port and token.

Chromium browsers only launch this for the extension ID listed in the host manifest, so the token never has to be typed.
Standard library only — it runs from the manifest's absolute path.
"""
import json
import os
import struct
import sys
from pathlib import Path

if os.environ.get("DAYBREAK_HOME"):
    DATA_DIR = Path(os.environ["DAYBREAK_HOME"])
elif sys.platform == "darwin":
    DATA_DIR = Path.home() / "Library/Application Support/Daybreak"
elif sys.platform.startswith("win"):
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Daybreak"
else:
    DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "daybreak"


def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    (length,) = struct.unpack("<I", raw)
    return json.loads(sys.stdin.buffer.read(length) or b"{}")


def send(obj) -> None:
    data = json.dumps(obj).encode()
    sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
    sys.stdout.buffer.flush()


def main() -> None:
    msg = read_message() or {}
    if msg.get("type") != "pair":
        send({"error": "unknown request"})
        return
    token_path = DATA_DIR / "token"
    port = 47821
    cfg = DATA_DIR / "config.toml"
    if cfg.exists():
        for line in cfg.read_text().splitlines():
            if line.strip().startswith("port"):
                try:
                    port = int(line.split("=", 1)[1].split("#")[0].strip())
                except ValueError:
                    pass
    if not token_path.exists():
        send({"error": "Daybreak helper has not run yet — start it with `daybreak run`."})
        return
    send({"port": port, "token": token_path.read_text().strip()})


if __name__ == "__main__":
    main()
