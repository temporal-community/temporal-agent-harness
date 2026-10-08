# ABOUTME: Unit tests for analyze_script, the static summary of which host functions a Code Mode
# script reaches and how. Pure: no Temporal env and no sandbox. Each case checks that the facts
# over-approximate what the script can do, never under-approximate it.

from __future__ import annotations

import textwrap

import pytest

from temporal_agent_harness.harness.code_mode import (
    HostFunctionUse,
    ScriptFacts,
    analyze_script,
)

HOST = {"search_flights", "book_flight", "book_hotel"}


def _analyze(body: str) -> ScriptFacts:
    script = "import asyncio\nasync def main():\n" + textwrap.indent(
        textwrap.dedent(body).strip("\n"), "    "
    ) + "\nasyncio.run(main())\n"
    return analyze_script(script, HOST)


def _use(call_sites: int = 1, *, in_loop: bool = False, concurrent: bool = False,
         indirect: bool = False) -> HostFunctionUse:
    return HostFunctionUse(
        call_sites=call_sites, in_loop=in_loop, concurrent=concurrent, indirect=indirect
    )


def test_straight_line_calls() -> None:
    facts = _analyze("""
        flights = await search_flights("SFO", "JFK")
        return await book_flight(flights[0]["id"])
    """)
    assert facts == ScriptFacts(
        uses={"book_flight": _use(), "search_flights": _use()},
        has_while_loop=False,
        dynamic_code=False,
    )


def test_unused_and_unknown_names_are_left_out() -> None:
    facts = _analyze("return await not_a_host_function()")
    assert facts.uses == {}


def test_counts_call_sites() -> None:
    facts = _analyze("""
        await book_flight("a")
        await book_flight("b")
    """)
    assert facts.uses["book_flight"].call_sites == 2


@pytest.mark.parametrize(
    "body",
    [
        "for f in ids:\n    await book_flight(f)",
        "async for f in ids:\n    await book_flight(f)",
        "results = [await book_flight(f) for f in ids]",
        "results = {f: await book_flight(f) for f in ids}",
    ],
)
def test_loops_and_comprehensions(body: str) -> None:
    assert _analyze(body).uses["book_flight"].in_loop


def test_loop_iterable_and_else_run_once() -> None:
    facts = _analyze("""
        for f in await search_flights("SFO", "JFK"):
            pass
        else:
            await book_flight("a")
    """)
    assert not facts.uses["search_flights"].in_loop
    assert not facts.uses["book_flight"].in_loop


def test_while_loop() -> None:
    facts = _analyze("""
        while await search_flights("SFO", "JFK"):
            await book_flight("a")
    """)
    assert facts.has_while_loop
    assert facts.uses["search_flights"].in_loop
    assert facts.uses["book_flight"].in_loop


@pytest.mark.parametrize(
    "body",
    [
        "await asyncio.gather(book_flight('a'), search_flights('SFO', 'JFK'))",
        "await asyncio.gather(*[book_flight(f) for f in ids])",
        "t = asyncio.create_task(book_flight('a'))\nawait t",
    ],
)
def test_concurrent_calls(body: str) -> None:
    assert _analyze(body).uses["book_flight"].concurrent


def test_helper_called_in_a_loop_puts_its_host_calls_in_the_loop() -> None:
    facts = _analyze("""
        async def book(f):
            return await book_flight(f)

        async def book_all(ids):
            return await asyncio.gather(*[book(f) for f in ids])

        await book_all(["a", "b"])
        await search_flights("SFO", "JFK")
    """)
    assert facts.uses["book_flight"] == _use(in_loop=True, concurrent=True)
    assert facts.uses["search_flights"] == _use()


def test_helper_called_once_stays_out_of_loops() -> None:
    facts = _analyze("""
        async def book(f):
            return await book_flight(f)

        await book("a")
    """)
    assert facts.uses["book_flight"] == _use()


def test_recursive_helper_counts_as_a_loop() -> None:
    facts = _analyze("""
        async def book(ids):
            if ids:
                await book_flight(ids[0])
                await book(ids[1:])

        await book(["a", "b"])
    """)
    assert facts.uses["book_flight"].in_loop


@pytest.mark.parametrize(
    "body",
    [
        # A helper passed around rather than called by name.
        "async def book(f):\n    return await book_flight(f)\nawait run_each(book, ids)",
        # A lambda.
        "g = lambda: book_flight('a')\nawait g()",
        # A method.
        "class C:\n    async def m(self):\n        return await book_flight('a')\nawait C().m()",
    ],
)
def test_code_reached_where_the_analysis_cannot_follow_is_unknown(body: str) -> None:
    use = _analyze(body).uses["book_flight"]
    assert use.in_loop and use.concurrent


def test_non_call_reference_is_indirect() -> None:
    facts = _analyze("""
        f = book_flight
        await f("a")
    """)
    assert facts.uses["book_flight"] == _use(call_sites=0, indirect=True)


@pytest.mark.parametrize("builtin", ["eval", "exec", "compile"])
def test_dynamic_code_reaches_every_host_function(builtin: str) -> None:
    facts = _analyze(f"{builtin}(source)")
    assert facts.dynamic_code
    assert set(facts.uses) == HOST
    for use in facts.uses.values():
        assert use.in_loop and use.concurrent and use.indirect


def test_syntax_error_propagates() -> None:
    with pytest.raises(SyntaxError):
        analyze_script("def broken(:\n", HOST)
