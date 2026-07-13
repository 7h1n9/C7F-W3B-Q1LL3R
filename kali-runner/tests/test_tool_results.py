from app.executors.http_executor import _extract_body


def test_http_extract_collects_html_facts_without_credentials_in_plain_model_fields() -> None:
    facts = _extract_body(
        '<title>Login</title><!-- user=ctf_test --><form method="POST"><input name="user"><input type="hidden" name="csrf"></form><a href="/admin">admin</a>',
        "text/html",
    )
    assert facts["html_title"] == "Login"
    assert facts["html_comments"] == ["user=ctf_test"]
    assert facts["parameter_names"] == ["user", "csrf"]
    assert facts["form_actions"] == [""]


def test_http_extract_detects_json_keys_and_flag_candidates() -> None:
    facts = _extract_body('{"token":"redacted","flag":"flag{test}"}', "application/json")
    assert facts["json_keys"] == ["token", "flag"]
    assert facts["suspected_credentials"] == ["token"]
    assert facts["suspected_flags"] == ["flag{test}"]
