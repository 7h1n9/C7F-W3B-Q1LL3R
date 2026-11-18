"""Claude 代理的模型名解析回归测试。

背景（2026-10-21 实测）：``route_via_proxy=true`` 的 claude_cli 引擎在 PREPARE
阶段的体检**每次都失败**，而 codex_cli 正常。真因不在模型侧 —— 直接把
``claude-opus-5-5`` 打到中继返回 200 —— 而在名字翻译：Claude CLI 2.1.293
默认请求 ``claude-sonnet-4-6``，而 ``_OFFICIAL_CLAUDE_MODELS`` 当时是**逐个枚举**
（``claude-sonnet-5`` 等），没有这一代，于是走到"未知名字原样透传"分支，
中继回 ``There's an issue with the selected model``，代理把错误转给 CLI，
体检被判为 ``WORKER_HEALTHCHECK_FAILED``。

改用家族前缀后，前缀要覆盖 CLI 任何一代的命名；同时**不能**把非 Claude
名字（``gpt-4o``）也吞掉 —— 那种情况原样透传才能暴露真实配置错误。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.api.claude_proxy import _resolve_upstream_model


def _config(model_name: str = "claude-opus-5-5", aliases: dict | None = None):
    return SimpleNamespace(
        name="claude",
        model_name=model_name,
        capabilities_json={"model_aliases": aliases or {}},
    )


def test_cli_default_model_is_translated_to_the_configured_one() -> None:
    """CLI 2.1.293 的默认模型名必须被翻译，否则体检整轮失败。"""

    assert _resolve_upstream_model(_config(), "claude-sonnet-4-6") == "claude-opus-5-5"


def test_other_claude_generations_also_land_on_the_configured_model() -> None:
    config = _config()
    for requested in (
        "claude-opus-4-5",
        "claude-haiku-4-5",
        "claude-3-5-sonnet-latest",
        "claude-2.1",
    ):
        assert _resolve_upstream_model(config, requested) == "claude-opus-5-5", requested


def test_explicit_alias_still_wins_over_the_family_fallback() -> None:
    """运维把某个档位指到别的上游模型时，别名优先于家族回落。"""

    config = _config(aliases={"claude-opus-5": "claude-opus-5-5-cheap"})
    assert (
        _resolve_upstream_model(config, "claude-opus-5") == "claude-opus-5-5-cheap"
    )


def test_foreign_model_names_are_passed_through_unchanged() -> None:
    """非 Claude 名字不能被吞掉，否则真实的配置错误会被掩盖。"""

    assert _resolve_upstream_model(_config(), "gpt-4o") == "gpt-4o"


def test_the_configured_name_itself_is_idempotent() -> None:
    assert _resolve_upstream_model(_config(), "claude-opus-5-5") == "claude-opus-5-5"


def test_missing_requested_name_falls_back_to_the_configured_model() -> None:
    assert _resolve_upstream_model(_config(), None) == "claude-opus-5-5"
