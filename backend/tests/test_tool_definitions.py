import pytest

from app.core.exceptions import DomainError
from app.tools.policy import enforce_tool_policy
from app.tools.registry import load_tool_definitions


def test_yaml_tool_definition_requires_declared_arguments() -> None:
    definitions = load_tool_definitions()
    with pytest.raises(DomainError) as error:
        definitions["http_request"].validate_arguments({"url": "http://challenge.local"})
    assert error.value.code == "TOOL_INVALID_ARGUMENT"


def test_file_search_has_no_path_requirement() -> None:
    definitions = load_tool_definitions()
    arguments = definitions["file_search"].validate_arguments({"query": "flag", "max_results": 5})
    enforce_tool_policy("file_search", arguments, ["challenge.local"])
