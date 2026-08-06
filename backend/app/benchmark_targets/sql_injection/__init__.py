"""The intentionally easy SQL Injection golden-path target."""

from app.benchmark_targets.sql_injection.app import app, create_app

__all__ = ["app", "create_app"]
