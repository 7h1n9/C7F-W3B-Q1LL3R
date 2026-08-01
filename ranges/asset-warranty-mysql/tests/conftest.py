import os

import pytest
import requests


@pytest.fixture(scope="session")
def target_url():
    return os.environ.get("TARGET_URL", "http://asset-warranty-web:5000")


@pytest.fixture(scope="session")
def root_db_config():
    return {
        "host": os.environ.get("MYSQL_HOST", "asset-warranty-db"),
        "port": int(os.environ.get("MYSQL_PORT", "3306")),
        "user": "root",
        "password": os.environ["MYSQL_ROOT_PASSWORD"],
        "database": "asset_warranty",
        "charset": "utf8mb4",
        "autocommit": True,
    }


def post_check(target_url, asset_no="PC-2026-013", department="OPS"):
    response = requests.post(
        f"{target_url}/api/warranty/check",
        json={"asset_no": asset_no, "department": department},
        timeout=5,
    )
    response.raise_for_status()
    return response.json()
