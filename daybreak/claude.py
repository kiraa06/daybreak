"""Runs Claude through the Agent SDK on the user's Claude subscription.

Two modes:
- plain:      no tools, no settings — fast text/JSON transforms (routing, synthesis, briefs).
- connectors: loads the user's Claude Code config so the claude.ai connectors (Gmail, Drive,
              Atlassian, Calendar, Slack...) are available, with hooks and plugins switched off
              and a permission gate that only lets read-only connector tools run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from .config import Config

log = logging.getLogger("daybreak.claude")

_HOST_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_AGENT_SDK")
_HOST_NAMES = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_PREVIEW_CLASSIFIER_FLOOR",
               "__CFBundleIdentifier", "AI_AGENT", "BAGGAGE"}


def scrub_host_env() -> list[str]:
    """Drop host-injected Claude/Anthropic variables so the CLI uses the subscription login.

    The Python SDK merges os.environ into the CLI's environment, so this must edit os.environ."""
    removed = [k for k in list(os.environ)
               if k != "CLAUDE_CODE_OAUTH_TOKEN" and (k.startswith(_HOST_PREFIXES) or k in _HOST_NAMES)]
    for k in removed:
        os.environ.pop(k)
    return removed


# --- permission gate ---------------------------------------------------------

_READ_VERBS = {"search", "get", "list", "read", "fetch", "find", "lookup", "query", "download",
               "retrieve", "view", "show", "describe", "suggest", "check"}
_DRAFT_TOOLS = {"create_draft", "update_draft"}  # chat may draft email replies, never send


def _first_word(action: str) -> str:
    action = re.sub(r"([a-z])([A-Z])", r"\1_\2", action)
    return action.split("_")[0].lower()


def make_gate(*, allow_drafts: bool = False, extra_allowed: tuple[str, ...] = ()) -> Callable:
    async def gate(name: str, _input: dict, _ctx) -> PermissionResultAllow | PermissionResultDeny:
        if name in ("ToolSearch",) or name.startswith(extra_allowed or ("\0",)):
            return PermissionResultAllow()
        if name.startswith("mcp__claude_ai_"):
            action = name.split("__", 2)[-1]
            if _first_word(action) in _READ_VERBS or action.endswith(("UserInfo", "Resources")):
                return PermissionResultAllow()
            if allow_drafts and action in _DRAFT_TOOLS:
                return PermissionResultAllow()
        return PermissionResultDeny(message="Daybreak only allows read-only connector tools here.")
    return gate


def _enabled_plugins() -> list[str]:
    names: set[str] = set()
    for p in (Path.home() / ".claude/settings.json", Path.home() / ".claude/settings.local.json"):
        try:
            names |= set((json.loads(p.read_text()).get("enabledPlugins") or {}).keys())
        except Exception:
            pass
    return sorted(names)


# --- JSON helpers --------------------------------------------------------------

def parse_json(text: str) -> Any:
    """Parse a JSON object/array from model output, tolerating code fences and stray prose."""
    t = text.strip()
    t = re.sub(r"^```(?:json|jsonl)?\s*|\s*```$", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if starts:
        start = min(starts)
        end = max(t.rfind("}"), t.rfind("]"))
        if end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                pass
    raise ValueError(f"model did not return JSON: {text[:300]!r}")


def parse_jsonl(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip().strip(",")
        if line.startswith("{") and line.endswith("}"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# --- runner --------------------------------------------------------------------

class Claude:
    def __init__(self, cfg: Config, *, concurrency: int = 3):
        self.cfg = cfg
        self.sem = asyncio.Semaphore(concurrency)
        self.plugins = _enabled_plugins()
        self.stats = {"calls": 0, "errors": 0, "seconds": 0.0}

    def options(self, *, system: str, connectors: bool, max_turns: int = 1, model: str | None = None,
                allow_drafts: bool = False, sdk_servers: dict | None = None, partial: bool = False,
                resume: str | None = None) -> ClaudeAgentOptions:
        model = model or self.cfg.model_default or None
        kw: dict[str, Any] = dict(system_prompt=system, max_turns=max_turns, cwd=str(Path.home()),
                                  include_partial_messages=partial, stderr=lambda line: log.debug("cli: %s", line))
        if model:
            kw["model"] = model
        if resume:
            kw["resume"] = resume
        if connectors:
            extra = ("mcp__daybreak__",) if sdk_servers else ()
            kw.update(
                setting_sources=["user"],
                settings=json.dumps({"disableAllHooks": True, "enabledPlugins": {p: False for p in self.plugins}}),
                tools=["ToolSearch"],
                can_use_tool=make_gate(allow_drafts=allow_drafts, extra_allowed=extra),
                permission_mode="default",
            )
            if sdk_servers:
                kw["mcp_servers"] = sdk_servers
        else:
            kw.update(setting_sources=[], tools=[], allowed_tools=[], permission_mode="bypassPermissions")
        return ClaudeAgentOptions(**kw)

    @staticmethod
    async def wait_for_connectors(client: ClaudeSDKClient, timeout: float = 15) -> dict[str, str]:
        """claude.ai connectors connect a few seconds after start; tools appear only once connected."""
        deadline = time.monotonic() + timeout
        status: dict[str, str] = {}
        while time.monotonic() < deadline:
            try:
                st = await client.get_mcp_status()
            except Exception:
                break
            servers = st.get("mcpServers", []) if isinstance(st, dict) else []
            status = {s.get("name", ""): s.get("status", "") for s in servers if s.get("name", "").startswith("claude.ai")}
            if status and "pending" not in status.values():
                break
            await asyncio.sleep(0.7)
        return status

    async def complete(self, system: str, prompt: str, *, connectors: bool = False, max_turns: int | None = None,
                       fast: bool = False, label: str = "", timeout: float = 600) -> str:
        """Run one task to completion and return the final text."""
        model = (self.cfg.model_fast if fast else "") or None
        opts = self.options(system=system, connectors=connectors,
                            max_turns=max_turns or (12 if connectors else 1), model=model)
        async with self.sem:
            t0 = time.monotonic()
            self.stats["calls"] += 1
            signals: list[str] = []
            chunks: list[str] = []
            final, is_error = None, False
            try:
                async with asyncio.timeout(timeout):
                    async with ClaudeSDKClient(options=opts) as client:
                        if connectors:
                            await self.wait_for_connectors(client)
                        await client.query(prompt)
                        async for msg in client.receive_response():
                            if isinstance(msg, SystemMessage) and msg.subtype in ("api_retry", "error", "init_error"):
                                signals.append(f"{msg.subtype}: {msg.data}")
                            elif isinstance(msg, AssistantMessage):
                                for b in msg.content:
                                    if isinstance(b, TextBlock):
                                        chunks.append(b.text)
                                    elif isinstance(b, ToolUseBlock):
                                        log.info("[%s] tool %s", label, b.name)
                            elif isinstance(msg, ResultMessage):
                                final = msg.result or final
                                is_error = bool(msg.is_error)
            except Exception as e:
                self.stats["errors"] += 1
                raise RuntimeError(f"Claude call '{label}' failed: {e}; signals={signals[-3:]}") from e
            finally:
                self.stats["seconds"] += time.monotonic() - t0
            if is_error:
                self.stats["errors"] += 1
                raise RuntimeError(f"Claude call '{label}' returned an error: {final}")
            log.info("[%s] done in %.1fs", label, time.monotonic() - t0)
            return (final or "".join(chunks)).strip()

    async def complete_json(self, system: str, prompt: str, *, jsonl: bool = False, **kw) -> Any:
        text = await self.complete(system, prompt, **kw)
        try:
            return parse_jsonl(text) if jsonl else parse_json(text)
        except ValueError:
            if kw.get("connectors"):
                raise  # don't repeat tool-using work; caller treats as empty
            retry = await self.complete(system, prompt + "\n\nReturn ONLY valid JSON this time — no prose.", **kw)
            return parse_jsonl(retry) if jsonl else parse_json(retry)


async def stream_text(client: ClaudeSDKClient) -> AsyncIterator[dict]:
    """Yield {"type": "text"|"tool"|"done"|"error", ...} events from one response with partial messages on."""
    async for msg in client.receive_response():
        if isinstance(msg, StreamEvent):
            ev = msg.event or {}
            if ev.get("type") == "content_block_delta" and (ev.get("delta") or {}).get("type") == "text_delta":
                yield {"type": "text", "text": ev["delta"].get("text", "")}
        elif isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    yield {"type": "tool", "name": b.name}
        elif isinstance(msg, ResultMessage):
            if msg.is_error:
                yield {"type": "error", "text": msg.result or "Claude returned an error"}
            yield {"type": "done", "session_id": msg.session_id, "result": msg.result or ""}
