import os

from dotenv import load_dotenv
from sqlalchemy import URL, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

db_password = os.getenv("DB_PASSWORD")
if not db_password:
    raise RuntimeError("DB_PASSWORD must be set in the environment or .env file.")

database_url = URL.create(
    drivername="postgresql+psycopg2",
    username=os.getenv("DB_USER", "minicart_admin"),
    password=db_password,
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", "5432")),
    database=os.getenv("PAYMENT_DB_NAME", "minicart_payment_db"),
)

engine = create_engine(database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass
