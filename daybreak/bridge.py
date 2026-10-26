"""Jobs the extension runs inside the browser on the helper's behalf.

The extension polls /api/bridge/next, performs the job with the user's browser session (e.g. read Slack through the
Slack web session already signed in to the browser) and posts the result back. Session tokens never leave the browser.
"""
from __future__ import annotations

import asyncio
import json
import time

from .db import DB, new_id

EXTENSION_ONLINE_SECONDS = 180


class Bridge:
    def __init__(self, db: DB):
        self.db = db
        self.waiters: dict[str, asyncio.Future] = {}

    def touch(self, info: dict | None = None) -> None:
        self.db.put("extension_seen", time.time())
        if info:
            self.db.put("extension_info", info)

    def extension_online(self) -> bool:
        return time.time() - (self.db.get("extension_seen", 0) or 0) < EXTENSION_ONLINE_SECONDS

    async def request(self, kind: str, params: dict, timeout: float = 120) -> dict | None:
        job_id = new_id()
        self.db.x("INSERT INTO bridge_jobs(id, kind, params, created) VALUES(?,?,?,?)",
                  (job_id, kind, json.dumps(params), time.time()))
        fut = asyncio.get_running_loop().create_future()
        self.waiters[job_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.db.x("UPDATE bridge_jobs SET status = 'failed', finished = ? WHERE id = ? AND status != 'done'",
                      (time.time(), job_id))
            return None
        finally:
            self.waiters.pop(job_id, None)

    def claim(self) -> dict | None:
        # Jobs older than 5 minutes are stale; nobody is waiting for them any more.
        self.db.x("UPDATE bridge_jobs SET status = 'failed' WHERE status = 'queued' AND created < ?", (time.time() - 300,))
        row = self.db.one("SELECT * FROM bridge_jobs WHERE status = 'queued' ORDER BY created LIMIT 1")
        if not row:
            return None
        self.db.x("UPDATE bridge_jobs SET status = 'claimed', claimed = ? WHERE id = ?", (time.time(), row["id"]))
        return {"id": row["id"], "kind": row["kind"], "params": json.loads(row["params"] or "{}")}

    def complete(self, job_id: str, result: dict) -> None:
        status = "failed" if result.get("error") else "done"
        self.db.x("UPDATE bridge_jobs SET status = ?, result = ?, finished = ? WHERE id = ?",
                  (status, json.dumps(result)[:200000], time.time(), job_id))
        fut = self.waiters.get(job_id)
        if fut and not fut.done():
            fut.set_result(result)
