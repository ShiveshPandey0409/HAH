from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.schemas.user import UserCreate, UserResponse
from app.services.users import EmailAlreadyExistsError, create_user

router = APIRouter(prefix="/users", tags=["users"])
SessionDependency = Annotated[AsyncSession, Depends(get_db_session)]


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user_endpoint(data: UserCreate, session: SessionDependency) -> UserResponse:
    try:
        user = await create_user(session, data)
    except EmailAlreadyExistsError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        ) from error
    return UserResponse.model_validate(user)
