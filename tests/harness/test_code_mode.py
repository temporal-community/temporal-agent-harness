# ABOUTME: Unit tests for the code_mode_tool factory — the construction-time guards (only real
# harness tools, no duplicate names, no injected params, all types representable) and the shape
# of the single generated run-code tool — and for the stepping engine that runs scripts in the
# sandbox. No Temporal env needed; these build, introspect, and step scripts directly.

from __future__ import annotations

import inspect
from datetime import datetime

import pytest
from pydantic import BaseModel

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.code_mode import monty_stepper
from temporal_agent_harness.harness.code_mode.stubs import (
    CodeModeStubError,
    TypeCheckStubs,
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


def test_type_check_reports_errors_by_line_and_answers_no_host_call():
    stubs = render_type_check_stubs([beta])
    clean = "import asyncio\nasync def main():\n    return await beta(1)\nasyncio.run(main())\n"
    assert monty_stepper.type_check(clean, stubs) is None

    # A clean script's run is discarded: what it prints, and how far it gets, is not reported.
    noisy = clean.replace("    return", "    print('ran')\n    return")
    assert monty_stepper.type_check(noisy, stubs) is None

    report = monty_stepper.type_check(clean.replace("beta(1)", 'beta("one")'), stubs)
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


def test_calling_a_stub_type_builds_a_dict_inside_the_sandbox():
    """The stubs' TypedDicts are not defined at run time; the stepper answers a call to one
    with a dict, as CPython does, before and after a resume, and never surfaces it as a host
    call."""
    script = (
        "import asyncio\n"
        "async def main():\n"
        "    first = await pack(Box(size=1))\n"
        "    again = Box({'size': first['size'] + 1})\n"
        "    return [again, await pack(again)]\n"
        "asyncio.run(main())\n"
    )
    run = _script_run(script, render_type_check_stubs([pack]))
    step = run.start()
    assert not step.done, step.error
    assert [(c.function_name, c.args) for c in step.awaiting] == [("pack", [{"size": 1}])]

    step = run.resume({step.awaiting[0].call_id: {"size": 5}})
    assert not step.done, step.error
    assert [(c.function_name, c.args) for c in step.awaiting] == [("pack", [{"size": 6}])]


def test_bad_arguments_to_a_stub_type_are_a_script_error():
    # A stub loose enough to pass the call, so the sandbox, not the type checker, sees it.
    loose = TypeCheckStubs(
        source="from typing import Any\ndef Box(*args: Any) -> Any: ...\n", type_names=["Box"]
    )
    step = _script_run("Box(1)\n", loose).start()
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
def test_type_check_allows_what_runs(head, body):
    script = _PACK.format(head=head, body=body)
    assert monty_stepper.type_check(script, render_type_check_stubs([pack])) is None


# ---------------------------------------------------------------- the stepping engine


def _no_os(name: str, args: tuple) -> object:
    raise NotImplementedError(name)


def _script_run(script: str, stubs: TypeCheckStubs, answer_os=_no_os):
    return monty_stepper.ScriptRun(script, stubs, answer_os=answer_os)


_BETA = render_type_check_stubs([beta])


def test_a_call_made_in_one_batch_resolves_when_a_later_one_awaits_it():
    script = (
        "import asyncio\n"
        "async def main():\n"
        "    later = beta(1)\n"
        "    first = await beta(2)\n"
        "    return [first, await later]\n"
        "asyncio.run(main())\n"
    )
    run = _script_run(script, _BETA)
    step = run.start()
    # Monty resolves every outstanding call at the first await, in the order they were made.
    assert [(c.function_name, c.args) for c in step.awaiting] == [("beta", [1]), ("beta", [2])]

    step = run.resume({c.call_id: c.args[0] * 10 for c in step.awaiting})
    assert step.done and step.output == [20, 10], step.error


def test_a_failed_host_call_raises_at_its_await():
    script = (
        "import asyncio\n"
        "async def main():\n"
        "    caught = []\n"
        "    for i in range(2):\n"
        "        try:\n"
        "            await beta(i)\n"
        "        except ValueError as e:\n"
        "            caught.append('value: ' + str(e))\n"
        "        except Exception as e:\n"
        "            caught.append('other: ' + str(e))\n"
        "    return caught\n"
        "asyncio.run(main())\n"
    )
    run = _script_run(script, _BETA)
    first = run.start().awaiting[0]
    second = run.resume({first.call_id: ValueError("bad")}).awaiting[0]
    step = run.resume({second.call_id: Exception("Denied: no")})
    assert step.output == ["value: bad", "other: Denied: no"], step.error


def test_a_sleep_is_handed_to_the_host():
    script = "import asyncio\nasync def main():\n    await asyncio.sleep(2.5)\n    return 1\nasyncio.run(main())\n"
    run = _script_run(script, _BETA)
    step = run.start()
    assert step.awaiting == [monty_stepper.Sleep(call_id=step.awaiting[0].call_id, seconds=2.5)]
    assert run.resume({step.awaiting[0].call_id: None}).output == 1


def test_the_clock_and_entropy_come_from_the_host():
    """Nothing the script observes comes from the worker, so equal answers give equal runs."""
    answers = {
        "datetime.now": datetime(2026, 1, 2, 3, 4, 5),
        "time.time": 1.5,
        "os.urandom": b"\x07" * 2496,
    }
    script = (
        "import datetime, random, time\n"
        "[datetime.datetime.now().isoformat(), time.time(), random.random(), list({'b', 'a'})]\n"
    )

    def outputs():
        run = _script_run(script, _BETA, answer_os=lambda name, args: answers[name])
        step = run.start()
        assert step.done, step.error
        return step.output

    first = outputs()
    assert first[:2] == ["2026-01-02T03:04:05", 1.5]
    assert outputs() == first


def test_an_os_call_the_host_leaves_unhandled_is_a_script_error():
    step = _script_run("open('/etc/passwd').read()\n", _BETA).start()
    assert step.done and step.error and not step.aborted


def test_a_step_that_runs_too_long_is_aborted():
    step = _script_run("while True:\n    pass\n", _BETA).start()
    assert step.done and step.aborted and step.timed_out


def test_a_resume_can_be_repeated_after_an_abort():
    """The driver re-runs an aborted step on replay; resuming restores the same dump each time."""
    script = (
        "import asyncio\n"
        "async def main():\n"
        "    n = await beta(1)\n"
        "    while n > 0:\n"
        "        pass\n"
        "    return n\n"
        "asyncio.run(main())\n"
    )
    run = _script_run(script, _BETA)
    call = run.start().awaiting[0]
    assert run.resume({call.call_id: 1}).aborted
    assert run.resume({call.call_id: 0}).output == 0


def test_the_stepper_keeps_real_monty_annotations():
    """The stepping logic must stay typed against the real Monty package.

    ``monty_stepper`` is the one module that imports Monty, at module scope, so it can be
    type-checked like anything else. Resolving an annotation here is what a type checker does,
    so this fails if a monty type is ever softened to ``Any``.
    """
    import typing

    import pydantic_monty

    hints = typing.get_type_hints(monty_stepper.ScriptRun._os_call)
    assert hints["snap"] is pydantic_monty.FunctionSnapshot


# ---------------------------------------------------------------- construction


def test_a_worker_without_the_extra_fails_when_the_tool_is_built(monkeypatch):
    """Building the tool, at ``@agent.init``, is where a missing extra surfaces: an error there
    fails the workflow task, which Temporal retries until a worker with the extra takes it,
    rather than becoming a failed tool call that history would then record."""
    import sys

    monkeypatch.setitem(sys.modules, "pydantic_monty", None)
    monkeypatch.delitem(sys.modules, monty_stepper.__name__)
    with pytest.raises(RuntimeError, match=r"temporal-agent-harness\[code-mode\]"):
        agent.code_mode_tool([beta], name="run_code")
