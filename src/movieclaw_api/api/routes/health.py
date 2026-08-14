from fastapi import APIRouter, Request

from movieclaw_api.core.config import get_settings
from movieclaw_api.schemas.base import BaseModel
from movieclaw_api.spec_state import get_spec_hash

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str
    # CLI 版本偏斜检测（docs/design/cli.md §2.1）：与 CLI 内置基线 spec 的
    # 指纹比对，不一致说明两端接口面不同版
    spec_hash: str


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    operation_id="health.check",
)
async def healthcheck(request: Request) -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.app_env,
        spec_hash=get_spec_hash(request.app),
    )
