"""orders-service : petite application de démonstration pour reproduire les incidents en live.

    uvicorn app:app --port 8001

Endpoints :
  GET  /orders              normal (une requête SQL rapide)
  GET  /orders/slow         requête SQL lente (pg_sleep)            -> scénario S03
  GET  /payments            appelle payment-service                 -> scénario S04 si celui-ci tombe
  GET  /stress              consomme du CPU                         -> scénario S02
  POST /hold-connections    garde des connexions du pool occupées   -> scénario S01
  GET  /metrics             métriques Prometheus
"""

import os
import threading
import time

import httpx
import psutil
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://doctor_log:doctor_log@localhost:5432/doctor_log"
)
POOL_SIZE = int(os.getenv("POOL_SIZE", "5"))
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://localhost:8002")

# Petit pool, sans débordement : facile à saturer pour le scénario S01.
engine = create_engine(DATABASE_URL, pool_size=POOL_SIZE, max_overflow=0, pool_timeout=5)
app = FastAPI(title="orders-service")

REQUEST_SECONDS = Histogram(
    "http_request_duration_seconds", "Durée des requêtes HTTP", ["route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
REQUESTS = Counter("http_requests_total", "Requêtes HTTP", ["route", "status"])
Gauge("db_connections_active", "Connexions du pool utilisées").set_function(
    lambda: engine.pool.checkedout()  # type: ignore[attr-defined]
)
Gauge("db_connections_idle", "Connexions du pool libres").set_function(
    lambda: POOL_SIZE - engine.pool.checkedout()  # type: ignore[attr-defined]
)
PROCESS = psutil.Process()
Gauge("cpu_usage", "Part du CPU utilisée par le service (0-1)").set_function(
    lambda: PROCESS.cpu_percent() / 100 / psutil.cpu_count()
)


@app.on_event("startup")
def create_table() -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS demo_orders (id SERIAL PRIMARY KEY, item TEXT)"))
        if conn.execute(text("SELECT count(*) FROM demo_orders")).scalar() == 0:
            conn.execute(text("INSERT INTO demo_orders (item) SELECT 'item-' || i FROM generate_series(1, 50) i"))


@app.middleware("http")
async def measure(request: Request, call_next):  # type: ignore[no-untyped-def]
    route = request.url.path
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        if route != "/metrics":
            REQUEST_SECONDS.labels(route).observe(time.perf_counter() - started)
            REQUESTS.labels(route, str(status)).inc()


@app.get("/orders")
def list_orders() -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text("SELECT item FROM demo_orders LIMIT 10"))]


@app.get("/orders/slow")
def slow_orders(seconds: float = 3.0) -> dict[str, float]:
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_sleep(:s)"), {"s": seconds})
    return {"slept": seconds}


@app.get("/payments")
def payments() -> dict[str, str]:
    try:
        response = httpx.get(f"{PAYMENT_URL}/pay", timeout=2)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"payment-service indisponible : {type(exc).__name__}") from exc
    return {"payment": "ok"}


@app.get("/stress")
def stress(seconds: float = 0.5) -> dict[str, int]:
    end, n = time.perf_counter() + seconds, 0
    while time.perf_counter() < end:
        n += sum(i * i for i in range(1000))
    return {"work": n}


@app.post("/hold-connections")
def hold_connections(count: int = POOL_SIZE, seconds: float = 30) -> dict[str, int]:
    """Occupe `count` connexions pendant `seconds` (en arrière-plan) pour saturer le pool."""

    def hold() -> None:
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_sleep(:s)"), {"s": seconds})

    for _ in range(count):
        threading.Thread(target=hold, daemon=True).start()
    return {"held": count}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
