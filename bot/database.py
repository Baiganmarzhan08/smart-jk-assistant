from os import getenv

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import ArgumentError

from bot.config import load_project_env


def database_url(*, required: bool = True) -> URL | None:
    load_project_env()
    value = getenv("DATABASE_URL", "").strip()
    if not value:
        if required:
            raise ValueError("Укажите DATABASE_URL в файле .env.")
        return None
    try:
        url = make_url(value)
        if url.drivername not in {"postgresql", "postgresql+psycopg"} or not url.database:
            raise ValueError
        return url.set(drivername="postgresql+psycopg")
    except (ArgumentError, ValueError):
        raise ValueError("Проверьте DATABASE_URL. Укажите адрес PostgreSQL с именем базы.") from None


def create_database_engine(url: URL) -> Engine:
    return create_engine(url, pool_pre_ping=True, hide_parameters=True, connect_args={"connect_timeout": 5})
