from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Classe parente de tous les modèles SQLAlchemy (Alembic lit Base.metadata)."""
