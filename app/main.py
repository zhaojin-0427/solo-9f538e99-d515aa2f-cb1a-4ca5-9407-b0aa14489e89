"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database import init_db
from .routers import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="中央厨房装盒线过敏原暂扣系统",
    description=(
        "录入原料批次过敏原与 A/B 两条装盒线作业，按全班统一工序号排列；"
        "成品经用料、回配与本线残留继承过敏原；支持清洗复验更正、"
        "波及批次实查与暂扣清单生成。"
        "查询接口只读取首次读库时已提交的班次快照，不回写判定；"
        "补登缺失原料后立即重判受影响成品。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/", tags=["status"])
def root():
    return {"service": "allergen-hold", "docs": "/docs"}
