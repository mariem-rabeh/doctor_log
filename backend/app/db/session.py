"""Connexion à PostgreSQL : moteur, fabrique de sessions et dépendance FastAPI."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    """Un seul moteur (et donc un seul pool de connexions) pour toute l'application."""
    # pool_pre_ping : vérifie qu'une connexion est vivante avant de la réutiliser.
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """Dépendance FastAPI : une session par requête, toujours refermée."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
