# ABOUTME: Unit tests for the Code Mode type-check stub generator (render_type_check_stubs /
# render_host_interface). These need no Temporal env — they assert on the generated source
# (validity via ast.parse + structural checks), that unrepresentable/untyped tools RAISE rather
# than degrade to Any, and (where pydantic_monty is installed) that the sandbox actually accepts
# the stubs and rejects a bad script.

from __future__ import annotations

import ast
import enum
import inspect
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

import pytest
from pydantic import BaseModel, Field, create_model

from temporal_agent_harness.harness import agent
from temporal_agent_harness.harness.code_mode.stubs import (
    CodeModeStubError,
    render_host_interface,
    render_type_check_stubs,
)


# --- models used across tests (module level so forward refs resolve via get_type_hints) ---


class Item(BaseModel):
    id: str
    price: float


class SearchReq(BaseModel):
    origin: str
    n: int


class SearchResp(BaseModel):
    items: list[Item]


class Node(BaseModel):
    value: int
    children: list["Node"] = Field(default_factory=list)


class Color(str, enum.Enum):
    RED = "red"
    GREEN = "green"


class ScalarBag(BaseModel):
    created: datetime
    ref: UUID
    amount: Decimal
    blob: bytes


def _parse(src: str) -> ast.Module:
    """Assert the generated stub is syntactically valid Python and return its AST."""
    return ast.parse(src)


def _typed_dict_names(src: str) -> list[str]:
    tree = _parse(src)
    return [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]


def _func_by_name(src: str, name: str) -> ast.AsyncFunctionDef:
    tree = _parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name:
            return n
    raise AssertionError(f"no async def {name} in stub:\n{src}")


def test_primitives_render_with_no_typeddicts():
    async def do(a: str, b: int, c: float, d: bool) -> str: ...

    src = render_type_check_stubs([do])
    _parse(src)
    assert _typed_dict_names(src) == []
    assert "async def do(a: str, b: int, c: float, d: bool) -> str: ..." in src


def test_nested_model_param_and_list_of_model_return():
    async def search(request: SearchReq) -> SearchResp: ...

    src = render_type_check_stubs([search])
    names = _typed_dict_names(src)
    assert set(names) == {"SearchReq", "SearchResp", "Item"}
    assert "items: list[Item]" in src
    assert "async def search(request: SearchReq) -> SearchResp: ..." in src


def test_optional_and_union_render_as_pep604():
    async def f(a: Optional[int], b: int | str) -> str: ...  # noqa: UP045

    src = render_type_check_stubs([f])
    fn = _func_by_name(src, "f")
    assert ast.unparse(fn.args.args[0].annotation) == "int | None"
    assert ast.unparse(fn.args.args[1].annotation) == "int | str"


def test_dict_container_renders_key_and_value():
    async def f(m: dict[str, int]) -> str: ...

    src = render_type_check_stubs([f])
    assert "m: dict[str, int]" in src


def test_unknown_type_raises_naming_context():
    # complex is a resolvable builtin but not representable in Code Mode stubs.
    async def f(x: complex) -> str: ...

    with pytest.raises(CodeModeStubError) as exc:
        render_type_check_stubs([f])
    assert "f(x)" in str(exc.value)


def test_missing_param_annotation_raises():
    async def f(a) -> str: ...  # noqa: ANN001

    with pytest.raises(CodeModeStubError, match="no type annotation"):
        render_type_check_stubs([f])


def test_missing_return_annotation_raises():
    async def f(a: str): ...

    with pytest.raises(CodeModeStubError, match="no return annotation"):
        render_type_check_stubs([f])


def test_bare_container_raises():
    async def f(xs: list) -> str: ...  # noqa: ANN401

    with pytest.raises(CodeModeStubError, match="element type"):
        render_type_check_stubs([f])


def test_shared_model_is_emitted_once():
    async def a(req: SearchReq) -> Item: ...
    async def b(req: SearchReq) -> Item: ...

    src = render_type_check_stubs([a, b])
    names = _typed_dict_names(src)
    # SearchReq and Item each appear exactly once despite being referenced by both tools.
    assert names.count("SearchReq") == 1
    assert names.count("Item") == 1


def test_name_collision_gets_distinct_typeddict_names():
    Dup1 = create_model("Dup", x=(int, ...))
    Dup2 = create_model("Dup", y=(str, ...))
    assert Dup1.__name__ == Dup2.__name__ == "Dup"

    # Stamp the real (distinct) class objects as annotations directly — the two models share a
    # __name__ but are different classes, which the generator must not merge.
    async def a(req): ...
    async def b(req): ...

    for fn, model in ((a, Dup1), (b, Dup2)):
        fn.__annotations__ = {"req": model, "return": str}
        fn.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            [inspect.Parameter("req", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=model)],
            return_annotation=str,
        )

    src = render_type_check_stubs([a, b])
    names = [n for n in _typed_dict_names(src) if n.startswith("Dup")]
    # Two distinct classes with the same __name__ get two distinct TypedDicts (not merged).
    assert len(names) == 2
    assert len(set(names)) == 2


def test_recursive_model_terminates_and_references_itself():
    async def f(n: Node) -> Node: ...

    src = render_type_check_stubs([f])
    assert _typed_dict_names(src).count("Node") == 1
    assert "children: list[Node]" in src


def test_enum_renders_as_literal_of_values():
    async def f(c: Color) -> str: ...

    src = render_type_check_stubs([f])
    assert "c: Literal['red', 'green']" in src
    assert "from typing import" in src and "Literal" in src


def test_json_scalar_types_render_as_str():
    async def f(bag: ScalarBag) -> str: ...

    src = render_type_check_stubs([f])
    # datetime / UUID / Decimal / bytes all serialize to strings under model_dump(mode="json").
    assert "created: str" in src
    assert "ref: str" in src
    assert "amount: str" in src
    assert "blob: str" in src


def test_keyword_only_params_render_star_separator():
    async def f(a: str, *, b: int) -> str: ...

    src = render_type_check_stubs([f])
    assert "async def f(a: str, *, b: int) -> str: ..." in src


def test_defaults_are_not_emitted():
    async def f(a: str, b: int = 5) -> str: ...

    src = render_type_check_stubs([f])
    fn = _func_by_name(src, "f")
    # No default values in the stub (types only) — the host supplies behavior, not the stub.
    assert fn.args.defaults == []


def test_explicit_any_is_allowed():
    async def f(x: Any) -> str: ...

    src = render_type_check_stubs([f])
    assert "x: Any" in src
    assert "from typing import" in src and "Any" in src


def test_host_interface_attaches_tool_docstrings():
    async def search(request: SearchReq) -> SearchResp:
        """Search the catalog for items."""
        ...

    interface = render_host_interface([search])
    _parse(interface)
    assert "Search the catalog for items." in interface
    # Result TypedDicts are present so the model knows the shape it can index into.
    assert "class SearchResp(TypedDict):" in interface


# --- composition over subagents: a subagent send tool must render faithfully ---


class _Question(BaseModel):
    """A question to research."""

    text: str = Field(description="The question.")


class _Answer(BaseModel):
    """An answer."""

    text: str


class _ChildAgent:
    @agent.accepts
    async def ask(self, q: _Question) -> _Answer:
        """Answer a free-form question."""
        ...


def test_subagent_send_tool_signature_renders():
    tools = {
        t.__name__: t
        for t in agent.subagent_toolset(_ChildAgent, key="child", task_queue="q")
    }
    src = render_type_check_stubs([tools["child_ask"]])
    _parse(src)
    # subagent handle (str) + the child's real input model, returning the real output model.
    assert "async def child_ask(subagent: str, q: _Question) -> _Answer: ..." in src
    assert {"_Question", "_Answer"} <= set(_typed_dict_names(src))


# --- integration: the sandbox actually accepts good stubs and rejects bad scripts ---

monty = pytest.importorskip("pydantic_monty")


def _good_script() -> str:
    return (
        "import asyncio\n"
        "async def main():\n"
        '    r = await search({"origin": "SFO", "n": 3})\n'
        '    return r["items"][0]["price"]\n'
        "asyncio.run(main())"
    )


async def _search_tool(request: SearchReq) -> SearchResp: ...
_search_tool.__name__ = "search"


def test_generated_stubs_type_check_a_good_script():
    stubs = render_type_check_stubs([_search_tool])
    monty.Monty(_good_script(), type_check=True, type_check_stubs=stubs).start(
        print_callback=monty.CollectString()
    )


def test_generated_stubs_reject_unknown_result_key():
    stubs = render_type_check_stubs([_search_tool])
    bad = (
        "import asyncio\n"
        "async def main():\n"
        '    r = await search({"origin": "SFO", "n": 3})\n'
        '    return r["items"][0]["nope"]\n'
        "asyncio.run(main())"
    )
    with pytest.raises(monty.MontyError):
        monty.Monty(bad, type_check=True, type_check_stubs=stubs).start(
            print_callback=monty.CollectString()
        )


def test_generated_stubs_reject_wrong_argument_type():
    stubs = render_type_check_stubs([_search_tool])
    bad = (
        "import asyncio\n"
        "async def main():\n"
        '    return await search({"origin": "SFO", "n": "three"})\n'
        "asyncio.run(main())"
    )
    with pytest.raises(monty.MontyError):
        monty.Monty(bad, type_check=True, type_check_stubs=stubs).start(
            print_callback=monty.CollectString()
        )
