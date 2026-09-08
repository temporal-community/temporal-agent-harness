"""Web fetch tool.

For docs and error messages the repository does not contain, the agent can pull a URL's
text. It does network I/O, so it is an activity (it runs on the worker, off the workflow).
It is not marked inherently safe: fetching an arbitrary URL from the worker is a real
capability (think SSRF), so under a gating policy it should be approved; a production build
would add a domain allowlist.

NB: no ``from __future__ import annotations``; the annotations build the model-facing schema
at runtime.
"""

from datetime import timedelta

from temporalio.workflow import ActivityConfig

from temporal_agent_harness.harness import agent

_MAX_CHARS = 20_000


async def fetch_text(url: str) -> str:
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        resp = await client.get(url, headers={"User-Agent": "portable-coding-agent"})
        resp.raise_for_status()
        text = resp.text
    if len(text) > _MAX_CHARS:
        return text[:_MAX_CHARS] + "\n...[truncated]"
    return text


@agent.activity_tool_defn(
    activity_config=ActivityConfig(start_to_close_timeout=timedelta(seconds=30)),
)
async def web_fetch(url: str) -> str:
    """Fetch a web page or raw file over HTTP(S) and return its text, for documentation or error
    messages the repository does not contain. Returns the response body (truncated if very large).
    Prefer the repository itself for anything already in it; use this for external references."""
    return await fetch_text(url)


WEB_TOOLS = [web_fetch]
