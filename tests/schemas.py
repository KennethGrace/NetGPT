"""
The published schema (schema/netgpt.schema.json), and a check that what NetGPT sent
is the record it says, for the tests of each face that sends records.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

SCHEMA = Path(__file__).parents[1] / "schema" / "netgpt.schema.json"


def published() -> dict[str, Any]:
    return json.loads(SCHEMA.read_text())


def check(record: str, value: object, shape: str = "one") -> None:
    """Fail unless the value is the record (or a list or collection of them) as the published schema has it."""
    defs = published()["$defs"]
    assert record in defs, f"{record} is not in the contract"
    one = {"$ref": f"#/$defs/{record}"}
    expected = {
        "one": one,
        "list": {"type": "object", "properties": {"result": {"type": "array", "items": one}}, "required": ["result"]},
        "collection": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": one},
                "errors": {"type": "array", "items": {"$ref": "#/$defs/DeviceError"}},
            },
            "required": ["items", "errors"],
        },
    }[shape]
    errors = sorted(Draft202012Validator({**expected, "$defs": defs}).iter_errors(value), key=str)
    found = "; ".join(f"{list(error.absolute_path)}: {error.message}" for error in errors[:5])
    assert not errors, f"{record} doesn't match the schema: {found}"
