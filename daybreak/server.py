"""Local HTTP API for the browser extension (127.0.0.1 only, token-authenticated)."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import web

from . import __version__, artifacts, config
from .app import App

log = logging.getLogger("daybreak.server")

PUBLIC = ("/api/health",)


EXTENSION_DIR = Path(__file__).resolve().parent.parent / "extension"

DEV_STUB = """<script>
// Dev preview only: a stand-in for the extension APIs so pages render outside the browser extension.
const P = {port: %(port)d, token: %(token)s};
window.chrome = {
  storage: { local: { get: async () => ({ pairing: P }), set: async () => {}, remove: async () => {} },
             session: { get: async () => ({}), set: async () => {} }, onChanged: { addListener() {} } },
  runtime: { onMessage: { addListener() {} }, sendMessage: async () => false, getURL: (p) => "/dev/" + p,
             openOptionsPage() { window.open("/dev/options.html"); }, sendNativeMessage: async () => P,
             getManifest: () => ({ version: "dev" }) },
  tabs: { create: ({ url }) => window.open(url || "/dev/newtab.html"), query: async () => [],
          onActivated: { addListener() {} }, onUpdated: { addListener() {} } },
  windows: { getLastFocused: async () => ({ id: 1 }), create: async ({ url }) => window.open(url) },
  scripting: { executeScript: async () => [{ result: null }] },
};
</script>"""


def make_app(app: App) -> web.Application:
    token = config.token()
    allowed_hosts = {f"127.0.0.1:{app.cfg.port}", f"localhost:{app.cfg.port}"}
    dev = bool(os.environ.get("DAYBREAK_DEV"))
    allowed_origins = {o.rstrip("/") for o in app.cfg.extension_origins}
    if dev:
        allowed_origins |= {f"http://{h}" for h in allowed_hosts}

    @web.middleware
    async def guard(request: web.Request, handler):
        if request.host not in allowed_hosts:  # DNS-rebinding protection
            raise web.HTTPForbidden(text="bad host")
        path = request.path
        if path.startswith("/api/") and path not in PUBLIC:
            origin = request.headers.get("Origin")
            if origin and origin not in allowed_origins:
                raise web.HTTPForbidden(text="bad origin")
            if not hmac.compare_digest(request.headers.get("X-Daybreak-Token", ""), token):
                raise web.HTTPUnauthorized(text="missing or wrong token")
        return await handler(request)

    routes = web.RouteTableDef()

    def ok(data=None, **kw):
        return web.json_response(data if data is not None else kw, dumps=lambda o: json.dumps(o, default=str))

    @routes.get("/api/health")
    async def health(_r):
        return ok(ok=True, app="daybreak", version=__version__)

    @routes.post("/api/hello")
    async def hello(r):
        info = await r.json() if r.can_read_body else {}
        app.bridge.touch(info)
        return ok(app.state())

    @routes.get("/api/state")
    async def state(_r):
        return ok(app.state())

    @routes.get("/api/brief")
    async def get_brief(r):
        day = r.query.get("day") or app.cfg.now().date().isoformat()
        data = app.brief.get(day)
        if not data:
            last = app.db.one("SELECT day FROM briefs ORDER BY day DESC LIMIT 1")
            return ok(missing=True, day=day, last_day=last["day"] if last else None, state=app.state())
        return ok(data)

    @routes.post("/api/brief/generate")
    async def gen_brief(_r):
        app.db.put("brief_error", None)
        asyncio.create_task(app.make_brief("manual"))
        return ok(started=True)

    @routes.get("/api/painting/{name}")
    async def painting(r):
        name = Path(r.match_info["name"]).name
        path = config.PAINTINGS_DIR / name
        if not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})

    @routes.post("/api/brief/focus")
    async def brief_focus(r):
        body = await r.json()
        app.brief.set_focus(body.get("day") or app.cfg.now().date().isoformat(), body.get("option") or {})
        return ok(ok=True)

    @routes.post("/api/todos/{id}")
    async def todo(r):
        body = await r.json()
        app.db.x("UPDATE todos SET done = ? WHERE id = ?", (1 if body.get("done") else 0, r.match_info["id"]))
        return ok(ok=True)

    @routes.get("/api/meetings")
    async def meetings(r):
        return ok(app.meetings.upcoming(hours=float(r.query.get("hours", 36))))

    @routes.post("/api/meetings/{id}/prep")
    async def prep(r):
        mid = r.match_info["id"]
        aid = await app.meetings.prepare(mid)
        return ok(url=artifacts.url_for(app.cfg, aid) if aid else "", ok=bool(aid))

    @routes.get("/api/nudges")
    async def nudges(_r):
        app.bridge.touch()
        now = time.time()
        rows = app.db.all("SELECT * FROM nudges WHERE deliver_at <= ? AND expires_at > ? AND delivered_at IS NULL "
                          "AND dismissed_at IS NULL ORDER BY deliver_at LIMIT 5", (now, now))
        for n in rows:
            app.db.x("UPDATE nudges SET delivered_at = ? WHERE id = ?", (now, n["id"]))
            n["payload"] = json.loads(n["payload"] or "{}")
        return ok(rows)

    @routes.post("/api/nudges/{id}")
    async def nudge_action(r):
        body = await r.json()
        if body.get("action") == "dismiss":
            app.db.x("UPDATE nudges SET dismissed_at = ? WHERE id = ?", (time.time(), r.match_info["id"]))
        elif body.get("action") == "shown":
            log.info("pop-up %s shown as %s (browser in front: %s)", r.match_info["id"],
                     " + ".join(map(str, body.get("via") or [])) or "nothing", body.get("in_front"))
        return ok(ok=True)

    @routes.get("/api/workstreams")
    async def workstreams(_r):
        rows = app.db.all("SELECT slug, title, overview, state, open_loops, updated FROM workstreams "
                          "WHERE state != 'dormant' ORDER BY updated DESC")
        for w in rows:
            w["open_loops"] = json.loads(w["open_loops"] or "[]")
            w["log"] = app.db.all("SELECT ts, text FROM workstream_log WHERE slug = ? ORDER BY ts DESC LIMIT 5", (w["slug"],))
        return ok(rows)

    @routes.post("/api/chat")
    async def chat(r):
        body = await r.json()
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(r)
        try:
            async for ev in app.chat.send(body.get("chat_id"), str(body.get("message", ""))[:20000], body.get("page")):
                await resp.write(f"data: {json.dumps(ev)}\n\n".encode())
        except ConnectionResetError:
            pass
        return resp

    @routes.get("/api/chats")
    async def chats(_r):
        return ok(app.db.all("SELECT id, title, updated FROM chats ORDER BY updated DESC LIMIT 30"))

    @routes.get("/api/chats/{id}")
    async def chat_history(r):
        return ok(app.chat.history(r.match_info["id"]))

    @routes.get("/api/schedules")
    async def schedules(_r):
        return ok(app.schedules.list())

    @routes.post("/api/schedules")
    async def add_schedule(r):
        body = await r.json()
        try:
            return ok(app.schedules.create(body["name"], body["prompt"], body["spec"]))
        except (KeyError, ValueError) as e:
            return web.json_response({"error": str(e)}, status=400)

    @routes.delete("/api/schedules/{id}")
    async def del_schedule(r):
        return ok(ok=app.schedules.delete(r.match_info["id"]))

    @routes.get("/api/artifacts")
    async def list_artifacts(_r):
        rows = app.db.all("SELECT id, kind, title, created FROM artifacts ORDER BY created DESC LIMIT 50")
        for a in rows:
            a["url"] = artifacts.url_for(app.cfg, a["id"])
        return ok(rows)

    @routes.post("/api/sense")
    async def sense(_r):
        asyncio.create_task(app.sense_cycle("manual"))
        return ok(started=True)

    @routes.post("/api/bridge/next")
    async def bridge_next(r):
        app.bridge.touch()
        return ok(app.bridge.claim() or {})

    @routes.post("/api/bridge/result")
    async def bridge_result(r):
        body = await r.json()
        app.bridge.complete(str(body.get("id")), body.get("result") or {"error": "empty result"})
        return ok(ok=True)

    @routes.get("/api/settings")
    async def get_settings(_r):
        c = app.cfg
        return ok(name=c.name, brief_time="%02d:%02d" % c.brief_time, slack=c.slack, slack_workspace=c.slack_workspace,
                  config_path=str(config.CONFIG_PATH))

    @routes.post("/api/settings")
    async def set_settings(r):
        body = await r.json()
        mapping = {"name": ("user", "name"), "brief_time": ("schedule", "brief_time"),
                   "slack": ("sources", "slack"), "slack_workspace": ("sources", "slack_workspace")}
        for key, (section, field) in mapping.items():
            if key in body:
                config.set_value(section, field, str(body[key]).strip())
        fresh = config.load()
        app.cfg.__dict__.update(fresh.__dict__)  # every component holds the same Config object
        return ok(ok=True)

    @routes.get("/a/{id}/{sig}")
    async def artifact(r):
        aid, sig = r.match_info["id"], r.match_info["sig"]
        if not artifacts.verify(aid, sig):
            raise web.HTTPNotFound()
        row = app.db.one("SELECT path FROM artifacts WHERE id = ?", (aid,))
        if not row or not Path(row["path"]).exists():
            raise web.HTTPNotFound()
        return web.FileResponse(row["path"], headers={
            "Content-Type": "text/html; charset=utf-8",
            "Content-Security-Policy": "default-src 'self' 'unsafe-inline' data: https:; connect-src 'none'"})

    if dev:
        @routes.get("/dev/{path:.*}")
        async def dev_files(r):
            rel = r.match_info["path"] or "newtab.html"
            path = (EXTENSION_DIR / rel).resolve()
            if EXTENSION_DIR not in path.parents or not path.is_file():
                raise web.HTTPNotFound()
            if path.suffix == ".html":
                stub = DEV_STUB % {"port": app.cfg.port, "token": json.dumps(token)}
                return web.Response(text=path.read_text().replace("<head>", "<head>" + stub, 1), content_type="text/html")
            return web.FileResponse(path)

    web_app = web.Application(middlewares=[guard], client_max_size=8 * 1024 * 1024)
    web_app.add_routes(routes)

    async def on_startup(_a):
        if not os.environ.get("DAYBREAK_NO_LOOPS"):  # demo renders and UI work don't need background loops
            app.start()

    web_app.on_startup.append(on_startup)
    return web_app


def serve() -> None:
    app = App()
    log.info("Daybreak %s listening on http://127.0.0.1:%s", __version__, app.cfg.port)
    web.run_app(make_app(app), host="127.0.0.1", port=app.cfg.port, print=None, access_log=None)
