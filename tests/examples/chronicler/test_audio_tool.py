import hashlib
import inspect
from types import SimpleNamespace

import pytest
from temporalio.common import WorkflowIDReusePolicy

from examples.chronicler import audio_tool
from examples.chronicler.audio_models import (
    AudioApprovalPackage,
    AudioGenerationRequest,
    AudioGenerationResult,
)
from examples.chronicler.audio_tool import audio_child_workflow_id, generate_audio
from examples.chronicler.audio_tool import _generate_audio_implementation


def _package() -> AudioApprovalPackage:
    source_content = "The party crossed the frozen bridge."
    return AudioApprovalPackage(
        package_revision=1,
        generation_id="generation-parent-7",
        source_kind="existing",
        source_identity="sessions/session-7.md",
        source_content=source_content,
        source_hash=hashlib.sha256(source_content.encode()).hexdigest(),
        recap_script="Previously, the party crossed the frozen bridge.",
        wav_path="audio/session-7-recap.wav",
        bridge_id="browser",
        root_id="campaign-root",
        folder_binding_id="binding-7",
    )


def test_audio_child_workflow_id_uses_the_exact_parent_scoped_id() -> None:
    assert audio_child_workflow_id("parent/chat-7") == "chronicler-audio--parent/chat-7"


def test_generate_audio_always_requires_fresh_approval() -> None:
    declaration = inspect.getclosurevars(generate_audio).nonlocals

    assert declaration["always_require_approval"] is True


@pytest.mark.asyncio
async def test_generate_audio_launches_the_approved_package_in_normal_mode(monkeypatch) -> None:
    package = _package()
    captured: list[AudioGenerationRequest] = []
    expected = object()

    async def launch(request: AudioGenerationRequest):
        captured.append(request)
        return expected

    monkeypatch.setattr(audio_tool, "launch_audio_child", launch)

    result = await _generate_audio_implementation(package)

    assert result is expected
    assert captured == [AudioGenerationRequest(package=package, mode="normal")]


@pytest.mark.asyncio
async def test_launch_audio_child_uses_one_reusable_fixed_child_id(monkeypatch) -> None:
    captured: dict[str, object] = {}
    expected = object()

    async def execute_child(*args, **kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr(
        audio_tool.workflow,
        "info",
        lambda: SimpleNamespace(workflow_id="parent/chat-7", task_queue="chronicler"),
    )
    monkeypatch.setattr(audio_tool.workflow, "execute_child_workflow", execute_child)

    result = await audio_tool.launch_audio_child(
        AudioGenerationRequest(package=_package(), mode="normal")
    )

    assert result is expected
    assert captured["id"] == "chronicler-audio--parent/chat-7"
    assert captured["id_reuse_policy"] is WorkflowIDReusePolicy.ALLOW_DUPLICATE
    assert captured["result_type"] is AudioGenerationResult
