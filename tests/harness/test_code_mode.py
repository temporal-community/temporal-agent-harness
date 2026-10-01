# ABOUTME: Unit tests for the code_mode_tool factory — the construction-time guards (only real
# harness tools, no duplicate names, no injected params, all types representable) and the shape
# of the single generated run-code tool. No Temporal env needed; these only build + introspect.

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel
from temporalio.exceptions import ApplicationError

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.code_mode.stubs import (
    CodeModeStubError,
    render_type_check_stubs,
)


@agent.activity_tool_defn(name="alpha")
async def _alpha(request: str) -> str:
    """Do the alpha thing."""
    ...


@agent.tool_defn()
async def beta(x: int) -> int:
    """Do the beta thing."""
    ...


def test_returns_single_inline_tool_with_expected_shape():
    tool = agent.code_mode_tool([_alpha, beta], name="run_stuff")
    assert tool.__name__ == "run_stuff"
    assert getattr(tool, "__agent_tool__", False)  # an inline tool_defn, run_tool-dispatchable
    sig = inspect.signature(tool)
    assert list(sig.parameters) == ["script"]
    assert sig.parameters["script"].annotation is str
    assert sig.return_annotation is str


def test_docstring_embeds_the_host_interface_and_contract():
    tool = agent.code_mode_tool([_alpha, beta], name="run_stuff")
    doc = tool.__doc__ or ""
    # The model-facing docstring carries the sandbox contract plus every host function signature.
    assert "asyncio.run(main())" in doc
    assert "async def alpha(request: str) -> str" in doc
    assert "async def beta(x: int) -> int" in doc
    assert "Do the alpha thing." in doc


def test_rejects_non_harness_callable():
    async def not_a_tool(a: str) -> str: ...

    with pytest.raises(ValueError, match="harness tools"):
        agent.code_mode_tool([not_a_tool], name="run_x")


def test_rejects_duplicate_host_function_names():
    @agent.activity_tool_defn(name="dup")
    async def one(a: str) -> str:
        """One."""
        ...

    @agent.activity_tool_defn(name="dup")
    async def two(b: str) -> str:
        """Two."""
        ...

    with pytest.raises(ValueError, match="duplicate host-function name 'dup'"):
        agent.code_mode_tool([one, two], name="run_x")


def test_injected_params_are_accepted_and_hidden_from_the_interface():
    @agent.tool_defn()
    async def read_page(store: agent.Injected[str], page_url: str) -> str:
        """Read a page from an injected store."""
        ...

    # A tool with an Injected[...] param is accepted (not rejected); the injected `store` is
    # hidden from the script (the harness supplies it), so the host function exposes only the
    # model-facing `page_url`. The type-check stub carries no docstrings, so its absence of
    # "store" is a clean signal the parameter was scrubbed.
    tool = agent.code_mode_tool([read_page], name="run_x", injections={"store": "S"})
    assert tool.__name__ == "run_x"
    stub = render_type_check_stubs([read_page]).source
    assert "async def read_page(page_url: str) -> str: ..." in stub
    assert "store" not in stub


def test_rejects_unrepresentable_tool_signature():
    class Weird:
        pass

    @agent.tool_defn()
    async def weird(x: Weird) -> str:
        """Takes an unrepresentable type."""
        ...

    with pytest.raises(CodeModeStubError):
        agent.code_mode_tool([weird], name="run_x")


def test_rejects_empty_tool_list():
    with pytest.raises(ValueError, match="at least one tool"):
        agent.code_mode_tool([], name="run_x")


def test_distinct_names_allow_multiple_code_mode_tools_over_overlapping_sets():
    # Two Code Mode tools on one agent, with overlapping tool sets, are independent — each is a
    # distinct host tool the model can address by its own name.
    a = agent.code_mode_tool([_alpha, beta], name="run_a")
    b = agent.code_mode_tool([_alpha], name="run_b")
    assert a.__name__ == "run_a"
    assert b.__name__ == "run_b"
    assert a is not b


# ---------------------------------------------------------------- type-checking without running


def test_the_tool_carries_the_stubs_code_mode_type_check_reads():
    tool = agent.code_mode_tool([beta], name="run_code")
    assert tool.__code_mode_stubs__ == render_type_check_stubs([beta])


def test_code_mode_type_check_rejects_a_tool_code_mode_did_not_make():
    import asyncio

    with pytest.raises(TypeError, match="not a tool returned by code_mode_tool"):
        asyncio.run(agent.code_mode_type_check(beta, "x = 1"))


async def test_type_check_reports_errors_by_line_and_answers_no_host_call():
    pytest.importorskip("pydantic_monty")
    from temporal_agent_harness.harness.code_mode import monty_stepper

    stubs = render_type_check_stubs([beta])
    clean = "import asyncio\nasync def main():\n    return await beta(1)\nasyncio.run(main())\n"
    assert await monty_stepper.type_check(clean, stubs) is None

    # A clean script that prints is still only checked: its output goes nowhere.
    noisy = clean.replace("    return", "    print('ran')\n    return")
    assert await monty_stepper.type_check(noisy, stubs) is None

    report = await monty_stepper.type_check(clean.replace("beta(1)", 'beta("one")'), stubs)
    assert report is not None and report.startswith("MontyTypingError")
    assert "main.py:3" in report


class Box(BaseModel):
    size: int


@agent.tool_defn()
async def pack(box: Box) -> Box:
    """Pack a box."""
    ...


_PACK = "import asyncio\n{head}async def main():\n{body}    return await pack({{'size': 1}})\nasyncio.run(main())\n"


def test_the_stubs_name_the_typed_dicts_they_define():
    stubs = render_type_check_stubs([pack])
    assert stubs.type_names == ["Box"]
    assert "class Box(TypedDict):" in stubs.source
    assert render_type_check_stubs([beta]).type_names == []


async def test_calling_a_stub_type_builds_a_dict_inside_the_sandbox():
    """The stubs' TypedDicts are not defined at run time; the stepper answers a call to one
    with a dict, as CPython does, before and after a resume, and never surfaces it as a host
    call."""
    pytest.importorskip("pydantic_monty")
    from temporal_agent_harness.harness.code_mode import monty_stepper
    from temporal_agent_harness.harness.code_mode.batch_models import (
        CallResult,
        ResumeBatchInput,
    )

    script = (
        "import asyncio\n"
        "async def main():\n"
        "    first = await pack(Box(size=1))\n"
        "    again = Box({'size': first['size'] + 1})\n"
        "    return [again, await pack(again)]\n"
        "asyncio.run(main())\n"
    )
    stubs = render_type_check_stubs([pack])
    step = await monty_stepper.start_batch(script, stubs)
    assert not step.done, step.error
    assert [(c.function_name, c.args) for c in step.pending] == [("pack", [{"size": 1}])]

    step = await monty_stepper.resume_batch(
        ResumeBatchInput(
            snapshot=step.snapshot,
            results=[CallResult(call_id=step.pending[0].call_id, return_value={"size": 5})],
            type_names=stubs.type_names,
        )
    )
    assert not step.done, step.error
    assert [(c.function_name, c.args) for c in step.pending] == [("pack", [{"size": 6}])]


async def test_bad_arguments_to_a_stub_type_are_a_script_error():
    pytest.importorskip("pydantic_monty")
    from temporal_agent_harness.harness.code_mode import monty_stepper
    from temporal_agent_harness.harness.code_mode.batch_models import TypeCheckStubs

    # A stub loose enough to pass the call, so the sandbox, not the type checker, sees it.
    loose = TypeCheckStubs(
        source="from typing import Any\ndef Box(*args: Any) -> Any: ...\n", type_names=["Box"]
    )
    step = await monty_stepper.start_batch("Box(1)\n", loose)
    assert step.done and step.error and "TypeError" in step.error


@pytest.mark.parametrize(
    ("head", "body"),
    [
        ("from asyncio import gather\nimport json\n", ""),
        ("", "    b: Box = {'size': 2}\n"),
        ("def size(b: Box) -> int:\n    return b['size']\n", ""),
        ("", "    b = Box(size=1)\n"),
    ],
)
async def test_type_check_allows_what_runs(head, body):
    pytest.importorskip("pydantic_monty")
    from temporal_agent_harness.harness.code_mode import monty_stepper

    script = _PACK.format(head=head, body=body)
    assert await monty_stepper.type_check(script, render_type_check_stubs([pack])) is None


# ---------------------------------------------------------------- activities module typing


def test_the_stepper_keeps_real_monty_annotations():
    """The stepping logic must stay typed against the real Monty package.

    ``monty_stepper`` exists so the monty-touching code can import Monty normally, at module
    scope, and be type-checked like anything else — the ``code-mode`` extra is optional only
    at the :mod:`activities` boundary. Resolving an annotation here is what a type checker
    does, so this fails if a monty type is ever softened to ``Any``.
    """
    import typing

    pytest.importorskip("pydantic_monty")
    import pydantic_monty

    from temporal_agent_harness.harness.code_mode import monty_stepper

    hints = typing.get_type_hints(monty_stepper._drive_to_batch)
    assert hints["stdout"] is pydantic_monty.CollectString


def test_the_activities_module_imports_without_the_code_mode_extra():
    """``activities`` must load on a worker with no ``code-mode`` extra installed.

    That is what lets a worker register the two activity NAMES either way, so a misconfigured
    worker gets one actionable non-retryable failure instead of Temporal retrying an
    unregistered activity name forever. Binding ``pydantic_monty`` to ``None`` in
    ``sys.modules`` is the documented way to make its import fail.
    """
    import importlib
    import sys

    module = "temporal_agent_harness.harness.code_mode.activities"
    saved = sys.modules.pop(module, None)
    sys.modules["pydantic_monty"] = None  # type: ignore[assignment]
    try:
        activities = importlib.import_module(module)
        assert len(activities.CODE_MODE_ACTIVITIES) == 3
        with pytest.raises(ApplicationError) as caught:
            activities._require_code_mode_extra()
        assert caught.value.type == activities.CODE_MODE_MISSING_EXTRA_ERROR
        assert caught.value.non_retryable
        assert "temporal-agent-harness[code-mode]" in str(caught.value)
    finally:
        del sys.modules["pydantic_monty"]
        sys.modules.pop(module, None)
        if saved is not None:
            sys.modules[module] = saved
        else:
            importlib.import_module(module)


def test_the_stepping_activity_signatures_resolve_without_monty():
    """Temporal resolves an activity's own type hints, so those must not mention monty.

    ``@activity.defn`` registration runs ``get_type_hints`` on each activity; if a monty type
    leaked into one of these signatures it would have to be quoted, and then registration on a
    worker without the extra would fail before the actionable error could ever be raised.
    """
    import typing

    from temporal_agent_harness.harness.code_mode import activities

    for fn in activities.CODE_MODE_ACTIVITIES:
        resolved = typing.get_type_hints(fn)
        assert resolved, f"{fn.__name__} should have resolvable hints"
        assert not any(
            getattr(hint, "__module__", "").startswith("pydantic_monty")
            for hint in resolved.values()
        ), f"{fn.__name__} must not expose a monty type in its activity signature"
