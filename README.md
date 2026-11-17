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
