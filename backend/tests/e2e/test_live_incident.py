"""Test de bout en bout sur la stack Docker complète (branche 29).

    docker compose up -d --build
    E2E=1 pytest tests/e2e -v

Déclenche S01 sur la demo-app, crée un incident live, lance l'analyse et attend COMPLETED.
Ignoré sans E2E=1 (il demande toute la stack et un vrai LLM : quelques minutes).
"""

import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

API = os.getenv("E2E_API", "http://localhost:8000/api")
DEMO = os.getenv("E2E_DEMO", "http://localhost:8001")
ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(os.getenv("E2E") != "1", reason="E2E=1 pour lancer ce test")


def test_live_pool_saturation_is_diagnosed() -> None:
    for _ in range(20):  # un peu de trafic normal avant l'incident
        httpx.get(f"{DEMO}/orders", timeout=10)
        time.sleep(1)
    started = datetime.now(UTC)
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "trigger_incident.py"), "s01", "--duration", "45"],
        check=True,
    )
    time.sleep(10)  # laisse Prometheus et Jaeger recevoir les dernières données

    window = {
        "start": (started - timedelta(minutes=2)).isoformat(),
        "end": datetime.now(UTC).isoformat(),
    }
    incident = httpx.post(
        f"{API}/incidents",
        json={
            "title": "E2E pool saturé",
            "source": "live",
            "window_start": window["start"],
            "window_end": window["end"],
        },
    ).json()
    assert httpx.post(f"{API}/incidents/{incident['id']}/analyze").status_code == 202

    deadline = time.time() + 600
    status: dict[str, str] = {}
    while time.time() < deadline:
        status = httpx.get(f"{API}/incidents/{incident['id']}/status").json()
        if status["status"] in ("completed", "failed"):
            break
        time.sleep(5)
    assert status.get("status") == "completed", status

    report = httpx.get(f"{API}/incidents/{incident['id']}/report").json()
    assert "pool" in (report["probable_cause"] or "").lower(), report["probable_cause"]
