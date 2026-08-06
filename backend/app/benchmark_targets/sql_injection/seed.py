"""Seed the SQL Injection golden-path database."""

from app.benchmark_targets.sql_injection.database import connect, seed


def main() -> None:
    connection = connect()
    try:
        seed(connection)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
