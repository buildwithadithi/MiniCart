import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = "postgres:17"
POSTGRES_USER = "minicart_test_admin"
POSTGRES_PASSWORD = "minicart_test_password"


def _docker(*arguments: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        check=False,
        text=True,
        input=input_text,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Docker command failed: docker {' '.join(arguments)}\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def execute_psql(
    cluster: dict[str, object],
    database: str,
    sql: str,
    *,
    environment: dict[str, str] | None = None,
) -> str:
    arguments = ["exec", "--interactive"]
    for key, value in (environment or {}).items():
        arguments.extend(("--env", f"{key}={value}"))
    arguments.extend([
        str(cluster["container"]),
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        POSTGRES_USER,
        "-d",
        database,
    ])
    return _docker(*arguments, input_text=sql)


def connect_to_database(cluster: dict[str, object], database: str):
    import psycopg2

    return psycopg2.connect(
        host="127.0.0.1",
        port=cluster["port"],
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        dbname=database,
        connect_timeout=5,
    )


@pytest.fixture(scope="module")
def postgres_cluster():
    suffix = uuid4().hex[:12]
    container_name = f"minicart-db-tests-{suffix}"
    databases = {
        "auth": f"mc_{suffix}_auth",
        "product": f"mc_{suffix}_product",
        "order": f"mc_{suffix}_order",
        "payment": f"mc_{suffix}_payment",
    }
    _docker(
        "run",
        "--detach",
        "--name",
        container_name,
        "--publish",
        "127.0.0.1::5432",
        "--tmpfs",
        "/var/lib/postgresql/data:rw,size=256m",
        "--env",
        f"POSTGRES_USER={POSTGRES_USER}",
        "--env",
        f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
        "--env",
        f"POSTGRES_DB={databases['auth']}",
        POSTGRES_IMAGE,
    )

    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state = _docker(
                "inspect",
                "--format",
                "{{.State.Status}}",
                container_name,
            )
            if state != "running":
                logs = _docker("logs", container_name)
                raise RuntimeError(
                    f"Temporary PostgreSQL exited with state {state}:\n{logs}"
                )
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "pg_isready",
                    "-U",
                    POSTGRES_USER,
                    "-d",
                    databases["auth"],
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            logs = _docker("logs", container_name)
            raise RuntimeError(
                f"Temporary PostgreSQL was not ready within 90 seconds:\n{logs}"
            )

        published_port = _docker("port", container_name, "5432/tcp")
        host_port = int(published_port.rsplit(":", maxsplit=1)[1])
        cluster = {
            "container": container_name,
            "port": host_port,
            "databases": databases,
        }

        init_environment = {
            "MINICART_PRODUCT_DB": databases["product"],
            "MINICART_ORDER_DB": databases["order"],
            "MINICART_PAYMENT_DB": databases["payment"],
        }
        init_sql = (
            PROJECT_ROOT / "docker" / "postgres" / "init-databases.sql"
        ).read_text(encoding="utf-8")
        execute_psql(
            cluster,
            databases["auth"],
            init_sql,
            environment=init_environment,
        )
        yield cluster
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container_name],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )


def create_database(cluster: dict[str, object], database: str) -> None:
    connection = connect_to_database(cluster, cluster["databases"]["auth"])
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE "{database}"')
    finally:
        connection.close()


def apply_migration(cluster: dict[str, object], database: str, path: Path) -> None:
    execute_psql(
        cluster,
        database,
        path.read_text(encoding="utf-8"),
    )


@pytest.fixture
def initializer_environment(postgres_cluster):
    environment = os.environ.copy()
    environment.update(
        {
            "DB_HOST": "127.0.0.1",
            "DB_PORT": str(postgres_cluster["port"]),
            "DB_USER": POSTGRES_USER,
            "DB_PASSWORD": POSTGRES_PASSWORD,
            "AUTH_DB_NAME": postgres_cluster["databases"]["auth"],
            "PRODUCT_DB_NAME": postgres_cluster["databases"]["product"],
            "ORDER_DB_NAME": postgres_cluster["databases"]["order"],
            "PAYMENT_DB_NAME": postgres_cluster["databases"]["payment"],
        }
    )
    return environment
