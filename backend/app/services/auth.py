from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import PasswordResetToken, User, UserSession
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    ResetPasswordRequest,
    SignUpRequest,
)
from app.schemas.user import UserResponse
from app.services.password_reset_delivery import (
    PasswordResetDeliveryError,
    PasswordResetNotifier,
)

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_DUMMY_PASSWORD_HASH: str | None = None


class EmailAlreadyExistsError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class InvalidPasswordResetTokenError(Exception):
    pass


class PasswordResetUnavailableError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    user: User
    session: UserSession


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$")
        if algorithm != "scrypt":
            return False
        n, r, p = int(raw_n), int(raw_r), int(raw_p)
        if (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        salt = _b64decode(raw_salt)
        expected = _b64decode(raw_digest)
        if len(salt) != 16 or len(expected) != _SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (binascii.Error, TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def _dummy_password_hash() -> str:
    global _DUMMY_PASSWORD_HASH
    if _DUMMY_PASSWORD_HASH is None:
        _DUMMY_PASSWORD_HASH = hash_password("not-a-real-account-password")
    return _DUMMY_PASSWORD_HASH


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_reset_token(user_id: UUID, ttl_seconds: int) -> tuple[PasswordResetToken, str]:
    token = f"hah_reset_{secrets.token_urlsafe(32)}"
    now = datetime.now(UTC)
    reset = PasswordResetToken(
        user_id=user_id,
        token_hash=_token_hash(token),
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    return reset, token


def _new_session(user_id: UUID, ttl_seconds: int) -> tuple[UserSession, str]:
    token = f"hah_session_{secrets.token_urlsafe(32)}"
    now = datetime.now(UTC)
    session = UserSession(
        user_id=user_id,
        token_hash=_token_hash(token),
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    return session, token


def _auth_response(user: User, session: UserSession, token: str) -> AuthResponse:
    return AuthResponse(
        access_token=token,
        expires_at=session.expires_at,
        user=UserResponse.model_validate(user),
    )


def _sqlstate(error: IntegrityError) -> str | None:
    direct = getattr(error.orig, "sqlstate", None)
    if direct is not None:
        return direct
    return getattr(getattr(error.orig, "__cause__", None), "sqlstate", None)


async def signup(
    session: AsyncSession,
    data: SignUpRequest,
    *,
    ttl_seconds: int,
) -> AuthResponse:
    user = User(
        email=str(data.email).strip().lower(),
        display_name=data.display_name,
        can_create_tasks=data.can_create_tasks,
        can_work_tasks=data.can_work_tasks,
        bio=data.bio,
        password_hash=hash_password(data.password.get_secret_value()),
    )
    session.add(user)
    try:
        await session.flush()
        login_session, token = _new_session(user.id, ttl_seconds)
        session.add(login_session)
        await session.flush()
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        if _sqlstate(error) == "23505":
            raise EmailAlreadyExistsError from error
        raise
    except Exception:
        await session.rollback()
        raise

    await session.refresh(user)
    await session.refresh(login_session)
    return _auth_response(user, login_session, token)


async def login(
    session: AsyncSession,
    data: LoginRequest,
    *,
    ttl_seconds: int,
) -> AuthResponse:
    user = await authenticate_password_user(
        session,
        email=str(data.email),
        password=data.password.get_secret_value(),
    )
    if user is None:
        raise InvalidCredentialsError

    login_session, token = _new_session(user.id, ttl_seconds)
    session.add(login_session)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    await session.refresh(login_session)
    return _auth_response(user, login_session, token)


async def authenticate_password_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
) -> User | None:
    """Verify first-party account credentials without creating an HTTP session."""

    normalized_email = email.strip().lower()
    user = await session.scalar(select(User).where(User.email == normalized_email))
    password_hash = user.password_hash if user is not None else _dummy_password_hash()
    password_valid = verify_password(password, password_hash or "")
    if user is None or user.password_hash is None or not password_valid:
        return None
    return user


async def authenticate_token(session: AsyncSession, token: str) -> AuthenticatedSession | None:
    if not token.startswith("hah_session_") or len(token) > 256:
        return None
    now = datetime.now(UTC)
    login_session = await session.scalar(
        select(UserSession).where(
            UserSession.token_hash == _token_hash(token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
    )
    if login_session is None:
        return None
    user = await session.get(User, login_session.user_id)
    if user is None:
        return None
    return AuthenticatedSession(user=user, session=login_session)


async def logout(session: AsyncSession, authenticated: AuthenticatedSession) -> None:
    authenticated.session.revoked_at = datetime.now(UTC)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise


async def change_password(
    session: AsyncSession,
    authenticated: AuthenticatedSession,
    data: ChangePasswordRequest,
) -> None:
    current = data.current_password.get_secret_value()
    if authenticated.user.password_hash is None or not verify_password(
        current, authenticated.user.password_hash
    ):
        raise InvalidCredentialsError
    authenticated.user.password_hash = hash_password(data.new_password.get_secret_value())
    now = datetime.now(UTC)
    await session.execute(
        update(UserSession)
        .where(
            UserSession.user_id == authenticated.user.id,
            UserSession.id != authenticated.session.id,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise


async def request_password_reset(
    session: AsyncSession,
    email: str,
    *,
    notifier: PasswordResetNotifier,
    ttl_seconds: int,
) -> None:
    if not notifier.configured:
        raise PasswordResetUnavailableError
    user = await session.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or user.password_hash is None:
        return

    now = datetime.now(UTC)
    await session.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    reset, token = _new_reset_token(user.id, ttl_seconds)
    session.add(reset)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    try:
        await notifier.send(email=str(user.email), token=token)
    except PasswordResetDeliveryError:
        reset.consumed_at = datetime.now(UTC)
        await session.commit()


async def reset_password(session: AsyncSession, data: ResetPasswordRequest) -> None:
    token = data.token.get_secret_value()
    if not token.startswith("hah_reset_") or len(token) > 256:
        raise InvalidPasswordResetTokenError
    now = datetime.now(UTC)
    reset = await session.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == _token_hash(token))
        .with_for_update()
    )
    if reset is None or reset.consumed_at is not None or reset.expires_at <= now:
        raise InvalidPasswordResetTokenError
    user = await session.get(User, reset.user_id)
    if user is None:
        raise InvalidPasswordResetTokenError

    user.password_hash = hash_password(data.new_password.get_secret_value())
    reset.consumed_at = now
    await session.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
