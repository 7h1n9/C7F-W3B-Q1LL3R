from pathlib import Path

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
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

    def validate_arguments(self, arguments: dict) -> dict:
        """Validate invocation input from the declarative YAML parameter schema."""
        type_map = {"string": StrictStr, "integer": StrictInt, "object": dict, "array": list}
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
            return argument_model.model_validate(arguments).model_dump(exclude_none=True)
        except ValidationError as error:
            raise DomainError(
                "TOOL_INVALID_ARGUMENT",
                "Tool arguments do not match the declared schema.",
                {"tool": self.name, "errors": error.errors()},
                422,
            ) from error


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
