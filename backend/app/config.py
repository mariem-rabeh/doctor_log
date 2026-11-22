"""Configuration de l'application.

Ordre de priorité : variables d'environnement > fichier `.env` à la racine du dépôt >
valeurs par défaut ci-dessous. Aucune valeur secrète ne doit être écrite dans le code.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> parents[2] = racine du dépôt
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",  # chemin absolu : marche quel que soit le dossier courant
        env_file_encoding="utf-8",
        extra="ignore",  # les variables du .env non utilisées ici (ex. POSTGRES_*) sont ignorées
    )

    database_url: str = "postgresql+psycopg://doctor_log:doctor_log@localhost:5432/doctor_log"
    llm_provider: str = "anthropic"  # "anthropic" (vrai modèle) ou "fake" (sans réseau)
    llm_model: str = ""  # vide = modèle par défaut du fournisseur (claude-opus-5-5)
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_api_key: SecretStr = SecretStr("")
    llm_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)

    prometheus_url: str = "http://localhost:9090"
    jaeger_url: str = "http://localhost:16686"

    datasets_dir: Path = REPO_ROOT / "datasets"

    # Origines autorisées à appeler l'API depuis un navigateur (le frontend Vite en dev).
    # Dans .env : CORS_ORIGINS=["http://localhost:5173","http://localhost:3000"]
    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    """Settings lus une seule fois puis réutilisés (aussi utilisable comme dépendance FastAPI)."""
    return Settings()
