"""payment-service : service en aval de orders-service, qu'on peut faire tomber (scénario S04).

    uvicorn payment_service:app --port 8002

  GET  /pay                    paiement (503 si la panne est activée)
  POST /admin/fail?enabled=1   active ou désactive la panne
"""

from fastapi import FastAPI, HTTPException

app = FastAPI(title="payment-service")
state = {"failing": False}


@app.get("/pay")
def pay() -> dict[str, str]:
    if state["failing"]:
        raise HTTPException(503, "payment-service en panne")
    return {"status": "paid"}


@app.post("/admin/fail")
def set_failure(enabled: bool = True) -> dict[str, bool]:
    state["failing"] = enabled
    return state
