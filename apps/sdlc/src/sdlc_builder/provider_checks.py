"""Explicit, ephemeral diagnostics using a synthetic request, never repository data."""

import asyncio
import json
import os
import re
import time
from typing import Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import BaseModel, Field

from .failures import causes, model_failure
from .integrations import integration_for
from .models import Profile
from .store import resolve_credential

CHECK_TIMEOUT = 45.0


class ProviderCheck(BaseModel):
    ok: bool = False
    stage: Literal["credential", "request", "response"] = "credential"
    title: str = ""
    explanation: str = ""
    actions: list[str] = Field(default_factory=list)
    endpoint: str = ""
    credential_source: str = ""
    elapsed_ms: int = 0
    http_status: int | None = None
    provider_code: str = ""
    provider_message: str = ""
    parameter: str = ""


def redact(value: str, key: str) -> str:
    # Providers may echo full, JSON-escaped, URL-encoded, or masked credentials.
    if key:
        for variant in {key, json.dumps(key)[1:-1], quote(key, safe="")}:
            value = value.replace(variant, "[redacted]")
    value = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [redacted]", value)
    value = re.sub(r"\bsk-[\w.*…-]+", "[redacted]", value)
    value = re.sub(
        r"(?i)((?:api[_ -]?key|authorization|access[_ -]?token)\s*[=:]\s*)[^\s,;]+",
        r"\1[redacted]",
        value,
    )
    # Error text may include a URL with credentials in its authority or query.
    value = re.sub(r"(https?://)[^\s/]+@", r"\1[redacted]@", value)
    value = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", value)
    return "".join(c for c in value if c.isprintable() or c in "\n\t")[:2000]


def endpoint(profile: Profile, key: str) -> str:
    if profile.provider == "anthropic":
        base = os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
        path = "/v1/messages"
    else:
        base = (
            profile.base_url
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
        path = "/responses" if profile.provider == "openai" else "/chat/completions"
    try:
        url = urlsplit(base)
        host = url.netloc.rsplit("@", 1)[-1]
        return redact(
            urlunsplit((url.scheme, host, url.path.rstrip("/") + path, "", "")), key
        )
    except ValueError:
        return "Invalid provider URL"


def error_details(error: Exception, key: str) -> dict[str, str]:
    # Unlike task failures, this call only contains our fixed test prompt. Return
    # selected redacted fields to the authenticated caller; never store or log them.
    for cause in causes(error):
        body = getattr(cause, "body", None)
        if not isinstance(body, dict):
            continue
        body = body.get("error", body)
        if not isinstance(body, dict):
            continue
        return {
            target: redact(body[source], key)
            for target, source in (
                ("provider_message", "message"),
                ("parameter", "param"),
            )
            if isinstance(body.get(source), str)
        }
    return {}


async def check_profile(profile: Profile) -> ProviderCheck:
    started = time.monotonic()
    result = ProviderCheck(
        credential_source=(
            "Environment variable " + profile.credential.removeprefix("env:")
            if profile.credential.startswith("env:")
            else "OS keyring"
        )
    )
    key = ""
    try:
        async with asyncio.timeout(CHECK_TIMEOUT):
            key = await asyncio.to_thread(resolve_credential, profile.credential)
            result.stage = "request"
            result.endpoint = endpoint(profile, key)
            await integration_for(profile).probe(
                profile,
                key,
                json.dumps(
                    {
                        "mode": "ask",
                        "request": "Connection check only. Do not inspect any repository. Return a finish action with summary 'Connection ready'.",
                        "step": 1,
                        "budget": profile.max_steps,
                        "history": [],
                        "continuation_context": "",
                    }
                ),
            )
            result.ok = True
            result.stage = "response"
            result.title = "Connection and agent response validated"
            result.explanation = "The saved credential and model completed a harmless native tool call, and the response passed the coding agent's structured role validation."
            result.actions = [
                "Select this profile for a new task, or Continue your existing task."
            ]
    except Exception as error:
        failure = model_failure(
            error, resolving_credential=result.stage == "credential"
        )
        result.title = failure.title
        result.explanation = failure.explanation
        result.http_status = failure.http_status
        result.provider_code = failure.provider_code
        # Task-recovery instructions do not apply to a connection check.
        result.actions = [
            failure.actions[0],
            "After correcting the issue, select Test connection again. Testing does not restart any task.",
        ]
        if failure.category == "invalid_response":
            result.stage = "response"
            result.actions[0] = (
                "The provider responded, but its output did not match the agent's action schema. Retry once; if it repeats, check that the model supports structured output."
            )
        elif failure.category == "request_rejected":
            result.actions[0] = (
                "Read the provider message and rejected parameter below. If they identify the app's action schema or request format, that needs an app fix; changing the API key will not fix the format."
            )
        elif failure.category == "provider_error":
            result.actions[0] = (
                "Check the endpoint and provider details below. If no details were returned, report this result with the profile's provider and model."
            )
        elif failure.category == "credential_missing" and profile.credential.startswith(
            "env:"
        ):
            name = profile.credential.removeprefix("env:")
            result.title = f"{name} is not set in the app's environment"
            result.explanation = "The running app could not read this variable. No provider request was sent."
            result.actions[0] = (
                f'In the terminal that launches boltzmann, export {name}="your-api-key", then restart ./apps/sdlc/dev from the repository root. Keep the profile’s API key field empty and its environment variable set to {name}.'
            )
        if result.stage != "credential":
            for field, value in error_details(error, key).items():
                setattr(result, field, value)
        if not result.provider_message and result.http_status:
            result.explanation += (
                " The provider did not supply a readable error message."
            )
    result.elapsed_ms = round((time.monotonic() - started) * 1000)
    return result
