import hashlib
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from aisbench_web.repositories.users import User, UserRepository
from aisbench_web.security import SESSION_COOKIE


def get_user_repository(request: Request) -> UserRepository:
    return UserRepository(request.app.state.database)


def get_current_user(
    request: Request,
    repository: Annotated[UserRepository, Depends(get_user_repository)],
) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
        )
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    user = repository.get_user_by_session_hash(token_hash)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
        )
    return user
