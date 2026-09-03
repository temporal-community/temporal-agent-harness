"""
Daytona custom preview proxy for the sandboxed coding agent — path-based routing.

Purpose: sit in front of Daytona's preview endpoint so a stopped CONTAINER
sandbox is woken on demand and the server (relaunched by the snapshot's
supervise.sh entrypoint) is actually listening before we forward. That
start-and-wait gate is what makes a stopped-then-woken sandbox serve traffic
seamlessly, so you can build a web app inside the sandbox via the chat agent and
preview it live.

This is deliberately SELF-CONTAINED to the example: it is its own aiohttp server
and touches nothing in `temporal_agent_harness.web`. It is a teaching skeleton,
not production code — start from the official samples for anything real:
https://github.com/daytonaio/daytona-proxy-samples

Routing is PATH-based (no wildcard DNS, no real domain needed), so it runs as-is
on plain localhost:

    http://localhost:8080/s/<sandboxId>/<port>/<path...>?<query>

The `<sandboxId>` is the value of $DAYTONA_SANDBOX_ID inside the sandbox — the
chat agent reads it (`echo "$DAYTONA_SANDBOX_ID"`) and hands you the URL. The
tradeoff of path routing: apps that reference absolute root paths (e.g.
`/assets/app.js`) will miss the `/s/<id>/<port>/` prefix. For the demo, prefer
relative asset paths or a `<base href>`; a production proxy would rewrite them.

Deps:  run via the justfile (`just preview-proxy`), which injects aiohttp with uv.
Env:   DAYTONA_API_KEY (required); DAYTONA_TARGET (optional region, e.g. "us");
       PREVIEW_PROXY_PORT (optional, default 8080).
"""

import asyncio
import os

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from yarl import URL

from daytona import AsyncDaytona, DaytonaConfig, SandboxState


def _make_daytona() -> AsyncDaytona:
    # One shared client for the whole process. The async client opens a single
    # state-streaming websocket that all sandboxes share, so do NOT construct one
    # per request — build it once and close it on shutdown.
    kwargs = {"api_key": os.environ["DAYTONA_API_KEY"]}
    target = os.environ.get("DAYTONA_TARGET")  # e.g. "us" — omit to use org default
    if target:
        kwargs["target"] = target
    return AsyncDaytona(DaytonaConfig(**kwargs))


daytona = _make_daytona()

# Headers that must not be blindly copied through a proxy.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# Reserved Daytona ports to NEVER route to unless you mean it:
#   22222 = web terminal, 2280 = toolbox, 33333 = recording dashboard.
RESERVED_PORTS = {22222, 2280, 33333}

# Idle cost cap. Without this, a woken sandbox keeps billing for compute until
# something else stops it — and preview traffic alone never will. So the proxy
# tells Daytona to auto-stop the sandbox after this many idle minutes. This is a
# PROXY-scoped concern on purpose: the harness that CREATES the sandbox is left
# untouched. Set 0 to leave the sandbox's auto-stop as-is (don't manage it).
AUTO_STOP_MINUTES = int(os.environ.get("PREVIEW_AUTO_STOP_MINUTES", "3"))

# Sandbox ids we've already applied AUTO_STOP_MINUTES to. Daytona persists the
# setting on the sandbox, so once per id is enough; setting it on every request
# would add a needless API round-trip. A restarted proxy just re-applies on the
# first request it sees for each sandbox.
_autostop_configured: set[str] = set()


# ---------------------------------------------------------------------------
# 1. Readiness gate: after the sandbox reports "started", the server inside it
#    still needs a moment to bind the port. Poll until it answers, or we 503.
#    We probe THROUGH Daytona's preview URL so we test the real path.
# ---------------------------------------------------------------------------
async def wait_for_server(
    session: ClientSession, url: str, token: str, timeout: float = 30.0
) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            async with session.head(
                url,
                headers={"x-daytona-preview-token": token},
                allow_redirects=False,
            ) as resp:
                # Any HTTP response means something is listening. Tune if your
                # app returns e.g. 404 at "/" — you just want "not refused".
                if resp.status < 500 or resp.status == 404:
                    return
        except Exception:
            pass  # connection refused / not up yet — keep waiting
        await asyncio.sleep(0.5)
    raise RuntimeError("server did not become ready in time")


# ---------------------------------------------------------------------------
# 2. Wake the sandbox if needed, then get a FRESH preview link + token.
#    Order matters: a standard preview token is invalidated when the sandbox
#    restarts, so a token cached from before the stop is dead. Fetch it AFTER
#    start(). get_preview_link also auto-opens the port if it's closed.
# ---------------------------------------------------------------------------
async def ensure_ready(sandbox_id: str, port: int, session: ClientSession):
    sandbox = await daytona.get(sandbox_id)
    if sandbox.state != SandboxState.STARTED:
        # This is where the snapshot entrypoint supervisor (supervise.sh) re-runs
        # /home/daytona/project/start.sh and relaunches the server. start() waits until
        # the sandbox itself is "started" (not until the server binds).
        await sandbox.start()
    # Cap the compute bill: auto-stop after AUTO_STOP_MINUTES of no SDK activity.
    # Daytona counts SDK interactions (state changes, process.exec, etc.) as
    # activity but NOT preview HTTP traffic — so the agent's own tool calls keep an
    # active chat turn alive, while a sandbox left idle (e.g. a preview tab open
    # but quiet) stops itself. A later request just wakes it again (brief
    # "warming up"). Set once per id; Daytona persists it.
    if AUTO_STOP_MINUTES and sandbox_id not in _autostop_configured:
        await sandbox.set_autostop_interval(AUTO_STOP_MINUTES)
        _autostop_configured.add(sandbox_id)
    preview = await sandbox.get_preview_link(port)   # -> .url, .token
    await wait_for_server(session, preview.url, preview.token)
    return preview


# ---------------------------------------------------------------------------
# 3. Routing: /s/<sandboxId>/<port>/<tail...>. The aiohttp route captures the
#    three parts for us; this just validates the port and rebuilds the upstream
#    path from <tail> (everything after the prefix).
# ---------------------------------------------------------------------------
def upstream_url(preview_url: str, tail: str, query: "URL") -> URL:
    base = URL(preview_url)
    return (base.origin() / tail.lstrip("/")).with_query(query)


# ---------------------------------------------------------------------------
# 4. WebSocket passthrough (HMR, live reload). Daytona auto-detects the upgrade
#    and skips its warning page; we just bridge client <-> upstream and carry
#    the token + forwarded host.
# ---------------------------------------------------------------------------
async def proxy_ws(request, sandbox_id, port, tail, session):
    try:
        preview = await ensure_ready(sandbox_id, port, session)
    except Exception:
        return web.Response(status=503, text="Warming up…")

    client_ws = web.WebSocketResponse()
    await client_ws.prepare(request)

    up_url = upstream_url(preview.url, tail, request.rel_url.query)
    up_url = up_url.with_scheme("wss" if up_url.scheme == "https" else "ws")

    async with session.ws_connect(
        str(up_url),
        headers={
            "X-Daytona-Preview-Token": preview.token,
            "X-Forwarded-Host": request.headers.get("Host", ""),
        },
    ) as upstream_ws:

        async def client_to_upstream():
            async for msg in client_ws:
                if msg.type == WSMsgType.TEXT:
                    await upstream_ws.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await upstream_ws.send_bytes(msg.data)

        async def upstream_to_client():
            async for msg in upstream_ws:
                if msg.type == WSMsgType.TEXT:
                    await client_ws.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await client_ws.send_bytes(msg.data)

        await asyncio.gather(client_to_upstream(), upstream_to_client())

    return client_ws


# ---------------------------------------------------------------------------
# 5. Main handler: parse route -> (ws?) -> ensure ready -> forward.
# ---------------------------------------------------------------------------
async def handler(request: web.Request):
    # 5a. YOUR auth would go here. This demo proxy is intentionally open.
    sandbox_id = request.match_info["sandbox_id"]
    port = int(request.match_info["port"])
    tail = request.match_info.get("tail", "")
    session: ClientSession = request.app["session"]

    if port in RESERVED_PORTS:
        return web.Response(status=403, text=f"Port {port} is reserved by Daytona.")

    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_ws(request, sandbox_id, port, tail, session)

    try:
        preview = await ensure_ready(sandbox_id, port, session)
    except Exception as e:
        # Serve a branded "warming up" page instead of a raw 502.
        return web.Response(
            status=503, content_type="text/html",
            text=f"<h1>Warming up…</h1><p>{e}</p>",
        )

    target = upstream_url(preview.url, tail, request.rel_url.query)

    out_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP
    }
    out_headers["X-Forwarded-Host"] = request.headers.get("Host", "")  # required by Daytona
    out_headers["X-Daytona-Preview-Token"] = preview.token             # fresh, post-start
    out_headers["X-Daytona-Skip-Preview-Warning"] = "true"             # we own the UX

    body = await request.read()
    async with session.request(
        request.method, str(target), headers=out_headers, data=body,
        allow_redirects=False,
    ) as upstream:
        resp_headers = {
            k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP
        }
        resp = web.StreamResponse(status=upstream.status, headers=resp_headers)
        await resp.prepare(request)
        async for chunk in upstream.content.iter_any():
            await resp.write(chunk)
        await resp.write_eof()
        return resp


# ---------------------------------------------------------------------------
# 6. A trailing-slash redirect + a plain help page at "/". No sandbox picker
#    (routing is id-in-the-URL by design) — just usage text.
# ---------------------------------------------------------------------------
async def redirect_add_slash(request: web.Request):
    return web.HTTPFound(f"{request.path}/")


async def index(request: web.Request):
    return web.Response(
        content_type="text/html",
        text=(
            "<h1>Sandbox preview proxy</h1>"
            "<p>Open <code>/s/&lt;sandboxId&gt;/&lt;port&gt;/</code>. The chat agent "
            "prints the full URL after it builds a site — it reads the id from "
            "<code>$DAYTONA_SANDBOX_ID</code> inside the sandbox.</p>"
        ),
    )


# ---------------------------------------------------------------------------
# 7. App wiring: shared HTTP session + SDK lifecycle.
# ---------------------------------------------------------------------------
async def on_startup(app):
    app["session"] = ClientSession(timeout=ClientTimeout(total=None))


async def on_cleanup(app):
    await app["session"].close()
    await daytona.close()   # closes the SDK's shared state-streaming websocket


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", index)
    # `/s/<id>/<port>` (no trailing slash) -> redirect so relative paths resolve.
    app.router.add_route("*", r"/s/{sandbox_id}/{port:\d+}", redirect_add_slash)
    app.router.add_route("*", r"/s/{sandbox_id}/{port:\d+}/{tail:.*}", handler)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), port=int(os.environ.get("PREVIEW_PROXY_PORT", "8080")))
