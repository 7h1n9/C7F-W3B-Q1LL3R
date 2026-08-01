import requests

from conftest import post_check


def test_target_uses_real_mysql_connection(target_url):
    response = requests.get(f"{target_url}/health", timeout=5)
    response.raise_for_status()
    body = response.json()
    assert body["status"] == "ready"
    assert body["dbms"] == "mysql"
    assert body["database_name"] == "asset_warranty"
    assert body["version"].startswith("8.4.")


def test_target_database_is_asset_warranty(target_url):
    assert requests.get(f"{target_url}/health", timeout=5).json()["database_name"] == "asset_warranty"


def test_valid_business_baseline(target_url):
    assert post_check(target_url) == {"matched": True, "message": "存在符合条件的保修记录"}


def test_invalid_business_baseline(target_url):
    assert post_check(target_url, asset_no="PC-2026-013X") ["matched"] is False
