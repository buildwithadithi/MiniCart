import json
import subprocess
import sys
from pathlib import Path

from conftest import (
    PROJECT_ROOT,
    apply_migration,
    connect_to_database,
    create_database,
    execute_psql,
)


EXPECTED_TABLES = {
    "auth": {"users"},
    "product": {"products", "inventory"},
    "order": {"orders", "order_items"},
    "payment": {"payments"},
}
AUTH_MIGRATION = (
    PROJECT_ROOT / "auth-service" / "migrations" / "0001_create_users.sql"
)
ORDER_MIGRATIONS = (
    PROJECT_ROOT / "order-service" / "migrations" / "0001_add_payment_details.sql",
    PROJECT_ROOT / "order-service" / "migrations" / "0002_add_user_id.sql",
)


def _tables_in_database(cluster, database: str) -> set[str]:
    with connect_to_database(cluster, database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                """
            )
            return {row[0] for row in cursor.fetchall()}


def test_compose_configures_postgres_database_initialization():
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    configuration = json.loads(result.stdout)
    postgres = configuration["services"]["postgres"]
    environment = postgres["environment"]
    assert postgres["image"] == "postgres:17"
    assert environment["POSTGRES_DB"] == "minicart_auth_db"
    assert environment["MINICART_PRODUCT_DB"] == "minicart_product_db"
    assert environment["MINICART_ORDER_DB"] == "minicart_order_db"
    assert environment["MINICART_PAYMENT_DB"] == "minicart_payment_db"
    assert any(
        volume["type"] == "bind"
        and Path(volume["source"])
        == PROJECT_ROOT / "docker" / "postgres" / "init-databases.sql"
        and volume["target"]
        == "/docker-entrypoint-initdb.d/10-create-service-databases.sql"
        and volume["read_only"]
        for volume in postgres["volumes"]
    )


def test_database_init_script_creates_four_accessible_databases_and_is_idempotent(
    postgres_cluster,
):
    database_names = set(postgres_cluster["databases"].values())
    init_sql = (
        PROJECT_ROOT / "docker" / "postgres" / "init-databases.sql"
    ).read_text(encoding="utf-8")
    environment = {
        f"MINICART_{service.upper()}_DB": database
        for service, database in postgres_cluster["databases"].items()
        if service != "auth"
    }

    execute_psql(
        postgres_cluster,
        postgres_cluster["databases"]["auth"],
        init_sql,
        environment=environment,
    )

    with connect_to_database(
        postgres_cluster, postgres_cluster["databases"]["auth"]
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT datname FROM pg_database WHERE datname = ANY(%s)",
                (list(database_names),),
            )
            assert {row[0] for row in cursor.fetchall()} == database_names

    for database in database_names:
        with connect_to_database(postgres_cluster, database) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                assert cursor.fetchone()[0] == database


def test_sqlalchemy_initializer_creates_owned_tables_and_is_idempotent(
    postgres_cluster, initializer_environment
):
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "initialize_databases.py"],
            cwd=PROJECT_ROOT,
            env=initializer_environment,
            capture_output=True,
            check=False,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, (
            f"Database initialization failed:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    for service, expected_tables in EXPECTED_TABLES.items():
        database = postgres_cluster["databases"][service]
        assert _tables_in_database(postgres_cluster, database) == expected_tables


def test_auth_migration_creates_users_on_a_fresh_database_and_is_idempotent(
    postgres_cluster,
):
    database = f"{postgres_cluster['databases']['auth']}_migration"
    create_database(postgres_cluster, database)

    apply_migration(postgres_cluster, database, AUTH_MIGRATION)
    apply_migration(postgres_cluster, database, AUTH_MIGRATION)

    assert _tables_in_database(postgres_cluster, database) == {"users"}
    with connect_to_database(postgres_cluster, database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'users'
                """
            )
            assert {row[0] for row in cursor.fetchall()} == {
                "id",
                "name",
                "email",
                "password_hash",
                "created_at",
            }
            cursor.execute(
                """
                SELECT count(*)
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND tablename = 'users'
                  AND indexdef ILIKE '%UNIQUE%'
                  AND indexdef ILIKE '%email%'
                """
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == 1


def test_order_migrations_add_columns_and_index_to_fresh_baseline_database(
    postgres_cluster,
):
    database = f"{postgres_cluster['databases']['order']}_migration"
    create_database(postgres_cluster, database)
    execute_psql(
        postgres_cluster,
        database,
        """
        CREATE TABLE orders (
            id SERIAL PRIMARY KEY,
            total_amount NUMERIC(12, 2) NOT NULL,
            status VARCHAR(32) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    )

    for migration in ORDER_MIGRATIONS:
        apply_migration(postgres_cluster, database, migration)
    for migration in ORDER_MIGRATIONS:
        apply_migration(postgres_cluster, database, migration)

    with connect_to_database(postgres_cluster, database) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'orders'
                """
            )
            assert {row[0] for row in cursor.fetchall()} == {
                "id",
                "total_amount",
                "status",
                "created_at",
                "payment_id",
                "payment_status",
                "user_id",
            }
            cursor.execute(
                """
                SELECT count(*)
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND tablename = 'orders'
                  AND indexname = 'ix_orders_user_id'
                """
            )
            assert cursor.fetchone()[0] == 1
            cursor.execute(
                """
                SELECT count(*)
                FROM pg_constraint
                WHERE conrelid = 'orders'::regclass AND contype = 'f'
                """
            )
            assert cursor.fetchone()[0] == 0

