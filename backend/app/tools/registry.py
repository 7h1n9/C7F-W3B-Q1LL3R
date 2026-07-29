import hashlib
import json
from pathlib import Path

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    create_model,
)

from app.core.exceptions import DomainError


class ToolDefinition(BaseModel):
    name: str
    display_name: str
    category: str
    description: str
    risk_level: str = "low"
    enabled: bool = True
    parameters: dict[str, dict] = Field(default_factory=dict)
    limits: dict[str, int] = Field(default_factory=dict)
    permissions: dict[str, bool] = Field(default_factory=dict)

    def schema_hash(self) -> str:
        payload = {
            "name": self.name,
            "parameters": self.parameters,
            "limits": self.limits,
            "permissions": self.permissions,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def validate_arguments(self, arguments: dict) -> dict:
        """Validate invocation input from the declarative YAML parameter schema."""
        type_map = {"string": StrictStr, "integer": StrictInt, "object": dict, "array": list, "boolean": StrictBool}
        fields: dict[str, tuple[object, object]] = {}
        for name, specification in self.parameters.items():
            declared_type = type_map.get(specification.get("type"))
            if declared_type is None:
                raise DomainError(
                    "TOOL_DEFINITION_INVALID",
                    "Tool definition has an unsupported parameter type.",
                    {"tool": self.name, "parameter": name},
                    500,
                )
            fields[name] = (declared_type, ... if specification.get("required", False) else None)
        argument_model = create_model(
            f"{self.name.title()}Arguments", __config__=ConfigDict(extra="forbid"), **fields
        )
        try:
            validated = argument_model.model_validate(arguments).model_dump(exclude_none=True)
            invalid_enum = {}
            for name, value in validated.items():
                specification = self.parameters.get(name) or {}
                if specification.get("enum") and value not in specification["enum"]:
                    invalid_enum[name] = value
            if invalid_enum:
                raise DomainError(
                    "TOOL_INVALID_ARGUMENT",
                    "Tool argument is outside the declared enum.",
                    {"tool": self.name, "unknown": invalid_enum, "expected_schema": self.parameters},
                    422,
                )
                self._validate_nested(specification, value, name)
            return validated
        except ValidationError as error:
            raise DomainError(
                "TOOL_INVALID_ARGUMENT",
                "Tool arguments do not match the declared schema.",
                {"tool": self.name, "errors": error.errors()},
                422,
            ) from error

    @classmethod
    def _validate_nested(cls, specification: dict, value: object, path: str) -> None:
        """Apply the useful nested subset of the declarative schema too."""
        if not isinstance(specification, dict):
            return
        properties = specification.get("properties") if isinstance(value, dict) else None
        if isinstance(properties, dict):
            required = specification.get("required") or []
            missing = [name for name in required if name not in value]
            unknown = [] if specification.get("additionalProperties", True) else [name for name in value if name not in properties]
            if missing or unknown:
                raise DomainError("TOOL_INVALID_ARGUMENT", "Nested tool arguments do not match the declared schema.", {"path": path, "missing": missing, "unknown": unknown}, 422)
            for name, child in properties.items():
                if name in value:
                    cls._validate_nested(child, value[name], f"{path}.{name}")
        if specification.get("type") == "array" and isinstance(value, list) and isinstance(specification.get("items"), dict):
            for index, item in enumerate(value):
                cls._validate_nested(specification["items"], item, f"{path}[{index}]")
        declared = specification.get("type")
        strict = {"string": StrictStr, "integer": StrictInt, "boolean": StrictBool, "object": dict, "array": list}.get(declared)
        if strict is not None and not isinstance(value, strict):
            raise DomainError("TOOL_INVALID_ARGUMENT", "Nested tool argument has the wrong type.", {"path": path, "expected": declared}, 422)


def load_tool_definitions(root: Path | None = None) -> dict[str, ToolDefinition]:
    """Load declarative tools from the repository, independent of process cwd."""
    root = root or Path(__file__).resolve().parents[3] / "configs" / "tools"
    return {
        definition.name: definition
        for path in root.glob("*.yaml")
        for definition in [
            ToolDefinition.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        ]
    }
