from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.services.auth import AuthenticatedSession, authenticate_token

SessionDependency = Annotated[AsyncSession, Depends(get_db_session)]
_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="HTTP session",
    description="Bearer token returned by /v1/auth/signup or /v1/auth/login.",
)


async def get_authenticated_session(
    session: SessionDependency,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthenticatedSession:
    authenticated = None
    if credentials is not None and credentials.scheme.lower() == "bearer":
        authenticated = await authenticate_token(session, credentials.credentials)
    if authenticated is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid login session required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authenticated


AuthenticatedSessionDependency = Annotated[
    AuthenticatedSession,
    Depends(get_authenticated_session),
]


def require_self(authenticated: AuthenticatedSession, requested_user_id: UUID) -> None:
    if authenticated.user.id != requested_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation is not allowed for another user",
        )
