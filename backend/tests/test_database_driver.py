from __future__ import annotations

import pytest

from app.core import config


def test_asyncmy_driver_validation_reports_runtime_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_driver(name: str):
        if name == "asyncmy":
            error = ModuleNotFoundError("No module named 'asyncmy'")
            error.name = "asyncmy"
            raise error
        return config.importlib.import_module(name)

    monkeypatch.setattr(config.importlib, "import_module", missing_driver)

    with pytest.raises(RuntimeError, match=r"DATABASE_ASYNC_DRIVER_MISSING.*driver=asyncmy") as raised:
        config.validate_database_driver("mysql+asyncmy://user:pass@localhost:3307/db")

    assert "python_executable=" in str(raised.value)
    assert "database_url_scheme=mysql+asyncmy" in str(raised.value)


def test_database_url_rejects_non_async_mysql_scheme() -> None:
    with pytest.raises(ValueError, match=r"mysql\+asyncmy"):
        config.Settings(database_url="mysql://user:pass@localhost:3307/db")
