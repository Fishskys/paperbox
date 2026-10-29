"""Expose the same effective identity used by REST permission gates."""

from fastapi import APIRouter, Depends, Response

from app.core.security import require_identity
from app.schemas.auth import AuthContextOut
from app.services import api_key_service
from app.services.api_key_service import AuthIdentity

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/context", response_model=AuthContextOut)
def auth_context(
    response: Response,
    identity: AuthIdentity = Depends(require_identity),
) -> AuthContextOut:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization, X-API-Key"
    return AuthContextOut.model_validate(api_key_service.describe_identity(identity))