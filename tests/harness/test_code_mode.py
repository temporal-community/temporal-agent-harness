# ABOUTME: Unit tests for the code_mode_tool factory — the construction-time guards (only real
# harness tools, no duplicate names, no injected params, all types representable) and the shape
# of the single generated run-code tool. No Temporal env needed; these only build + introspect.

from __future__ import annotations

import inspect

import pytest

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
    stub = render_type_check_stubs([read_page])
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
