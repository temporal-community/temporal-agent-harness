"""Local-only API, worker lifetime, packaged harness embed, and state archive."""

import asyncio
import contextlib
import hashlib
import importlib.metadata
import secrets
import shutil
import time
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import (
    SEND_AGENT_MESSAGE_UPDATE,
    AgentConfig,
    AgentMessage,
    AgentMessageReply,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporal_agent_harness.utils.large_payload import local_payload_storage
from temporal_agent_harness.web import (
    AgentDescriptor,
    AgentRegistry,
    create_agent_harness_app,
    create_session_manager_worker,
)
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.envconfig import ClientConfig
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from . import reviews, workspaces
from .activities import decide, workspace_action
from .bounded_code import bounded_resume, bounded_start
from .coding_workflow import WORKFLOW_NAME_V2, CodingRoleWorkflow, CodingWorkflow
from .coding_workspace import coding_operation, project_merge_operation
from .integrations import integration_for, profile_available, registered_integrations
from .models import Control, FollowUpInput, Profile, TaskInput
from .store import Store, data_dir, workspace
from .workflow import TASK_QUEUE, WORKFLOW_NAME, SdlcAgentWorkflow


class NewProject(BaseModel):
    path: str = Field(min_length=1, max_length=4000)


class NewTask(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    project_id: str
    profile_id: str
    prompt: str = Field(min_length=1, max_length=20000)
    mode: str = Field(default="change", pattern="^(ask|change)$")
    parent_task_id: str = ""
    engine: str = Field(default="v2", pattern="^(v1|v2)$")


class SaveProfile(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    provider: str = Field(pattern="^(openai|anthropic|compatible)$")
    model: str = Field(min_length=1, max_length=200)
    base_url: str = ""
    env_var: str = Field(default="", pattern=r"^([A-Za-z_][A-Za-z0-9_]*)?$")
    api_key: str = Field(default="", max_length=4096)
    max_steps: int = Field(default=24, ge=3, le=200)


class FileEdit(BaseModel):
    path: str
    content: str = Field(max_length=100000)
    expected_hash: str


class DeleteTask(BaseModel):
    remove_workspace: bool = False


class NewMessage(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    expected_turn: int = Field(ge=2)
    prompt: str = Field(min_length=1, max_length=20000)
    mode: str = Field(pattern="^(ask|change)$")
    profile_id: str
    max_steps: int = Field(ge=3, le=200)


def create_app() -> FastAPI:
    store = Store()
    token_path = data_dir() / "session-token"
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32))
        token_path.chmod(0o600)
    token = token_path.read_text().strip()
    offload = local_payload_storage(base_dir=data_dir() / "payloads")
    registry = AgentRegistry(
        agents=[
            AgentDescriptor(
                key="sdlc",
                workflow_type=WORKFLOW_NAME,
                task_queue=TASK_QUEUE,
                label="SDLC builder",
                description="Local development controller",
            ),
            AgentDescriptor(
                key="sdlc-v2",
                workflow_type=WORKFLOW_NAME_V2,
                task_queue=TASK_QUEUE,
                label="Coding agent V2",
                description="Native coding tools, Code Mode and independent review",
            ),
            AgentDescriptor(
                key="coding-role-v2",
                workflow_type="SdlcCodingRoleV2",
                task_queue=TASK_QUEUE,
                label="Coding role",
                description="Controller-owned coding assignment",
            ),
        ]
    )
    harness = create_agent_harness_app(
        registry=registry,
        manager_workflow_id="sdlc-session-manager",
        manager_task_queue="sdlc-session-manager",
        large_payload_offload=offload,
    )
    task_locks: dict[str, asyncio.Lock] = {}
    profile_checks: set[str] = set()
    removing_profiles: set[str] = set()

    async def dispatch_followup(app, task):
        pending = task["pending_message"]
        try:
            reply = await app.state.client.get_workflow_handle(
                task["workflow_id"]
            ).execute_update(
                SEND_AGENT_MESSAGE_UPDATE,
                AgentMessage(
                    type="follow_up",
                    payload=pending["payload"],
                    expected_turn=pending["expected_turn"],
                ),
                id="sdlc-message-" + pending["id"],
                result_type=AgentMessageReply,
                rpc_timeout=timedelta(seconds=5),
            )
        except WorkflowUpdateFailedError:
            receipt = {
                "id": pending["id"],
                "status": "rejected",
                "error": "The agent could not accept this message. Refresh the task, wait for its current message to finish, and send again.",
            }
            task["message_error"] = {**receipt, "prompt": pending["payload"]["prompt"]}
        except asyncio.CancelledError:
            raise
        except Exception:
            pending["error"] = (
                "Waiting for Temporal to confirm this message. boltzmann will retry delivery automatically; it will not run the message twice."
            )
            store.put("tasks", task)
            return {"id": pending["id"], "status": "pending"}
        else:
            task["current_profile_id"] = pending["payload"]["profile"]["id"]
            receipt = {
                "id": pending["id"],
                "status": "accepted",
                "turn_number": reply.turn_number,
            }
            task.pop("message_error", None)
        task.setdefault("message_receipts", {})[pending["id"]] = {
            **receipt,
            "request_hash": pending["request_hash"],
        }
        task.pop("pending_message")
        store.put("tasks", task)
        return receipt

    async def reconcile(app):
        while True:
            for listed in store.all("tasks"):
                # Dispatch, deletion, and user controls share a lock. Re-read
                # after acquiring it so a queued dispatch cannot resurrect a
                # task deleted while the collector was working on another task.
                async with task_locks.setdefault(listed["id"], asyncio.Lock()):
                    try:
                        task = store.get("tasks", listed["id"])
                    except ValueError:
                        continue
                    await reconcile_task(app, task)
            await asyncio.sleep(1)

    async def reconcile_task(app, task):
        if task.get("dispatch") == "preparing":
            return
        try:
            if task.get("dispatch") != "submitted":
                await AgentClient(
                    app.state.client, task["workflow_id"]
                ).start_and_submit_message(
                    "build",
                    task["input"],
                    1,
                    workflow_name=WORKFLOW_NAME_V2
                    if task.get("engine") == "v2"
                    else WORKFLOW_NAME,
                    task_queue=TASK_QUEUE,
                    start_config=AgentConfig(),
                    update_id=task["id"],
                )
                task["dispatch"] = "submitted"
                task.pop("dispatch_error", None)
                store.put("tasks", task)
            if task.get("pending_message"):
                await dispatch_followup(app, task)
            state = await app.state.client.get_workflow_handle(
                task["workflow_id"]
            ).query("progress", rpc_timeout=timedelta(seconds=3))
            store.archive(task["id"], state)
            app.state.temporal_ok = True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Keep the last known snapshot. UI marks it stale instead of erasing progress.
            app.state.temporal_ok = False
            if task.get("dispatch") != "submitted":
                task["dispatch_error"] = (
                    f"Waiting to dispatch ({type(error).__name__}). Retrying automatically."
                )
                store.put("tasks", task)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        app.state.temporal_ok = False
        integrations = list(registered_integrations().values())
        client = await Client.connect(
            **ClientConfig.load_client_connect_config(),
            plugins=[
                *[
                    plugin
                    for backend in integrations
                    for plugin in backend.client_plugins()
                ],
                AgentHarnessPlugin(
                    tools=[workspace_action], large_payload_offload=offload
                ),
            ],
        )
        app.state.client = client
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(
                Worker(
                    client,
                    task_queue=TASK_QUEUE,
                    workflows=[SdlcAgentWorkflow, CodingWorkflow, CodingRoleWorkflow],
                    activities=[
                        decide,
                        coding_operation,
                        project_merge_operation,
                        bounded_start,
                        bounded_resume,
                    ],
                    plugins=[
                        plugin
                        for backend in integrations
                        for plugin in backend.worker_plugins()
                    ],
                )
            )
            await stack.enter_async_context(
                create_session_manager_worker(client, task_queue="sdlc-session-manager")
            )
            # Mounted FastAPI applications do not enter their lifespan automatically.
            await stack.enter_async_context(harness.router.lifespan_context(harness))
            app.state.temporal_ok = True
            collector = asyncio.create_task(reconcile(app))
            try:
                yield
            finally:
                collector.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await collector

    app = FastAPI(
        title="boltzmann",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.store = store

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.headers.get("host", "")
        if host.split(":")[0] not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"detail": "Local requests only"}, status_code=403)
        path = request.url.path
        if request.method not in {"GET", "HEAD"}:
            origin = request.headers.get("origin")
            if origin and origin != f"http://{host}":
                return JSONResponse({"detail": "Origin mismatch"}, status_code=403)
            if request.headers.get("x-sdlc") != "1":
                return JSONResponse(
                    {"detail": "Missing application request header"}, status_code=403
                )
        if path.startswith(("/api/", "/harness")) and path != "/api/unlock":
            if not secrets.compare_digest(
                request.cookies.get("sdlc_session", ""), token
            ):
                return JSONResponse(
                    {"detail": "Open the launch URL to unlock this local app"},
                    status_code=401,
                )
        if path.startswith("/harness") and request.method not in {"GET", "HEAD"}:
            return JSONResponse(
                {"detail": "Use the SDLC controls; the embedded debugger is read-only"},
                status_code=403,
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, error):
        # Pydantic's default response includes input values, which can contain an API key.
        return JSONResponse(
            {
                "detail": [
                    {"loc": item["loc"], "msg": item["msg"]} for item in error.errors()
                ]
            },
            status_code=422,
        )

    @app.post("/api/unlock")
    async def unlock(request: Request):
        body = await request.json()
        if not secrets.compare_digest(str(body.get("token", "")), token):
            raise HTTPException(401, "Incorrect launch token")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "sdlc_session", token, httponly=True, samesite="strict", max_age=30 * 86400
        )
        return response

    @app.get("/api/home")
    async def home():
        return {
            "projects": store.all("projects"),
            "profiles": [
                {**profile, "available": profile_available(profile)}
                for profile in store.all("profiles")
            ],
            "tasks": store.all("tasks"),
            "health": {
                "temporal": app.state.temporal_ok,
                "harness_version": importlib.metadata.version("temporal-agent-harness"),
                "data_dir": str(data_dir()),
                "execution": "host-with-approval",
                "milestone": "3 · execution workspace and interactive review",
            },
        }

    @app.post("/api/projects")
    async def add_project(body: NewProject):
        path = Path(body.path).expanduser().resolve()
        actual = (
            await asyncio.to_thread(
                workspaces.git, path, "rev-parse", "--show-toplevel"
            )
        ).strip()
        for item in store.all("projects"):
            if item["path"] == actual:
                return item
        remote = await asyncio.to_thread(workspaces.git, path, "remote", "-v")
        # Do not persist embedded HTTP credentials in remote URLs.
        from urllib.parse import urlsplit

        origin = ""
        for line in remote.splitlines():
            if line.startswith("origin\t"):
                candidate = line.split()[1]
                if candidate.startswith("git@github.com:"):
                    origin = "https://github.com/" + candidate.split(":", 1)[
                        1
                    ].removesuffix(".git")
                elif (
                    candidate.startswith("https://github.com/")
                    and not urlsplit(candidate).username
                ):
                    origin = candidate.removesuffix(".git")
                break
        project = {
            "id": uuid.uuid4().hex,
            "name": Path(actual).name,
            "path": actual,
            "github_url": origin,
        }
        store.put("projects", project)
        return project

    @app.post("/api/demo")
    async def demo():
        path = await asyncio.to_thread(workspaces.demo_project)
        project = await add_project(NewProject(path=str(path)))
        project["demo"] = True
        store.put("projects", project)
        return project

    @app.post("/api/profiles")
    async def save_profile(body: SaveProfile):
        if body.provider not in registered_integrations():
            raise ValueError("Only OpenAI profiles can be added in this milestone")
        if body.provider == "compatible" and not body.base_url:
            raise ValueError("Compatible providers need a base URL")
        if body.base_url:
            from urllib.parse import urlsplit

            url = urlsplit(body.base_url)
            if (
                url.scheme not in {"https", "http"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
            ):
                raise ValueError(
                    "Use an HTTP(S) provider URL without embedded credentials or query parameters"
                )
            if url.scheme == "http" and url.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise ValueError("Remote provider URLs must use HTTPS")
        identity = uuid.uuid4().hex
        credential = "env:" + body.env_var if body.env_var else ""
        if body.api_key:
            import keyring

            try:
                await asyncio.to_thread(
                    keyring.set_password, "sdlc-builder", identity, body.api_key
                )
            except Exception:
                raise ValueError(
                    "The OS keyring is unavailable. Use an environment variable reference instead."
                ) from None
            credential = "keyring:" + identity
        if not credential:
            raise ValueError("Provide an API key or environment variable name")
        profile = Profile(
            id=identity,
            label=body.label,
            provider=body.provider,
            model=body.model,
            base_url=body.base_url,
            credential=credential,
            max_steps=body.max_steps,
        )
        store.put("profiles", profile.model_dump())
        return profile

    @app.post("/api/profiles/{identity}/test")
    async def test_profile(identity: str):
        from .provider_checks import check_profile

        if identity in removing_profiles:
            raise HTTPException(409, "Wait for this profile to finish being removed")
        profile = Profile.model_validate(store.get("profiles", identity))
        if profile.provider == "demo":
            raise ValueError("The demo profile does not connect to a provider")
        integration_for(profile)
        if identity in profile_checks:
            raise HTTPException(
                409, "A connection test for this profile is already running"
            )
        profile_checks.add(identity)
        try:
            return await check_profile(profile)
        finally:
            profile_checks.discard(identity)

    @app.delete("/api/profiles/{identity}")
    async def delete_profile(identity: str):
        if identity in removing_profiles:
            raise HTTPException(409, "This profile is already being removed")
        removing_profiles.add(identity)
        try:
            return await remove_profile(identity)
        finally:
            removing_profiles.discard(identity)

    async def remove_profile(identity: str):
        profile = store.get("profiles", identity)
        if identity in profile_checks:
            raise HTTPException(
                409, "Wait for the connection test before removing this profile"
            )
        if identity == "demo":
            raise ValueError("The built-in demo profile cannot be removed")
        for task in store.all("tasks"):
            turns = (
                (task.get("snapshot") or {})
                .get("conversation", {})
                .get("value", {})
                .get("turns", [])
            )
            active_profile = task.get("current_profile_id") or (
                turns[-1]["profile_id"] if turns else task.get("profile_id")
            )
            pending_profile = (
                task.get("pending_message", {})
                .get("payload", {})
                .get("profile", {})
                .get("id")
            )
            if pending_profile == identity:
                raise HTTPException(
                    409,
                    "Stop active tasks using this profile before removing its credential",
                )
            if active_profile == identity:
                async with task_locks.setdefault(task["id"], asyncio.Lock()):
                    try:
                        current = await get_task(task["id"])
                    except ValueError:  # Deleted while waiting for the task lock.
                        continue
                    if not current.get("can_message"):
                        raise HTTPException(
                            409,
                            "Stop active tasks using this profile before removing its credential; reconnect to Temporal if it is unavailable",
                        )
        if profile["credential"].startswith("keyring:"):
            import keyring

            try:
                await asyncio.to_thread(
                    keyring.delete_password, "sdlc-builder", profile["credential"][8:]
                )
            except keyring.errors.PasswordDeleteError:
                pass
            except Exception:
                raise ValueError(
                    "Could not remove the key from the OS keyring; the profile was kept"
                ) from None
        store.delete_profile(identity)
        return {"ok": True}

    @app.post("/api/tasks")
    async def create_task(body: NewTask):
        async with task_locks.setdefault(body.id, asyncio.Lock()):
            previous = next((t for t in store.all("tasks") if t["id"] == body.id), None)
            if previous:
                if previous.get("dispatch") == "preparing":
                    raise ValueError(
                        "Workspace preparation was interrupted. Start a new task; the partial workspace is preserved for inspection."
                    )
                return previous
            project = store.get("projects", body.project_id)
            if body.profile_id in removing_profiles:
                raise HTTPException(
                    409, "Wait for profile removal and select another profile"
                )
            profile = Profile.model_validate(store.get("profiles", body.profile_id))
            if profile.provider != "demo":
                integration_for(profile)
            if profile.provider == "demo" and not project.get("demo"):
                raise ValueError(
                    "The demo profile only operates on the demo repository. Configure a provider for your project."
                )
            prior = None
            if body.parent_task_id:
                prior = await get_task(body.parent_task_id)
                prior_state = (prior.get("snapshot") or {}).get("value", {})
                if (
                    prior["project_id"] != project["id"]
                    or prior_state.get("status")
                    not in {"review", "accepted", "failed", "cancelled"}
                    or not prior.get("can_message")
                ):
                    raise ValueError(
                        "Continue from a finished task in the same repository"
                    )
            task_input = TaskInput(
                task_id=body.id, prompt=body.prompt, mode=body.mode, profile=profile
            )
            if prior:
                task_input.continuation_context = (
                    "Prior request: "
                    + prior["title"]
                    + "\nPrior outcome: "
                    + prior_state.get("outcome", "")
                )[:8000]
            task = {
                "id": body.id,
                "workflow_id": "sdlc-" + body.id,
                "engine": body.engine,
                "project_id": project["id"],
                "project_name": project["name"],
                "title": body.prompt[:90],
                "mode": body.mode,
                "profile_id": profile.id,
                "created_at": time.time(),
                "dispatch": "preparing",
                "input": task_input.model_dump(),
            }
            store.put("tasks", task)
            try:
                if prior:
                    async with task_locks.setdefault(prior["id"], asyncio.Lock()):
                        current_parent = await get_task(prior["id"])
                        if not current_parent.get("can_message"):
                            raise ValueError(
                                "Finish or stop the parent task before forking its workspace"
                            )
                        prepared = await asyncio.to_thread(
                            workspaces.continue_workspace, prior["id"], body.id
                        )
                else:
                    prepared = await asyncio.to_thread(
                        workspaces.prepare_workspace, project["path"], body.id
                    )
            except (ValueError, OSError) as error:
                task.update(dispatch="preparing", preparation_error=str(error))
                store.put("tasks", task)
                raise
            task.update(prepared, dispatch="pending")
            store.put("tasks", task)
            return task

    @app.get("/api/tasks/{identity}")
    async def get_task(identity: str):
        task = store.get("tasks", identity)
        try:
            envelope = await app.state.client.get_workflow_handle(
                task["workflow_id"]
            ).query("progress", rpc_timeout=timedelta(seconds=3))
            store.archive(identity, envelope)
            task = store.get("tasks", identity)
            task["live"] = True
            task["can_message"] = bool(envelope.get("can_message")) and not task.get(
                "pending_message"
            )
            task["next_turn"] = envelope.get("next_turn", 2)
        except Exception:
            task["live"] = False
            task["can_message"] = False
        return task

    @app.post("/api/tasks/{identity}/messages")
    async def send_message(identity: str, body: NewMessage):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = store.get("tasks", identity)
            fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
            previous = task.get("message_receipts", {}).get(body.id)
            pending = task.get("pending_message")
            if previous:
                if previous["request_hash"] != fingerprint:
                    raise HTTPException(
                        409, "This message ID was already used for different content"
                    )
                return previous
            if pending:
                if pending["id"] != body.id:
                    raise HTTPException(
                        409, "Wait for the pending message to be delivered"
                    )
                if pending["request_hash"] != fingerprint:
                    raise HTTPException(
                        409, "This message ID was already used for different content"
                    )
                return await dispatch_followup(app, task)
            if not body.prompt.strip():
                raise HTTPException(422, "Write a message before sending")
            current = await get_task(identity)
            if not current["live"]:
                raise HTTPException(
                    503, "Reconnect boltzmann to Temporal before sending a message"
                )
            if not current["can_message"]:
                raise HTTPException(
                    409, "Finish or stop the current message before sending a follow-up"
                )
            if body.expected_turn != current["next_turn"]:
                raise HTTPException(
                    409,
                    "Another message was sent from this task. Refresh and review it before sending again",
                )
            if body.profile_id in removing_profiles:
                raise HTTPException(
                    409, "Wait for profile removal and select another profile"
                )
            profile = Profile.model_validate(store.get("profiles", body.profile_id))
            if profile.provider != "demo":
                integration_for(profile)
            elif not store.get("projects", task["project_id"]).get("demo"):
                raise ValueError(
                    "The demo profile only operates on the demo repository"
                )
            profile = profile.model_copy(update={"max_steps": body.max_steps})
            payload = FollowUpInput(
                message_id=body.id,
                prompt=body.prompt,
                mode=body.mode,
                profile=profile,
            )
            task["pending_message"] = {
                "id": body.id,
                "request_hash": fingerprint,
                "expected_turn": body.expected_turn,
                "payload": payload.model_dump(),
            }
            task.pop("message_error", None)
            # Persist before delivery. Reconciliation retries the same Temporal
            # update ID after a disconnect/restart, including a lost receipt.
            store.put("tasks", task)
            return await dispatch_followup(app, task)

    @app.post("/api/tasks/{identity}/control")
    async def control(identity: str, body: Control):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = store.get("tasks", identity)
            if task.get("pending_message"):
                raise HTTPException(
                    409, "Wait for message delivery before using task controls"
                )
            result = await app.state.client.get_workflow_handle(
                task["workflow_id"]
            ).execute_update("control", body)
            if not result["ok"]:
                raise HTTPException(409, result["error"])
            return result

    @app.delete("/api/tasks/{identity}")
    async def delete_task(identity: str, body: DeleteTask = DeleteTask()):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = store.get("tasks", identity)
            if task.get("pending_message"):
                raise HTTPException(
                    409,
                    "Wait for message delivery, then stop the task before deleting it",
                )
            # Never trust the catalogue's path for a destructive operation.
            task_workspace = workspace(identity)
            if task.get("dispatch") != "preparing":
                handle = app.state.client.get_workflow_handle(task["workflow_id"])
                try:
                    state = await handle.query(
                        "progress", rpc_timeout=timedelta(seconds=3)
                    )
                except RPCError as error:
                    if error.status != RPCStatusCode.NOT_FOUND:
                        raise HTTPException(
                            503,
                            "Could not verify that this task has stopped. Check Temporal and try deleting again; the task and workspace were kept.",
                        ) from None
                    # No execution remains (including a previous deletion whose
                    # filesystem cleanup failed). A fresh pending dispatch must
                    # first be resolved by the collector.
                    if task.get("dispatch") != "submitted":
                        raise HTTPException(
                            409,
                            "Wait for task dispatch, then stop the task before deleting it",
                        ) from None
                except Exception:
                    raise HTTPException(
                        503,
                        "Could not verify that this task has stopped. Check Temporal and try deleting again; the task and workspace were kept.",
                    ) from None
                else:
                    if state.get("can_message") is False or state["value"].get(
                        "status"
                    ) not in {
                        "review",
                        "accepted",
                        "failed",
                        "cancelled",
                    }:
                        raise HTTPException(
                            409,
                            "Stop this task and wait for its current action to finish before deleting it",
                        )
                    store.archive(identity, state)
                try:
                    await handle.terminate(
                        reason="Deleted in boltzmann",
                        rpc_timeout=timedelta(seconds=3),
                    )
                except RPCError as error:
                    if error.status != RPCStatusCode.NOT_FOUND:
                        raise HTTPException(
                            503,
                            "Could not close this task in Temporal. Try deleting again; the task and workspace were kept.",
                        ) from None
                except Exception:
                    raise HTTPException(
                        503,
                        "Could not close this task in Temporal. Try deleting again; the task and workspace were kept.",
                    ) from None
            if body.remove_workspace:
                try:
                    if task_workspace.exists():
                        await asyncio.to_thread(shutil.rmtree, task_workspace)
                except OSError:
                    raise HTTPException(
                        409,
                        "The task is stopped, but its workspace could not be fully removed. Check file permissions and retry, or delete with the workspace option unchecked to keep the remaining files.",
                    ) from None
            store.delete_task(identity)
            return {
                "ok": True,
                "workspace_removed": body.remove_workspace,
                "workspace": str(task_workspace),
            }

    @app.get("/api/tasks/{identity}/files")
    async def list_files(identity: str):
        store.get("tasks", identity)
        return await asyncio.to_thread(workspaces.files, workspace(identity))

    @app.get("/api/tasks/{identity}/file")
    async def get_file(identity: str, path: str):
        store.get("tasks", identity)
        return await asyncio.to_thread(workspaces.read_file, workspace(identity), path)

    @app.put("/api/tasks/{identity}/file")
    async def edit_file(identity: str, body: FileEdit):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = await get_task(identity)
            state = (task.get("snapshot") or {}).get("value", {})
            if (
                task.get("pending_message")
                or (state.get("status") != "paused" and not task.get("can_message"))
                or not task["live"]
                or state.get("status")
                not in {
                    "paused",
                    "review",
                    "failed",
                    "cancelled",
                }
            ):
                raise HTTPException(
                    409, "Pause the agent at an action boundary before editing"
                )
            handle = app.state.client.get_workflow_handle(task["workflow_id"])
            invalidated = await handle.execute_update(
                "control", Control(command="edit_started")
            )
            if not invalidated["ok"]:
                raise HTTPException(409, invalidated["error"])
            result = await asyncio.to_thread(
                workspaces.write_file,
                workspace(identity),
                body.path,
                body.content,
                body.expected_hash,
            )
            await handle.execute_update(
                "control", Control(command="edit_finished", text=body.path)
            )
            return result

    @app.get("/api/tasks/{identity}/diff")
    async def get_diff(identity: str):
        task = store.get("tasks", identity)
        return await asyncio.to_thread(
            workspaces.diff, workspace(identity), reviews.base_revision(task)
        )

    @app.get("/api/tasks/{identity}/review")
    async def get_review(identity: str):
        task = store.get("tasks", identity)
        return await asyncio.to_thread(
            reviews.summary, workspace(identity), task, store.review(identity)
        )

    @app.get("/api/tasks/{identity}/review/file")
    async def review_file(identity: str, path: str, comparison: str = "all"):
        if comparison not in {"all", "reviewed"}:
            raise HTTPException(422, "Unknown review comparison")
        task = store.get("tasks", identity)
        return await asyncio.to_thread(
            reviews.detail,
            workspace(identity),
            task,
            store.review(identity),
            path,
            comparison,
        )

    @app.put("/api/tasks/{identity}/review/checkpoint")
    async def review_checkpoint(identity: str, body: reviews.ReviewFile):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = store.get("tasks", identity)
            review = store.review(identity)
            await asyncio.to_thread(
                reviews.checkpoint, workspace(identity), task, review, body
            )
            store.save_review(identity, review)
            return {"ok": True}

    @app.post("/api/tasks/{identity}/review/comments")
    async def add_review_comment(identity: str, body: reviews.AddComment):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = store.get("tasks", identity)
            review = store.review(identity)
            if await asyncio.to_thread(
                reviews.add_comment, workspace(identity), task, review, body
            ):
                store.save_review(identity, review)
            return {"ok": True}

    @app.put("/api/tasks/{identity}/review/comments/{comment_id}")
    async def resolve_review_comment(
        identity: str, comment_id: str, body: reviews.ResolveComment
    ):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            store.get("tasks", identity)
            review = store.review(identity)
            comment = next(
                (c for c in review["comments"] if c["id"] == comment_id), None
            )
            if comment is None:
                raise HTTPException(404, "Review comment not found")
            if comment["resolved"] != body.resolved:
                comment["resolved"] = body.resolved
                store.save_review(identity, review)
            return {"ok": True}

    @app.post("/api/tasks/{identity}/review/fix-request")
    async def prepare_review_fixes(identity: str, body: reviews.FixRequest):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            store.get("tasks", identity)
            return {"prompt": reviews.fix_prompt(store.review(identity), body)}

    @app.post("/api/tasks/{identity}/merge")
    async def merge_task_into_project(identity: str):
        async with task_locks.setdefault(identity, asyncio.Lock()):
            task = await get_task(identity)
            if not task.get("live"):
                raise HTTPException(
                    503,
                    "Reconnect boltzmann to Temporal before merging this task",
                )
            if task.get("pending_message"):
                raise HTTPException(
                    409, "Wait for message delivery before merging this task"
                )
            if task.get("engine") != "v2":
                raise HTTPException(
                    409,
                    "Workflow-managed merge is available for Coding V2 tasks. Export this older task's patch instead.",
                )
            handle = app.state.client.get_workflow_handle(task["workflow_id"])
            result = await handle.execute_update("merge_project")
            if not result["ok"]:
                raise HTTPException(409, result["error"])
            # Make the workflow-owned receipt visible immediately when possible.
            # A failed refresh does not turn an already-completed merge into an error.
            try:
                store.archive(
                    identity,
                    await handle.query("progress", rpc_timeout=timedelta(seconds=3)),
                )
            except Exception:
                pass
            return result

    @app.get("/api/tasks/{identity}/patch")
    async def export_patch(identity: str):
        value = await get_diff(identity)
        if value["truncated"]:
            raise HTTPException(
                413, "Diff exceeds export limit; use Git in the workspace"
            )
        return Response(
            value["patch"],
            media_type="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="sdlc-{identity[:8]}.patch"'
            },
        )

    @app.get("/harness/api/sessions")
    async def harness_sessions():
        return [
            {
                "workflow_id": t["workflow_id"],
                "created_at": t["created_at"],
                "label": t["title"],
                "agent_workflow_type": WORKFLOW_NAME_V2
                if t.get("engine") == "v2"
                else WORKFLOW_NAME,
                "is_message_queuing_enabled": False,
                "is_discovered": True,
            }
            for t in store.all("tasks")
            if t.get("dispatch") == "submitted"
        ]

    app.mount("/harness", harness)
    static = Path(__file__).parent / "static"
    if static.exists():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/")
    async def index():
        if not (static / "index.html").exists():
            raise HTTPException(
                503,
                "Build the app UI first: cd apps/sdlc/ui && pnpm install && pnpm build",
            )
        return FileResponse(static / "index.html")

    return app
