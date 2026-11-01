"""
The contract module names the records NetGPT sends outside itself, and writes
their JSON Schema: one document that other repositories (xspace, say) read
instead of NetGPT's Python.

The records are the views the page's map draws, charts and the map's traffic, and
the primitives the tools return. The schema is written from the pydantic models
that make them, in the shape NetGPT sends them, so it can't say anything the code
doesn't. schema/netgpt.schema.json is the repository's copy, and a test fails when
it and the code differ, so a change to the contract shows up in a diff. The
conversation the page streams is the page's own, and not part of the contract.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import GenerateJsonSchema, models_json_schema
from pydantic_core import core_schema

from netgpt import __version__
from netgpt.api import Refusal
from netgpt.forwarding import ForwardingPath
from netgpt.primitives import (
    ArpEntry,
    BgpSession,
    CommandOutput,
    DeviceError,
    DeviceInfo,
    Interface,
    InterfaceTraffic,
    LldpNeighbor,
    MacEntry,
    OspfInterface,
    OspfInterfaces,
    OspfNeighbor,
    PingResult,
    Route,
)
from netgpt.statistics import Chart, Traffic
from netgpt.topology import Topology
from netgpt.views import View

DIALECT = "https://json-schema.org/draft/2020-12/schema"

# What the page's map draws: a view, and the graph and the path it is made from.
VIEWS: tuple[type[BaseModel], ...] = (View, Topology, ForwardingPath)
# What the page charts, and the traffic the map draws on its links.
STATISTICS: tuple[type[BaseModel], ...] = (Chart, Traffic)
# What the API answers when it won't answer with a record.
REFUSALS: tuple[type[BaseModel], ...] = (Refusal,)
# What the tools return, by layer, as netgpt.primitives groups them.
PRIMITIVES: tuple[type[BaseModel], ...] = (
    DeviceInfo, DeviceError, CommandOutput, LldpNeighbor, Interface, InterfaceTraffic, MacEntry, ArpEntry, Route,
    BgpSession, OspfInterface, OspfInterfaces, OspfNeighbor, PingResult,
)
RECORDS = VIEWS + STATISTICS + REFUSALS + PRIMITIVES

DESCRIPTION = (
    "Every record NetGPT sends outside itself, in the shape it sends them: the views the page's map draws, charts and "
    "the map's traffic, the records the tools return, and the API's refusals. Each record is under $defs, by name. "
    "Every property is always sent; one that can be empty is null then. A tool that asks several devices returns "
    '{"items": [records], "errors": [DeviceError]}, so one device\'s failure leaves the rest of the answer.'
)


class _AsSent(GenerateJsonSchema):
    """
    Marks every property required: NetGPT sends each one, set or not, so a reader can
    count on finding it. Properties carry no title: pydantic's is the field's name
    title-cased, and type generators name a type after each one (json-schema-to-typescript
    made Device1 to Device10, and renamed the Interface record Interface2).
    """

    def field_is_required(
        self, field: core_schema.ModelField | core_schema.DataclassField | core_schema.TypedDictField, total: bool
    ) -> bool:
        if self.mode == "serialization":
            return field.get("serialization_exclude_if") is None
        return super().field_is_required(field, total)

    def field_title_should_be_set(self, schema: Any) -> bool:
        return False


def schema() -> dict[str, Any]:
    """The contract as one JSON Schema document, marked with the version of NetGPT that wrote it."""
    _, written = models_json_schema([(record, "serialization") for record in RECORDS], schema_generator=_AsSent)
    return {"$schema": DIALECT, "title": "NetGPT", "description": DESCRIPTION, "version": __version__, "$defs": written["$defs"]}


def text(document: dict[str, Any]) -> str:
    """The document as the repository keeps it."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    """Write the contract's JSON Schema."""
    parser = argparse.ArgumentParser(description="Write the JSON Schema of every record NetGPT sends outside itself.")
    parser.add_argument("--out", type=Path, help="The file to write, such as schema/netgpt.schema.json (default: standard output).")
    args = parser.parse_args()
    written = text(schema())
    if args.out is None:
        print(written, end="")
    else:
        args.out.write_text(written)
