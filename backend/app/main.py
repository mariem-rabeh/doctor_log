import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import analysis
from app.api.analysis import router as analysis_router
from app.api.errors import register_error_handlers
from app.api.incidents import router as incidents_router
from app.config import get_settings
from app.db import repository as repo

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Une analyse en cours meurt avec le serveur : sans ça, elle resterait « running » à vie.
    try:
        with analysis.open_session() as db:
            if count := repo.fail_interrupted(db):
                logger.warning("%d analyse(s) interrompue(s) passée(s) en échec", count)
    except Exception:  # base injoignable : l'API démarre quand même
        logger.exception("impossible de nettoyer les analyses interrompues")
    yield


app = FastAPI(title="doctor_log", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
register_error_handlers(app)
app.include_router(incidents_router)
app.include_router(analysis_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
