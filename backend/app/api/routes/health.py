from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def readiness(request: Request) -> JSONResponse:
    is_ready = await request.app.state.database.is_ready()
    status_code = status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE
    readiness_status = "ready" if is_ready else "not_ready"

    return JSONResponse(
        status_code=status_code,
        content={"status": readiness_status},
    )
