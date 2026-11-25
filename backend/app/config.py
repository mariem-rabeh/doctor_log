"""Configuration de l'application.

Ordre de priorité : variables d'environnement > fichier `.env` à la racine du dépôt >
valeurs par défaut ci-dessous. Aucune valeur secrète ne doit être écrite dans le code.
"""

from functools import lru_cache
from pathlib import Path

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
    llm_provider: str = "gemini"  # "gemini", "groq" ou "fake" (sans réseau)
    llm_model: str = ""  # vide = modèle par défaut du fournisseur
    llm_api_key: SecretStr = SecretStr("")
    llm_timeout_seconds: float = Field(
        default=120.0, gt=0
    )  # les modèles qui « réfléchissent » sont lents
    llm_max_retries: int = Field(default=3, ge=0)

    prometheus_url: str = "http://localhost:9090"
    jaeger_url: str = "http://localhost:16686"

    datasets_dir: Path = REPO_ROOT / "datasets"
    logs_dir: Path = REPO_ROOT / "logs"  # logs JSON de la demo-app (incidents live)
    knowledge_dir: Path = REPO_ROOT / "knowledge"  # runbooks du RAG

    # Origines autorisées à appeler l'API depuis un navigateur (le frontend Vite en dev).
    # Dans .env : CORS_ORIGINS=["http://localhost:5173","http://localhost:3000"]
    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    """Settings lus une seule fois puis réutilisés (aussi utilisable comme dépendance FastAPI)."""
    return Settings()
