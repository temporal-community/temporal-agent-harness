# ABOUTME: agent_schema(cls) — one JSON document describing an agent's typed surface: every
# @agent.accepts handler's input and output model and every agent.state(...) declaration,
# with all their models in one shared $defs. Pure reflection over the class (no workflow is
# started), so it is what client codegen reads. protocol_schema() is the same for the event
# stream every agent publishes.

"""An agent's handlers and declared state, and the event protocol, as JSON Schema."""

from __future__ import annotations

import importlib
import json
import os
import sys
from typing import Any

from pydantic import BaseModel, TypeAdapter
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode, models_json_schema
from pydantic_core import core_schema
from temporalio import workflow

from temporal_agent_harness.harness.agent_protocol.events import AgentEvent
from temporal_agent_harness.harness.agent_workflow import agent_handlers
from temporal_agent_harness.harness.state import declared_states

__all__ = [
    "agent_schema",
    "dump_agent_schema",
    "dump_protocol_schema",
    "load_agent_class",
    "protocol_schema",
]

_REF_TEMPLATE = "#/$defs/{model}"


class _WireJsonSchema(GenerateJsonSchema):
    """Serialization-mode schemas describe what ``model_dump(mode="json")`` emits.

    That dump includes every field, defaulted or not, so every model field is required in
    serialization mode. Pydantic's default marks defaulted fields optional there too, which
    would type a client's view of a reply or a state document as possibly missing fields it
    always receives.
    """

    def field_is_required(
        self,
        field: core_schema.ModelField | core_schema.DataclassField | core_schema.TypedDictField,
        total: bool,
    ) -> bool:
        if self.mode == "serialization" and field["type"] != "typed-dict-field":
            return field.get("serialization_exclude_if") is None
        return super().field_is_required(field, total)


def agent_schema(cls: type) -> dict[str, Any]:
    """The typed surface of the agent class ``cls``, as one JSON-serializable document::

        {
          "agent": "TicTacToeAgent",                    # the workflow type name
          "source": "examples.tictactoe.workflow:TicTacToeAgentWorkflow",
          "handlers": {"play": {"description", "mid_turn", "input", "output"}, ...},
          "states": {"board": {"$ref": "#/$defs/Board"}},
          "$defs": {...},
        }

    Handler inputs are generated in pydantic's validation mode (a field with a default may be
    omitted by a sender); handler outputs and states in serialization mode (what arrives on
    the wire, where every field is present). A model used both ways whose two schemas differ
    appears twice in ``$defs``, as ``Name-Input`` and ``Name-Output``.

    Handlers are ordered by name and states by declaration order. Raises ``ValueError`` if
    two distinct models share a class name, since the generated client types use plain names.
    """
    handlers = agent_handlers(cls)
    states = declared_states(cls)

    models: list[tuple[type[BaseModel], JsonSchemaMode]] = []
    for handler in handlers.values():
        models.append((handler.input_type, "validation"))
        models.append((handler.output_type, "serialization"))
    for decl in states.values():
        models.append((decl.state_type, "serialization"))

    refs, top = models_json_schema(
        list(dict.fromkeys(models)),
        ref_template=_REF_TEMPLATE,
        schema_generator=_WireJsonSchema,
    )
    defs: dict[str, Any] = top.get("$defs", {})
    _reject_name_collisions(cls, [model for model, _ in models], defs)

    definition = workflow._Definition.from_class(cls)
    return {
        "agent": definition.name if definition is not None else cls.__name__,
        "source": f"{cls.__module__}:{cls.__qualname__}",
        "handlers": {
            name: {
                "description": handler.description,
                "mid_turn": handler.mid_turn.value,
                "input": refs[(handler.input_type, "validation")],
                "output": refs[(handler.output_type, "serialization")],
            }
            for name, handler in sorted(handlers.items())
        },
        "states": {
            state_id: refs[(decl.state_type, "serialization")]
            for state_id, decl in states.items()
        },
        "$defs": dict(sorted(defs.items())),
    }


def protocol_schema() -> dict[str, Any]:
    """The event stream's wire types, as one JSON-serializable document::

        {
          "event": {"$ref": "#/$defs/AgentEvent"},        # the envelope
          "stream_items": [{"$ref": "#/$defs/MessageAccepted"}, ...],
          "$defs": {...},
        }

    ``stream_items`` lists the payloads ``AgentEvent.event`` can hold, in the order of the
    ``AgentStreamItem`` union. Serialization mode throughout: this is what is read, never
    written, by a client.
    """
    top = TypeAdapter(AgentEvent).json_schema(
        mode="serialization", ref_template=_REF_TEMPLATE, schema_generator=_WireJsonSchema
    )
    defs: dict[str, Any] = top.pop("$defs")
    defs[AgentEvent.__name__] = top
    return {
        "event": {"$ref": _REF_TEMPLATE.format(model=AgentEvent.__name__)},
        "stream_items": top["properties"]["event"]["oneOf"],
        "$defs": dict(sorted(defs.items())),
    }


def _reject_name_collisions(
    cls: type, models: list[type[BaseModel]], defs: dict[str, Any]
) -> None:
    # Pydantic keeps a model's plain class name as its $defs key unless two different models
    # share it. Used in the same mode, it falls back to module-qualified keys (`pkg__a__Item`);
    # used in different modes, it names them `Item-Input` / `Item-Output` as if they were one.
    collisions = sorted(key for key in defs if "__" in key)
    by_name: dict[str, set[str]] = {}
    for model in models:
        by_name.setdefault(model.__name__, set()).add(f"{model.__module__}.{model.__qualname__}")
    collisions += sorted(p for paths in by_name.values() if len(paths) > 1 for p in paths)
    if collisions:
        raise ValueError(
            f"{cls.__name__}: several different models share a class name, so their schemas "
            f"could only be told apart by module path ({', '.join(collisions)}). Rename one "
            "so each model an agent exposes has a unique class name."
        )


def load_agent_class(target: str) -> type:
    """Import the agent class named by ``module.path:ClassName``.

    The current directory is importable, as it is for ``python -m``, so a project's own agents
    resolve when run from the project root.
    """
    module_name, sep, qualname = target.partition(":")
    if not sep or not module_name or not qualname:
        raise ValueError(f"expected module.path:ClassName, got {target!r}")
    if "" not in sys.path and os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    try:
        obj: Any = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        if e.name != module_name and not module_name.startswith(f"{e.name}."):
            raise
        raise ValueError(f"no module named {module_name!r}") from None
    for part in qualname.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise ValueError(f"{module_name} has no attribute {qualname!r}") from None
    if not (isinstance(obj, type) and workflow._Definition.from_class(obj) is not None):
        raise ValueError(f"{target} is not a @workflow.defn agent class")
    return obj


def dump_agent_schema(cls: type) -> str:
    """:func:`agent_schema` as stable, diffable JSON text."""
    return json.dumps(agent_schema(cls), indent=2, ensure_ascii=False) + "\n"


def dump_protocol_schema() -> str:
    """:func:`protocol_schema` as stable, diffable JSON text."""
    return json.dumps(protocol_schema(), indent=2, ensure_ascii=False) + "\n"
