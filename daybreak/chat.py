"""Side-panel chat: a Claude session with the user's connectors plus Daybreak's own tools."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

from claude_agent_sdk import ClaudeSDKClient, create_sdk_mcp_server, tool

from . import artifacts, prompts
from .claude import Claude, stream_text
from .db import new_id

log = logging.getLogger("daybreak.chat")

IDLE_SECONDS = 15 * 60


def _text(obj) -> dict:
    return {"content": [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)}]}


def build_tools(app) -> dict:
    """In-process MCP tools exposed to chat as mcp__daybreak__*."""
    db, cfg = app.db, app.cfg

    @tool("get_current_focus", "The user's live workstreams (overview, state, open loops, recent log), today's surfaced "
          "set and how workstreams relate. Call whenever an answer depends on their current work or priorities.", {})
    async def get_current_focus(_args):
        day = cfg.now().date().isoformat()
        surface = json.loads((db.one("SELECT slugs FROM surface WHERE day = ?", (day,)) or {"slugs": "[]"})["slugs"])
        rows = db.all("SELECT * FROM workstreams WHERE state IN ('active','waiting') ORDER BY updated DESC LIMIT 25")
        out = []
        for w in rows:
            logs = db.all("SELECT text FROM workstream_log WHERE slug = ? ORDER BY ts DESC LIMIT 3", (w["slug"],))
            members = db.all("SELECT i.ref, i.title, i.url, i.state FROM items i JOIN memberships m ON m.ref = i.ref "
                             "WHERE m.slug = ? ORDER BY i.updated DESC LIMIT 8", (w["slug"],))
            out.append({"slug": w["slug"], "title": w["title"], "state": w["state"], "surfaced_today": w["slug"] in surface,
                        "overview": w["overview"], "open_loops": json.loads(w["open_loops"] or "[]"),
                        "recent": [l["text"] for l in logs], "items": members})
        edges = db.all("SELECT src, dst, kind FROM edges WHERE strength >= 0.75")
        return _text({"workstreams": out, "edges": edges} if out else
                     "No workstreams yet — Daybreak is still reading the connected sources.")

    @tool("list_meetings", "The user's meetings for the next N hours (default 36), with attendees, join links and "
          "prep-note links when ready.", {"hours": int})
    async def list_meetings(args):
        rows = app.meetings.upcoming(hours=float(args.get("hours") or 36))
        return _text([{k: r[k] for k in ("id", "title", "time", "attendees", "join_url", "prep_url", "description")}
                      for r in rows] or "No meetings found (is Google Calendar connected?).")

    @tool("create_artifact", "Save a complete self-contained HTML document (report, deck, recap, one-pager, prep doc) and "
          "get a link the user can open. kind is one of report, deck, recap, prep.",
          {"kind": str, "title": str, "html": str})
    async def create_artifact(args):
        html = str(args.get("html", ""))
        if "<html" not in html.lower():
            html = artifacts.wrap_page(args.get("title", "Untitled"), str(args.get("kind", "report")).title(),
                                       artifacts.sanitize_fragment(html))
        aid = artifacts.save(db, str(args.get("kind", "report")), str(args.get("title", "Untitled")), html)
        return _text({"url": artifacts.url_for(cfg, aid), "id": aid})

    @tool("schedule_prompt", "Create a recurring or future task. Only when the user explicitly asks. spec examples: "
          "'daily 09:00', 'weekdays 17:30', 'mon,thu 10:00', 'every 2h', 'once 2026-11-02 09:00'.",
          {"name": str, "prompt": str, "spec": str})
    async def schedule_prompt(args):
        try:
            return _text(app.schedules.create(args["name"], args["prompt"], args["spec"]))
        except (KeyError, ValueError) as e:
            return _text(f"Could not schedule: {e}")

    @tool("list_schedules", "List the user's scheduled tasks.", {})
    async def list_schedules(_args):
        return _text([{k: r[k] for k in ("id", "name", "spec", "next_run_text", "last_status")} for r in app.schedules.list()]
                     or "No scheduled tasks.")

    @tool("delete_schedule", "Delete a scheduled task by id, only when the user asks.", {"id": str})
    async def delete_schedule(args):
        return _text("Deleted." if app.schedules.delete(str(args.get("id"))) else "No task with that id.")

    server = create_sdk_mcp_server(name="daybreak", tools=[get_current_focus, list_meetings, create_artifact,
                                                           schedule_prompt, list_schedules, delete_schedule])
    return {"daybreak": server}


class ChatManager:
    def __init__(self, app):
        self.app = app
        self.sessions: dict[str, dict] = {}
        self.tools = build_tools(app)

    def _system(self) -> str:
        cfg = self.app.cfg
        return prompts.fill(prompts.CHAT_SYSTEM, me=self.app.me, now=cfg.now().strftime("%A %-d %B %Y, %H:%M"),
                            tz=str(cfg.tz))

    async def _client(self, chat_id: str) -> dict:
        s = self.sessions.get(chat_id)
        if s and time.time() - s["used"] < IDLE_SECONDS:
            return s
        if s:
            await self._close(chat_id)
        row = self.app.db.one("SELECT session_id FROM chats WHERE id = ?", (chat_id,))
        opts = self.app.claude.options(system=self._system(), connectors=True, max_turns=40, allow_drafts=True,
                                       sdk_servers=self.tools, partial=True, resume=(row or {}).get("session_id") or None)
        client = ClaudeSDKClient(options=opts)
        await client.connect()
        await self.app.claude.wait_for_connectors(client, timeout=12)
        s = {"client": client, "used": time.time(), "lock": asyncio.Lock()}
        self.sessions[chat_id] = s
        return s

    async def _close(self, chat_id: str) -> None:
        s = self.sessions.pop(chat_id, None)
        if s:
            try:
                await s["client"].disconnect()
            except Exception:
                pass

    async def reap(self) -> None:
        for cid, s in list(self.sessions.items()):
            if time.time() - s["used"] > IDLE_SECONDS and not s["lock"].locked():
                await self._close(cid)

    async def send(self, chat_id: str | None, message: str, page: dict | None) -> AsyncIterator[dict]:
        db = self.app.db
        now = time.time()
        if not chat_id or not db.one("SELECT 1 FROM chats WHERE id = ?", (chat_id,)):
            chat_id = chat_id or new_id()
            db.x("INSERT INTO chats(id, title, created, updated) VALUES(?,?,?,?)", (chat_id, message[:80], now, now))
        yield {"type": "chat", "chat_id": chat_id}
        db.x("INSERT INTO chat_messages(chat_id, role, content, meta, created) VALUES(?,?,?,?,?)",
             (chat_id, "user", message, json.dumps({"page": {k: (page or {}).get(k) for k in ("url", "title")}}), now))

        prompt = message
        if page and (page.get("text") or page.get("selection")):
            sel = f"\n<selection>\n{page['selection'][:4000]}\n</selection>" if page.get("selection") else ""
            prompt = (f"<current_page url=\"{page.get('url', '')}\" title=\"{page.get('title', '')}\">\n"
                      f"{(page.get('text') or '')[:14000]}\n</current_page>{sel}\n\n{message}")

        s = await self._client(chat_id)
        answer, tools_used = [], []
        async with s["lock"]:
            s["used"] = time.time()
            try:
                await s["client"].query(prompt)
                async for ev in stream_text(s["client"]):
                    if ev["type"] == "text":
                        answer.append(ev["text"])
                    elif ev["type"] == "tool":
                        tools_used.append(ev["name"])
                    elif ev["type"] == "done":
                        if ev.get("session_id"):
                            db.x("UPDATE chats SET session_id = ?, updated = ? WHERE id = ?",
                                 (ev["session_id"], time.time(), chat_id))
                        if not answer and ev.get("result"):
                            answer.append(ev["result"])
                            yield {"type": "text", "text": ev["result"]}
                        continue
                    yield ev
            except Exception as e:
                log.exception("chat failed")
                await self._close(chat_id)
                yield {"type": "error", "text": f"Chat failed: {e}"}
        text = "".join(answer)
        db.x("INSERT INTO chat_messages(chat_id, role, content, meta, created) VALUES(?,?,?,?,?)",
             (chat_id, "assistant", text, json.dumps({"tools": tools_used}), time.time()))
        yield {"type": "done", "chat_id": chat_id}
        asyncio.create_task(self._note(chat_id, message, text))

    async def _note(self, chat_id: str, question: str, answer: str) -> None:
        """Feed work-relevant chat intent into the ledger, so assistant chats count as work signals too."""
        try:
            out = await self.app.claude.complete_json(
                "You write terse work notes. Output JSON only.",
                prompts.fill(prompts.CHAT_NOTE, me=self.app.me, question=question[:2000], answer=answer[:2500]),
                fast=True, label="chat-note")
        except Exception:
            return
        if isinstance(out, dict) and out.get("relevant"):
            self.app.sensors.ingest("chat", [{"ref": f"chat:{chat_id}", "title": out.get("title", "Chat"), "kind": "context",
                                              "author": "me", "target": "", "summary": out.get("summary", ""),
                                              "state": "asked assistant"}])

    def history(self, chat_id: str) -> list[dict]:
        return self.app.db.all("SELECT role, content, created FROM chat_messages WHERE chat_id = ? ORDER BY id", (chat_id,))
