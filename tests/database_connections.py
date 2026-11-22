import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import URL, create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

SERVICE_DATABASES = {
    "auth": ("AUTH_DB_NAME", "minicart_auth_db"),
    "product": ("PRODUCT_DB_NAME", "minicart_product_db"),
    "order": ("ORDER_DB_NAME", "minicart_order_db"),
}


def create_service_database_connections():
    password = os.getenv("DB_PASSWORD")
    if not password:
        raise RuntimeError("DB_PASSWORD must be set in the project .env file.")

    connections = {}
    for service, (database_setting, default_database) in SERVICE_DATABASES.items():
        engine = create_engine(
            URL.create(
                drivername="postgresql+psycopg2",
                username=os.getenv("DB_USER", "minicart_admin"),
                password=password,
                host=os.getenv("DB_HOST", "localhost"),
                port=int(os.getenv("DB_PORT", "5432")),
                database=os.getenv(database_setting, default_database),
            ),
            pool_pre_ping=True,
        )
        connections[service] = {
            "engine": engine,
            "sessions": sessionmaker(
                bind=engine, autoflush=False, expire_on_commit=False
            ),
        }
    return connections
