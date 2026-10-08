"""Facts about a Code Mode script, read from its source without running it.

:func:`analyze_script` tells an approval evaluator which host functions a script can reach and
how: how many call sites each has, whether any of them can run repeatedly or concurrently. It is
an OVER-approximation by design. Every fact errs toward "the script may do more", so a policy
that permits a script on these facts never permits something the script could do but the facts
hide:

  * A host function counts as used if its name appears anywhere, not only where it is called.
  * Loop and concurrency context follows calls into the script's own functions: a host call
    inside a helper that is called in a loop is ``in_loop``. A helper that is passed around
    rather than called by name, a lambda, a method, and a recursive function all run where the
    analysis can't see, so the host calls inside them count as both ``in_loop`` and
    ``concurrent``.
  * ``eval``, ``exec`` and ``compile`` run code the analysis can't read. Monty does dispatch a
    host call made inside an ``eval`` string, so a script that names any of them reports
    ``dynamic_code``, and every host function is reported as reachable, in a loop and
    concurrently.

Monty resolves a host function only as a direct call by name: an alias (``f = book_flight``)
raises ``NameError`` at run time, and ``globals()``, ``vars()`` and ``getattr`` are unavailable.
A non-call reference is still reported, as ``indirect``, in case that changes.

Pure and deterministic (stdlib :mod:`ast`), so it runs in workflow code as well as anywhere else.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from temporal_agent_harness.harness.agent_protocol import AutoApprovalContext

# Calls whose arguments run concurrently with each other, matched by the called name or
# attribute (``asyncio.gather``, ``tg.create_task``, ...).
_CONCURRENT_CALLS = frozenset({"gather", "create_task", "as_completed", "wait"})
_DYNAMIC_CODE = frozenset({"eval", "exec", "compile"})


class HostFunctionUse(BaseModel):
    """How a script uses one host function."""

    model_config = ConfigDict(frozen=True)

    call_sites: int
    """Direct calls in the source, ``book_flight(...)``. Not how many times they run."""
    in_loop: bool
    """Some call can run more than once: it sits in a loop, a comprehension, or code reached
    from one."""
    concurrent: bool
    """Some call can run alongside other calls: it sits in the arguments of
    ``asyncio.gather``, ``create_task``, ``as_completed`` or ``wait``, or code reached from them."""
    indirect: bool
    """The name is used other than as a direct call, e.g. ``f = book_flight``."""


class ScriptFacts(BaseModel):
    """What :func:`analyze_script` found. ``uses`` lists only the host functions a script can
    reach, keyed by name."""

    model_config = ConfigDict(frozen=True)

    uses: dict[str, HostFunctionUse]
    has_while_loop: bool
    dynamic_code: bool
    """The script names ``eval``, ``exec`` or ``compile``. Every host function is then in
    ``uses``, marked as in a loop and concurrent."""


ScriptClassifier = Callable[[AutoApprovalContext, ScriptFacts], Awaitable[Mapping[str, int]]]
"""Answers questions about a Code Mode script that its source alone can't settle, keyed by
question, each as how likely "yes" is from 0 to 100. Integers, not probabilities, because a
policy engine like Cedar has no floats. Given the call (whose ``code_mode`` is set) and the
:class:`ScriptFacts` already established, so a model-backed classifier can be shown them.
``jev_script_classifier`` is the builtin."""


def analyze_script(script: str, host_functions: Iterable[str]) -> ScriptFacts:
    """Summarize which of ``host_functions`` ``script`` can reach, and how.

    Raises :class:`SyntaxError` for a script that doesn't parse. Inside an auto mode evaluator,
    letting it propagate escalates the call to a human, which suits a script that can't run.
    """
    found = _Collector()
    found.visit(ast.parse(script))
    contexts = _scope_contexts(found)
    dynamic_code = any(name in found.calls or name in found.refs for name in _DYNAMIC_CODE)

    uses: dict[str, HostFunctionUse] = {}
    for name in sorted(set(host_functions)):
        sites = found.calls.get(name, [])
        refs = found.refs.get(name, [])
        if not sites and not refs and not dynamic_code:
            continue
        uses[name] = HostFunctionUse(
            call_sites=len(sites),
            in_loop=dynamic_code
            or any(site.in_loop or contexts[site.scope].in_loop for site in sites),
            concurrent=dynamic_code
            or any(site.concurrent or contexts[site.scope].concurrent for site in sites),
            indirect=dynamic_code or bool(refs),
        )
    return ScriptFacts(
        uses=uses, has_while_loop=found.has_while_loop, dynamic_code=dynamic_code
    )


@dataclass(frozen=True)
class _Context:
    in_loop: bool = False
    concurrent: bool = False

    def __or__(self, other: _Context) -> _Context:
        return _Context(self.in_loop or other.in_loop, self.concurrent or other.concurrent)


_UNKNOWN = _Context(in_loop=True, concurrent=True)

# A scope is the function or lambda node whose body a site is in; ``None`` is the module.
_Scope = ast.AST | None


@dataclass(frozen=True)
class _Site:
    scope: _Scope
    in_loop: bool
    concurrent: bool


class _Collector(ast.NodeVisitor):
    """One pass over the tree, recording every name's call sites and other references with the
    loop and concurrency context they appear in, relative to their own scope."""

    def __init__(self) -> None:
        self.calls: dict[str, list[_Site]] = defaultdict(list)
        self.refs: dict[str, list[_Site]] = defaultdict(list)
        self.defs: dict[str, list[ast.AST]] = defaultdict(list)
        self.opaque: set[ast.AST] = set()  # lambdas and methods: invoked where we can't see
        self.has_while_loop = False
        self._scope: _Scope = None
        self._loops = 0
        self._concurrent = 0
        self._in_class = False

    def _site(self) -> _Site:
        return _Site(self._scope, self._loops > 0, self._concurrent > 0)

    @contextmanager
    def _enter(self, *, scope: _Scope = None, loop: int = 0, concurrent: int = 0,
               in_class: bool | None = None) -> Iterator[None]:
        saved = (self._scope, self._loops, self._concurrent, self._in_class)
        if scope is not None:
            self._scope, self._loops, self._concurrent, self._in_class = scope, 0, 0, False
        self._loops += loop
        self._concurrent += concurrent
        if in_class is not None:
            self._in_class = in_class
        try:
            yield
        finally:
            self._scope, self._loops, self._concurrent, self._in_class = saved

    def _visit_all(self, nodes: Iterable[ast.AST | None]) -> None:
        for node in nodes:
            if node is not None:
                self.visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._visit_all(node.decorator_list)
        self._visit_all([*node.args.defaults, *node.args.kw_defaults])
        self.defs[node.name].append(node)
        if self._in_class:
            self.opaque.add(node)
        with self._enter(scope=node):
            self._visit_all(node.body)

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_all([*node.args.defaults, *node.args.kw_defaults])
        self.opaque.add(node)
        with self._enter(scope=node):
            self.visit(node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_all([*node.decorator_list, *node.bases, *node.keywords])
        with self._enter(in_class=True):
            self._visit_all(node.body)

    def _visit_for(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        with self._enter(loop=1):
            self.visit(node.target)
            self._visit_all(node.body)
        self._visit_all(node.orelse)

    visit_For = _visit_for
    visit_AsyncFor = _visit_for

    def visit_While(self, node: ast.While) -> None:
        self.has_while_loop = True
        with self._enter(loop=1):
            self.visit(node.test)
            self._visit_all(node.body)
        self._visit_all(node.orelse)

    def _visit_comprehension(self, node: ast.AST) -> None:
        with self._enter(loop=1):
            self.generic_visit(node)

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_DictComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            self.calls[func.id].append(self._site())
            called = func.id
        else:
            self.visit(func)
            called = func.attr if isinstance(func, ast.Attribute) else None
        with self._enter(concurrent=int(called in _CONCURRENT_CALLS)):
            self._visit_all([*node.args, *node.keywords])

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.refs[node.id].append(self._site())


def _scope_contexts(found: _Collector) -> dict[_Scope, _Context]:
    """The context each scope's body runs in, as seen from the module.

    A function's context is the union of the contexts of every call to it. Scopes the analysis
    can't follow, functions passed around by reference, and recursive functions get the unknown
    context. Computed to a fixed point, so helpers calling helpers inherit through the chain.
    """
    contexts: dict[_Scope, _Context] = {None: _Context()}
    for scope in found.opaque:
        contexts[scope] = _UNKNOWN
    for name, defs in found.defs.items():
        for scope in defs:
            contexts.setdefault(scope, _UNKNOWN if found.refs.get(name) else _Context())
    for scope in _recursive_scopes(found):
        contexts[scope] = contexts[scope] | _Context(in_loop=True)

    changed = True
    while changed:
        changed = False
        for name, defs in found.defs.items():
            for site in found.calls.get(name, []):
                incoming = contexts[site.scope] | _Context(site.in_loop, site.concurrent)
                for scope in defs:
                    updated = contexts[scope] | incoming
                    if updated != contexts[scope]:
                        contexts[scope] = updated
                        changed = True
    return contexts


def _recursive_scopes(found: _Collector) -> set[ast.AST]:
    """Local functions that can reach themselves through calls by name."""
    callees: dict[_Scope, set[ast.AST]] = defaultdict(set)
    for name, defs in found.defs.items():
        for site in found.calls.get(name, []):
            callees[site.scope].update(defs)

    recursive: set[ast.AST] = set()
    for start in {scope for defs in found.defs.values() for scope in defs}:
        seen: set[ast.AST] = set()
        stack = list(callees[start])
        while stack:
            scope = stack.pop()
            if scope is start:
                recursive.add(start)
                break
            if scope not in seen:
                seen.add(scope)
                stack.extend(callees[scope])
    return recursive
