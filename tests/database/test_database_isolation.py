import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE_DATABASES = (
    pytest.param(
        "auth-service",
        "auth_database",
        "minicart_auth_db",
        ("users",),
        id="auth",
    ),
    pytest.param(
        "product-service",
        "database",
        "minicart_product_db",
        ("products", "inventory"),
        id="product",
    ),
    pytest.param(
        "order-service",
        "database",
        "minicart_order_db",
        ("orders", "order_items"),
        id="order",
    ),
)
ALL_SERVICE_DATABASES = {
    "minicart_auth_db",
    "minicart_product_db",
    "minicart_order_db",
}


def _inspect_application_database(service_directory: str, module_name: str) -> dict:
    service_root = PROJECT_ROOT / service_directory
    script = dedent(
        f"""
        import json
        import sys

        from sqlalchemy import inspect, text
        from {module_name} import engine

        try:
            with engine.connect() as connection:
                current_database = connection.execute(
                    text("SELECT current_database()")
                ).scalar_one()
            print(json.dumps({{
                "configured_database": engine.url.database,
                "current_database": current_database,
                "tables": sorted(inspect(engine).get_table_names()),
            }}))
        except Exception as error:
            print(
                f"Database inspection failed: {{type(error).__name__}}: {{error}}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=service_root,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"Could not inspect {service_directory}'s PostgreSQL configuration.\n"
        f"{result.stderr.strip() or result.stdout.strip()}"
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    ("service_directory", "module_name", "expected_database", "expected_tables"),
    SERVICE_DATABASES,
)
def test_service_uses_only_its_own_postgresql_database(
    service_directory: str,
    module_name: str,
    expected_database: str,
    expected_tables: tuple[str, ...],
):
    database_state = _inspect_application_database(service_directory, module_name)

    assert database_state["configured_database"] == expected_database
    assert database_state["current_database"] == expected_database
    assert database_state["current_database"] in ALL_SERVICE_DATABASES
    assert set(expected_tables) <= set(database_state["tables"])
    assert set(database_state["tables"]).isdisjoint(
        {
            "users",
            "products",
            "inventory",
            "orders",
            "order_items",
        }
        - set(expected_tables)
    )
