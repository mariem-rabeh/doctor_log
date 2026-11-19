from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

ENV_VARS = (
    "DATABASE_URL",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_RETRIES",
    "PROMETHEUS_URL",
    "JAEGER_URL",
    "DATASETS_DIR",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isole chaque test des variables réellement présentes sur la machine."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults_without_env_file() -> None:
    settings = Settings(_env_file=None)

    assert settings.prometheus_url == "http://localhost:9090"
    assert settings.jaeger_url == "http://localhost:16686"
    assert settings.llm_max_retries == 3
    assert settings.datasets_dir.name == "datasets"


def test_loads_values_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_PROVIDER=anthropic\nLLM_MODEL=model-x\nLLM_TIMEOUT_SECONDS=12.5\n"
        "POSTGRES_USER=ignored\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.llm_provider == "anthropic"
    assert settings.llm_model == "model-x"
    assert settings.llm_timeout_seconds == 12.5  # converti en float


def test_environment_variable_overrides_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql+psycopg://from-file/db\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://from-env/db")

    settings = Settings(_env_file=env_file)

    assert settings.database_url == "postgresql+psycopg://from-env/db"


def test_api_key_is_never_shown_in_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-value")

    settings = Settings(_env_file=None)

    assert "sk-secret-value" not in repr(settings)
    assert "sk-secret-value" not in str(settings.model_dump())
    assert settings.llm_api_key.get_secret_value() == "sk-secret-value"


def test_invalid_value_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "-5")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
