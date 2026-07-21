from app.executors.http_executor import _extract_body, _request_kwargs, merge_url_query


def test_http_request_preserves_query_and_maps_structured_bodies() -> None:
    assert _request_kwargs({"query": None}) == {"params": None}
    assert _request_kwargs({"query": {"page": "2"}}) == {"params": {"page": "2"}}
    assert _request_kwargs({"query": {"id": ["1", "2"]}, "json": {"ok": True}}) == {
        "params": {"id": ["1", "2"]},
        "json": {"ok": True},
    }
    assert _request_kwargs({"form": {"user": "ctf"}}) == {
        "params": None,
        "data": {"user": "ctf"},
    }


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


def test_merge_url_query_preserves_and_replaces_explicitly() -> None:
    assert merge_url_query("http://target.test/search?q=test", None).endswith("q=test")
    assert merge_url_query("http://target.test/search?q=test", {"format": "json"}).endswith("q=test&format=json")
    assert merge_url_query("http://target.test/search?q=old", {"q": "new"}).endswith("q=new")
    assert merge_url_query("http://target.test/search", {"id": ["1", "2"]}).endswith("id=1&id=2")
