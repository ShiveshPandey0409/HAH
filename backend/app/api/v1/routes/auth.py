from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import AuthenticatedSessionDependency
from app.core.config import get_settings
from app.db.session import get_db_session
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    ResetPasswordRequest,
    SignUpRequest,
)
from app.schemas.user import UserResponse
from app.services.auth import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    InvalidPasswordResetTokenError,
    PasswordResetUnavailableError,
    change_password,
    login,
    logout,
    request_password_reset,
    reset_password,
    signup,
)
from app.services.password_reset_delivery import PasswordResetNotifier, notifier_from_settings

router = APIRouter(prefix="/auth", tags=["authentication"])
SessionDependency = Annotated[AsyncSession, Depends(get_db_session)]


def get_password_reset_notifier() -> PasswordResetNotifier:
    return notifier_from_settings(get_settings())


PasswordResetNotifierDependency = Annotated[
    PasswordResetNotifier,
    Depends(get_password_reset_notifier),
]


@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def signup_endpoint(data: SignUpRequest, session: SessionDependency) -> AuthResponse:
    try:
        return await signup(
            session,
            data,
            ttl_seconds=get_settings().http_session_ttl_seconds,
        )
    except EmailAlreadyExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        ) from error


@router.post("/login", response_model=AuthResponse)
async def login_endpoint(data: LoginRequest, session: SessionDependency) -> AuthResponse:
    try:
        return await login(
            session,
            data,
            ttl_seconds=get_settings().http_session_ttl_seconds,
        )
    except InvalidCredentialsError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error


@router.get("/me", response_model=UserResponse)
async def me_endpoint(authenticated: AuthenticatedSessionDependency) -> UserResponse:
    return UserResponse.model_validate(authenticated.user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_endpoint(
    authenticated: AuthenticatedSessionDependency,
    session: SessionDependency,
) -> Response:
    await logout(session, authenticated)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password_endpoint(
    data: ChangePasswordRequest,
    authenticated: AuthenticatedSessionDependency,
    session: SessionDependency,
) -> Response:
    try:
        await change_password(session, authenticated, data)
    except InvalidCredentialsError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password_endpoint(
    data: ForgotPasswordRequest,
    session: SessionDependency,
    notifier: PasswordResetNotifierDependency,
) -> Response:
    settings = get_settings()
    try:
        await request_password_reset(
            session,
            str(data.email),
            notifier=notifier,
            ttl_seconds=settings.password_reset_ttl_seconds,
        )
    except PasswordResetUnavailableError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Password reset email is not configured",
        ) from error
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password_endpoint(
    data: ResetPasswordRequest,
    session: SessionDependency,
) -> Response:
    try:
        await reset_password(session, data)
    except InvalidPasswordResetTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password reset token is invalid or expired",
        ) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)
