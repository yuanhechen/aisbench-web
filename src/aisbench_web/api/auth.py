import hashlib
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from aisbench_web.dependencies import get_current_user, get_user_repository
from aisbench_web.repositories.users import DuplicateUsernameError, User, UserRepository
from aisbench_web.security import (
    SESSION_COOKIE,
    SESSION_DAYS,
    hash_password,
    new_session_token,
    verify_password,
)

router = APIRouter()
RepositoryDependency = Annotated[UserRepository, Depends(get_user_repository)]
CurrentUserDependency = Annotated[User, Depends(get_current_user)]
_DUMMY_PASSWORD_HASH = hash_password("dummy password used only for login timing")


class RegistrationRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: str
    username: str
    created_at: str
    last_login_at: str | None

    @classmethod
    def from_user(cls, user: User) -> "UserResponse":
        return cls(
            id=user.id,
            username=user.username,
            created_at=user.created_at,
            last_login_at=user.last_login_at,
        )


def _set_session_cookie(
    response: Response,
    request: Request,
    token: str,
    now: datetime,
) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        expires=now + timedelta(days=SESSION_DAYS),
        path="/",
        secure=request.url.scheme.casefold() == "https",
        httponly=True,
        samesite="lax",
    )


@router.post(
    "/api/auth/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    payload: RegistrationRequest,
    request: Request,
    response: Response,
    repository: RepositoryDependency,
) -> UserResponse:
    password_hash = hash_password(payload.password)
    token, token_hash = new_session_token()
    now = datetime.now(timezone.utc)
    try:
        user, _session = repository.create_user_with_session(
            username=payload.username,
            password_hash=password_hash,
            token_hash=token_hash,
            now=now,
        )
    except DuplicateUsernameError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="username already exists",
        ) from exc
    _set_session_cookie(response, request, token, now)
    return UserResponse.from_user(user)


@router.post("/api/auth/login", response_model=UserResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    repository: RepositoryDependency,
) -> UserResponse:
    credentials = repository.get_credentials_by_username(payload.username)
    encoded_password = (
        credentials.password_hash if credentials is not None else _DUMMY_PASSWORD_HASH
    )
    password_is_valid = verify_password(encoded_password, payload.password)
    if credentials is None or not password_is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
        )

    token, token_hash = new_session_token()
    now = datetime.now(timezone.utc)
    user, _session = repository.record_login_and_create_session(
        user_id=credentials.user.id,
        token_hash=token_hash,
        now=now,
    )
    _set_session_cookie(response, request, token, now)
    return UserResponse.from_user(user)


@router.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, repository: RepositoryDependency) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        repository.revoke_session(token_hash)

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        key=SESSION_COOKIE,
        path="/",
        secure=request.url.scheme.casefold() == "https",
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/api/me", response_model=UserResponse)
def me(user: CurrentUserDependency) -> UserResponse:
    return UserResponse.from_user(user)
