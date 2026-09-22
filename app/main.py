"""对象存储代理的 HTTP Range 回源规划服务。"""

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .planner import PlanError, build_plan

# 服务级默认值，可由环境变量覆盖；请求逐次携带 max_gap/max_fetch 时优先用请求值。
DEFAULT_MAX_GAP = int(os.getenv("MAX_GAP_DEFAULT", "65536"))
DEFAULT_MAX_FETCH = int(os.getenv("MAX_FETCH_DEFAULT", str(8 * 1024 * 1024)))

app = FastAPI(
    title="Range 回源规划服务",
    description="根据对象长度、多个 Range 与合并约束，规划最小化的回源块及切片映射。",
    version="1.0.0",
)


class RangeSpec(BaseModel):
    """闭区间用 start + end（均含），suffix 范围用 suffix（最后 N 字节）。两类二选一。"""

    start: int | None = Field(default=None, ge=0, description="闭区间起点（含）")
    end: int | None = Field(default=None, ge=0, description="闭区间终点（含）")
    suffix: int | None = Field(default=None, ge=1, description="suffix 范围：最后 N 字节")


class PlanRequest(BaseModel):
    total_length: int = Field(..., ge=0, description="对象总长度（字节）")
    ranges: list[RangeSpec] = Field(..., description="请求范围列表")
    max_gap: int | None = Field(
        default=None,
        ge=0,
        description=f"允许合并的最大间隙（字节），相邻区间间隙为 0；缺省用服务默认 {DEFAULT_MAX_GAP}",
    )
    max_fetch: int | None = Field(
        default=None,
        ge=1,
        description=f"单次回源长度上限（字节）；缺省用服务默认 {DEFAULT_MAX_FETCH}",
    )


class Slice(BaseModel):
    start: int
    end: int
    length: int


class RangePlan(BaseModel):
    request_index: int
    block_index: int
    request_range: dict
    length: int
    slice: Slice


class BlockInfo(BaseModel):
    index: int
    start: int
    end: int
    length: int
    range_header: str


class PlanResponse(BaseModel):
    total_length: int
    blocks: list[BlockInfo]
    plans: list[RangePlan]


@app.exception_handler(PlanError)
async def plan_error_handler(request, exc: PlanError):
    detail = {"message": exc.message}
    if exc.index is not None:
        detail["request_index"] = exc.index
    return JSONResponse(status_code=422, detail={"detail": detail})


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/plan", response_model=PlanResponse)
async def plan(req: PlanRequest):
    result = build_plan(
        req.ranges,
        total_length=req.total_length,
        max_gap=req.max_gap if req.max_gap is not None else DEFAULT_MAX_GAP,
        max_fetch=req.max_fetch if req.max_fetch is not None else DEFAULT_MAX_FETCH,
    )
    return {"total_length": req.total_length, **result}
