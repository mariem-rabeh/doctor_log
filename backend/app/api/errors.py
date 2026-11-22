"""Format d'erreur commun à toute l'API.

Toutes les erreurs ont la même forme, pour que le frontend n'ait qu'un seul cas à gérer :

    {"error": {"code": "not_found", "message": "Incident introuvable", "details": null}}
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
}


class ApiError(Exception):
    """Erreur métier levée par une route : code HTTP, code lisible et message."""

    def __init__(self, status_code: int, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details


def error_body(status_code: int, message: str, details: Any = None) -> dict[str, Any]:
    code = _CODES.get(status_code, "server_error" if status_code >= 500 else "error")
    return {"error": {"code": code, "message": message, "details": details}}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        body = error_body(exc.status_code, exc.message, jsonable_encoder(exc.details))
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "Erreur HTTP"
        return JSONResponse(
            status_code=exc.status_code, content=error_body(exc.status_code, message)
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "field": ".".join(str(part) for part in err["loc"] if part != "body"),
                "message": err["msg"],
            }
            for err in exc.errors()
        ]
        body = error_body(422, "Requête invalide", jsonable_encoder(details))
        return JSONResponse(status_code=422, content=body)
