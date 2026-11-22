"""Génère de la charge sur la demo-app pour reproduire un scénario d'incident en live.

    python scripts/trigger_incident.py s01   # pool DB saturé
    python scripts/trigger_incident.py s02   # CPU saturé
    python scripts/trigger_incident.py s03   # requête SQL lente
    python scripts/trigger_incident.py s04   # payment-service en panne

Options : --duration 60 (secondes), --orders http://localhost:8001, --payments http://localhost:8002
"""

import argparse
import threading
import time

import httpx

ROUTES = {"s01": "/orders", "s02": "/stress", "s03": "/orders/slow", "s04": "/payments"}


def hammer(url: str, until: float, results: list[int]) -> None:
    with httpx.Client(timeout=10) as client:
        while time.time() < until:
            try:
                results.append(client.get(url).status_code)
            except httpx.HTTPError:
                results.append(0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=sorted(ROUTES))
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--orders", default="http://localhost:8001")
    parser.add_argument("--payments", default="http://localhost:8002")
    args = parser.parse_args()

    if args.scenario == "s01":  # occupe tout le pool, puis les autres requêtes attendent
        httpx.post(f"{args.orders}/hold-connections", params={"seconds": args.duration})
    if args.scenario == "s04":
        httpx.post(f"{args.payments}/admin/fail", params={"enabled": True})

    until, results = time.time() + args.duration, []  # type: ignore[var-annotated]
    print(f"{args.scenario} : {args.workers} clients pendant {args.duration:.0f} s...")
    threads = [
        threading.Thread(target=hammer, args=(args.orders + ROUTES[args.scenario], until, results))
        for _ in range(args.workers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if args.scenario == "s04":
        httpx.post(f"{args.payments}/admin/fail", params={"enabled": False})
    errors = sum(1 for code in results if code == 0 or code >= 500)
    print(f"terminé : {len(results)} requêtes, {errors} en erreur")


if __name__ == "__main__":
    main()
