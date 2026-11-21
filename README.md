# doctor_log

Système multi-agents (LangGraph) de diagnostic d'incidents à partir des logs, métriques et traces.

## Démarrage (développement)

```bash
cd backend
python -m venv .venv
# Windows : .venv\Scripts\activate   |   Linux/macOS : source .venv/bin/activate
pip install -e ".[dev]"
cd .. && pre-commit install
cp .env.example .env
```

Lancer l'API : `cd backend && uvicorn app.main:app --reload` puis ouvrir http://localhost:8000/health

## Base de données (PostgreSQL)

```bash
docker compose -f docker-compose.dev.yml up -d   # PostgreSQL seul, pour le développement
cd backend && alembic upgrade head               # crée / met à jour les tables
```

Après une modification de `backend/app/db/models.py` :
`alembic revision --autogenerate -m "description"`, relire le fichier généré, puis `alembic upgrade head`.

Les tests de `tests/test_db_repository.py` utilisent la base de `DATABASE_URL`
(ignorés si elle n'est pas joignable ; en CI, un service PostgreSQL est fourni).
