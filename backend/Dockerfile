# Backend doctor_log. Construit depuis la racine du dépôt (il a besoin de datasets/).
FROM python:3.11-slim

WORKDIR /app/backend
COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip install --no-cache-dir .

COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY datasets /app/datasets
COPY knowledge /app/knowledge

EXPOSE 8000
# Les migrations sont appliquées à chaque démarrage (sans effet si la base est à jour).
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
