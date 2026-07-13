from app.services.flags import FlagService


def test_default_flag_pattern_does_not_cross_serialized_json_boundaries() -> None:
    content = r'''flag{e78\"\n \"facts\": {\"body\": \"flag{e781e4bb6b7b46024dd7d0d4e763abd7}\"}'''
    assert FlagService._extract_matches(r"flag\{[^}]+\}", content) == [
        "flag{e781e4bb6b7b46024dd7d0d4e763abd7}"
    ]


def test_flag_extraction_deduplicates_clean_candidates() -> None:
    content = "flag{test} flag{test}"
    assert FlagService._extract_matches(r"flag\{[^}]+\}", content) == ["flag{test}"]


def test_malformed_cross_boundary_candidate_is_not_displayable() -> None:
    assert not FlagService._is_displayable(
        'flag{e78" facts: {"body": "flag{test}"}', r"flag\{[^}]+\}"
    )
