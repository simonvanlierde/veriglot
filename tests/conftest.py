import json
from importlib import resources
from typing import TYPE_CHECKING

import pytest
from jsonschema import Draft202012Validator

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def findings_schema() -> Callable[..., None]:
    """Validate data against the committed findings schema, or against one of its ``$defs``."""
    schema = json.loads(resources.files("veriglot").joinpath("findings.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)

    def validate(data: object, part: str | None = None) -> None:
        target = {"$defs": schema["$defs"], "$ref": f"#/$defs/{part}"} if part else schema
        Draft202012Validator(target).validate(data)

    return validate
