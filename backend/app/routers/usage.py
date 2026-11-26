from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..deps import get_current_user, get_optional_user, get_usage_service
from ..schemas import DashboardUsageResponse
from ..usage_service import UsageService, build_demo_dashboard

router = APIRouter(prefix="/api/usage", tags=["usage"])


@router.get("", response_model=DashboardUsageResponse, operation_id="getDashboardUsage")
async def get_usage(
    demo: bool = Query(default=False, description="回傳三平台展示假資料"),
    user: dict[str, Any] | None = Depends(get_optional_user),
    svc: UsageService = Depends(get_usage_service),
) -> DashboardUsageResponse:
    if demo:
        return build_demo_dashboard()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="請先登入帳號，或切換至 Demo 預覽模式",
        )
    return await svc.get_dashboard_for_user(int(user["id"]))


@router.post("/refresh", response_model=DashboardUsageResponse, operation_id="refreshDashboardUsage")
async def refresh_usage(
    user: dict[str, Any] = Depends(get_current_user),
    svc: UsageService = Depends(get_usage_service),
) -> DashboardUsageResponse:
    return await svc.refresh_user_dashboard(int(user["id"]))
