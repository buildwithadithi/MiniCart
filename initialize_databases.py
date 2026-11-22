import subprocess
import sys
from textwrap import dedent
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SCHEMA_INITIALIZERS = (
    (
        "Authentication Service",
        PROJECT_ROOT / "auth-service",
        "auth_models",
        "auth_database",
        ("users",),
    ),
    (
        "Product Service",
        PROJECT_ROOT / "product-service",
        "models",
        "database",
        ("products", "inventory"),
    ),
    (
        "Order Service",
        PROJECT_ROOT / "order-service",
        "models",
        "database",
        ("orders", "order_items"),
    ),
)


def _initializer_code(
    models_module: str, database_module: str, expected_tables: tuple[str, ...]
) -> str:
    return dedent(
        f"""
        import {models_module}
        from sqlalchemy import inspect
        from {database_module} import Base, engine

        Base.metadata.create_all(engine)
        tables = set(inspect(engine).get_table_names())
        missing = set({expected_tables!r}) - tables
        if missing:
            raise RuntimeError(f"Missing expected tables: {{sorted(missing)}}")
        print("Connected to", engine.url.database, "tables:", sorted(tables))
        """
    )


def main() -> None:
    for (
        service_name,
        service_root,
        models_module,
        database_module,
        tables,
    ) in SCHEMA_INITIALIZERS:
        print(
            f"Creating {service_name} schema in its configured database...",
            flush=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-c",
                _initializer_code(models_module, database_module, tables),
            ],
            cwd=service_root,
            check=True,
        )
        print(f"{service_name} schema is ready.", flush=True)


if __name__ == "__main__":
    main()
