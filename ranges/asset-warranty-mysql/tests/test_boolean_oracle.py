from conftest import post_check


def test_boolean_true_false(target_url):
    for _ in range(2):
        assert post_check(target_url, department="OPS' AND (1=1) -- ")["matched"] is True
        assert post_check(target_url, department="OPS' AND (1=2) -- ")["matched"] is False


def test_random_arithmetic_predicates(target_url):
    assert post_check(target_url, department="OPS' AND ((7*9)=63) -- ")["matched"] is True
    assert post_check(target_url, department="OPS' AND ((13+29)=41) -- ")["matched"] is False
