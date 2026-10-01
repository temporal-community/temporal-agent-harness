"""Generate a policy's Cedar action schema from an agent's host functions.

A Dogwood policy is written against a Cedar schema declaring one action per tool, with the
tool's arguments as ``context.input`` and its result as ``context.output``. Dogwood generates
that schema from an MCP tool manifest (``dogwood schema mcp``); this module writes the manifest
from the harness tools a ``code_mode_tool`` exposes — the same model-facing signatures the
script is type-checked against — so the schema and the calls the trace records cannot drift.

Dogwood's template puts every action in its ``Drupe`` namespace; the schema is renamed into the
agent's own, which is what the policy file then names (``Support::Action::"issue_refund"``).

Run it whenever a host function's signature changes::

    uv run python -m temporal_agent_harness.playpen.schema \\
        examples.playpen.store:STORE_TOOLS --namespace Support \\
        --dogwood "$DOGWOOD_CLI" -o examples/playpen/policies/support/schema.cedarschema
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, create_model

from temporal_agent_harness.harness.code_mode.stubs import resolve_hints

_TEMPLATE_NAMESPACE = "Drupe"


def mcp_manifest(tools: list[Callable[..., Awaitable[Any]]]) -> list[dict[str, Any]]:
    """An MCP ``tools/list`` manifest for ``tools``: name, description, input and output schemas."""
    manifest = []
    for tool in tools:
        hints = resolve_hints(tool)
        params: dict[str, Any] = {}
        for name, param in inspect.signature(tool).parameters.items():
            annotation = hints.get(name, param.annotation)
            if annotation is inspect.Parameter.empty:
                raise ValueError(f"{tool.__name__}: parameter {name!r} has no type annotation")
            default = ... if param.default is inspect.Parameter.empty else param.default
            params[name] = (annotation, default)
        returns = hints.get("return", inspect.Parameter.empty)
        if returns is inspect.Parameter.empty:
            raise ValueError(f"{tool.__name__}: no return annotation")
        manifest.append(
            {
                "name": tool.__name__,
                "description": inspect.getdoc(tool) or "",
                "inputSchema": _flatten(create_model(f"{tool.__name__}_input", **params).model_json_schema()),
                "outputSchema": _flatten(TypeAdapter(returns).json_schema()),
            }
        )
    return manifest


def cedar_schema(
    tools: list[Callable[..., Awaitable[Any]]], *, namespace: str, dogwood_cli: str
) -> str:
    """The Cedar action schema for ``tools``, in ``namespace``."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", namespace):
        raise ValueError(f"namespace {namespace!r} is not a Cedar identifier")
    generated = subprocess.run(
        [dogwood_cli, "schema", "mcp", "--manifest", "-"],
        input=json.dumps(mcp_manifest(tools)),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if f"namespace {_TEMPLATE_NAMESPACE} {{" not in generated:
        raise ValueError(f"dogwood's schema is not in the {_TEMPLATE_NAMESPACE} namespace")
    renamed = re.sub(rf"\b{_TEMPLATE_NAMESPACE}\b", namespace, generated)
    return renamed


def _flatten(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$ref``s and drop what the manifest does not use (titles, defaults).

    An optional field (``X | None``) becomes plain ``X`` and stays out of ``required``, which is
    how Dogwood reads an optional attribute."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
        if "anyOf" in node:
            options = [o for o in node["anyOf"] if o != {"type": "null"}]
            if len(options) != 1:
                raise ValueError(f"cannot express a union in a Cedar schema: {node['anyOf']}")
            return walk(options[0])
        return {
            k: walk(v) for k, v in node.items() if k not in ("$defs", "title", "default")
        }

    return walk(schema)


def _load_tools(spec: str) -> list[Callable[..., Awaitable[Any]]]:
    module_name, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit(f"tools must be given as module:attribute, got {spec!r}")
    return list(getattr(importlib.import_module(module_name), attr))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("tools", help="the host functions, as module:attribute")
    parser.add_argument("--namespace", required=True, help="the Cedar namespace for the actions")
    parser.add_argument("--dogwood", required=True, help="path to the dogwood CLI")
    parser.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    args = parser.parse_args(argv)
    schema = cedar_schema(
        _load_tools(args.tools), namespace=args.namespace, dogwood_cli=args.dogwood
    )
    if args.output is None:
        sys.stdout.write(schema)
    else:
        args.output.write_text(schema)


if __name__ == "__main__":
    main()
