"""Mettle Verify — 中转站余额真实性验证 Web 后端 (MVP)

复用 verify_probe.py 的对照法逻辑，包装成 HTTP API。
隐私承诺：api_key 仅用于当次探针请求，不落盘、不写库、不记日志。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))  # 确保能 import verify_probe

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import verify_probe

app = FastAPI(title="Mettle Verify", version="0.1.0-mvp")


class ProbeRequest(BaseModel):
    base_url: str = Field(..., min_length=1, description="中转站 base_url，如 https://www.cun.ai/v1")
    api_key: str = Field(..., min_length=1, description="中转站 API key (sk-...)，仅当次使用不落盘")
    model: str = Field(..., min_length=1, description="宣称模型名，如 gpt-4o")
    ratio: float = Field(1.0, gt=0, description="宣称倍率")
    before: float = Field(..., description="调用前余额")
    after: float = Field(..., description="调用后余额")
    fx: float = Field(1.0, gt=0, description="汇率：余额是美元填 1.0，人民币填 7.2")
    price_input: float | None = Field(None, gt=0, description="可选：手动指定官方输入价(USD/1M)")
    price_output: float | None = Field(None, gt=0, description="可选：手动指定官方输出价(USD/1M)")


@app.post("/api/v1/probe")
def probe(req: ProbeRequest):
    """对照法验证：发一次探针请求，比对「实际扣费 vs 合理扣费」"""
    prices = None
    if req.price_input and req.price_output:
        prices = {req.model: {"input": req.price_input, "output": req.price_output}}
    return verify_probe.run_probe(
        base_url=req.base_url,
        api_key=req.api_key,
        model=req.model,
        ratio=req.ratio,
        before=req.before,
        after=req.after,
        fx=req.fx,
        prices=prices,
    )


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0-mvp"}


@app.get("/")
def root():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))
