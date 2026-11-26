from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status

from ..cost_service import (
    CostService,
    build_demo_cost_drilldown,
    build_demo_cost_summary,
)
from ..database import Database
from ..deps import get_cost_service, get_current_user, get_db, get_optional_user
from ..schemas import (
    CostApiKeyCreateRequest,
    CostApiKeyCreateResponse,
    CostApiKeyDeleteResponse,
    CostApiKeyListResponse,
    CostBudgetUpdateRequest,
    CostDays,
    CostDrilldownResponse,
    CostGroupBy,
    CostIngestRequest,
    CostIngestResponse,
    CostSummaryResponse,
    CostSyncResponse,
    MonthlyBudgetStatusOut,
)

router = APIRouter(prefix="/api/costs", tags=["costs"])


def _validate_days(days: int) -> CostDays:
    if days == 7:
        return 7
    if days == 30:
        return 30
    if days == 90:
        return 90
    raise ValueError("days 參數僅支援 7、30 或 90")


@router.get(
    "/summary",
    response_model=CostSummaryResponse,
    operation_id="getCostSummary",
)
async def get_cost_summary(
    days: int = Query(default=30, description="彙總天數區間（7 / 30 / 90）"),
    group_by: CostGroupBy = Query(
        default="provider", description="單次 GROUP BY 分組維度（provider / model / key）"
    ),
    provider: str | None = Query(default=None, description="依平台篩選（例如 anthropic / openai / openrouter）"),
    orderby: str = Query(default="cost_usd desc", description="排序欄位與方向（例如 cost_usd desc）"),
    top: int = Query(default=25, ge=1, le=100, description="單頁筆數上限"),
    skip: int = Query(default=0, ge=0, description="分頁略過筆數"),
    demo: bool = Query(default=False, description="回傳免登入多平台 API 帳單展示資料"),
    user: dict[str, Any] | None = Depends(get_optional_user),
    cost_svc: CostService = Depends(get_cost_service),
) -> CostSummaryResponse:
    try:
        valid_days = _validate_days(days)
        if demo:
            return build_demo_cost_summary(
                days=valid_days,
                group_by=group_by,
                provider_filter=provider,
                orderby=orderby,
                top=top,
                skip=skip,
            )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="請先登入帳號，或切換至 Demo 預覽模式",
            )
        return cost_svc.get_summary_for_user(
            int(user["id"]),
            days=valid_days,
            group_by=group_by,
            provider_filter=provider,
            orderby=orderby,
            top=top,
            skip=skip,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get(
    "/drilldown",
    response_model=CostDrilldownResponse,
    operation_id="getCostDrilldown",
)
async def get_cost_drilldown(
    group_by: CostGroupBy = Query(..., description="點擊展開列所屬之分組維度"),
    group_value: str = Query(..., min_length=1, max_length=160, description="展開列之鍵值"),
    days: int = Query(default=30, description="明細彙總天數區間（7 / 30 / 90）"),
    provider: str | None = Query(default=None, description="選用的平台篩選條件"),
    demo: bool = Query(default=False, description="回傳免登入 Demo 明細資料"),
    user: dict[str, Any] | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
) -> CostDrilldownResponse:
    try:
        valid_days = _validate_days(days)
        if demo:
            return build_demo_cost_drilldown(
                group_by=group_by,
                group_value=group_value,
                days=valid_days,
                provider_filter=provider,
            )
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="請先登入帳號，或切換至 Demo 預覽模式",
            )
        return db.query_cost_drilldown(
            int(user["id"]),
            group_by=group_by,
            group_value=group_value,
            days=valid_days,
            provider_filter=provider,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get(
    "/keys",
    response_model=CostApiKeyListResponse,
    operation_id="listCostApiKeys",
)
async def list_cost_keys(
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> CostApiKeyListResponse:
    uid = int(user["id"])
    return CostApiKeyListResponse(
        keys=db.list_cost_api_keys(uid),
        monthly_budget_usd=db.get_monthly_budget(uid),
    )


@router.post(
    "/keys",
    response_model=CostApiKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createCostApiKey",
)
async def create_cost_key(
    payload: CostApiKeyCreateRequest,
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> CostApiKeyCreateResponse:
    key_out, generated_secret = db.create_cost_api_key(
        user_id=int(user["id"]),
        provider=payload.provider,
        label=payload.label,
        raw_api_key=payload.api_key,
    )
    return CostApiKeyCreateResponse(
        key=key_out,
        generated_secret=generated_secret,
    )


@router.delete(
    "/keys/{key_id}",
    response_model=CostApiKeyDeleteResponse,
    operation_id="deleteCostApiKey",
)
async def delete_cost_key(
    key_id: int = Path(..., ge=1),
    user: dict[str, Any] = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> CostApiKeyDeleteResponse:
    deleted = db.delete_cost_api_key(int(user["id"]), key_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="找不到指定的 API 金鑰",
        )
    return CostApiKeyDeleteResponse(id=key_id, deleted=True)


@router.put(
    "/budget",
    response_model=MonthlyBudgetStatusOut,
    operation_id="updateCostBudget",
)
async def update_cost_budget(
    payload: CostBudgetUpdateRequest,
    user: dict[str, Any] = Depends(get_current_user),
    cost_svc: CostService = Depends(get_cost_service),
) -> MonthlyBudgetStatusOut:
    return await cost_svc.update_budget(int(user["id"]), payload.monthly_budget_usd)


@router.post(
    "/sync",
    response_model=CostSyncResponse,
    operation_id="syncCostUsage",
)
async def sync_cost_usage(
    days: int = Query(default=30),
    group_by: CostGroupBy = Query(default="provider"),
    provider: str | None = Query(default=None),
    orderby: str = Query(default="cost_usd desc"),
    top: int = Query(default=25, ge=1, le=100),
    skip: int = Query(default=0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
    cost_svc: CostService = Depends(get_cost_service),
) -> CostSyncResponse:
    try:
        valid_days = _validate_days(days)
        return await cost_svc.sync_user_costs(
            int(user["id"]),
            days=valid_days,
            group_by=group_by,
            provider_filter=provider,
            orderby=orderby,
            top=top,
            skip=skip,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.post(
    "/ingest",
    response_model=CostIngestResponse,
    operation_id="ingestCostRecords",
)
async def ingest_cost_records(
    payload: CostIngestRequest,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    user: dict[str, Any] | None = Depends(get_optional_user),
    db: Database = Depends(get_db),
    cost_svc: CostService = Depends(get_cost_service),
) -> CostIngestResponse:
    bearer_token = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer_token = authorization[7:].strip()
    elif x_api_key:
        bearer_token = x_api_key.strip()

    if bearer_token:
        resolved = db.resolve_ingest_bearer_key(bearer_token)
        if resolved is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="無效或已停用的 Custom Ingest API 金鑰",
            )
        target_uid, target_key_id = resolved
        if payload.key_id is not None and payload.key_id != target_key_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bearer 金鑰僅能寫入其自身對應之 key_id",
            )
    elif user is not None:
        target_uid = int(user["id"])
        if payload.key_id is not None:
            meta = db.get_cost_api_key_meta(target_uid, payload.key_id)
            if meta is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="找不到指定的 API 金鑰",
                )
            target_key_id = meta.id
        else:
            target_key_id = db.get_or_create_default_ingest_key(target_uid)
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="請提供有效的 Custom Ingest Bearer 金鑰或先登入帳號",
        )

    return await cost_svc.ingest_records(
        user_id=target_uid,
        key_id=target_key_id,
        items=payload.records,
    )
