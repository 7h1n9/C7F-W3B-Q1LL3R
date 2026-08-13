from app.solver.muteki.titler import clean_title, fallback_title


def test_fallback_title_is_prompt_derived_and_bounded() -> None:
    title = fallback_title("请分析企业合同审核平台并寻找可验证的安全缺陷")

    assert title
    assert len(title) <= 48
    assert len(title.split()) <= 6


def test_clean_title_discards_model_refusal_and_multiline_output() -> None:
    assert clean_title("I cannot help with this\nAnother line", "设备报修工单平台") == "设备报修工单平台"
    assert clean_title('"合同审核平台"\nextra', "fallback") == "合同审核平台"

