"""Mettle Verify — 中转站余额真实性验证 Web 后端 (MVP)

两步式交互（解决对照法时序问题）：
  Step 1: POST /api/v1/probe/send  发探针，缓存 usage，返回 session_id
  Step 2: POST /api/v1/probe/judge 用 session_id + 前后余额 算判定

隐私承诺：api_key 仅用于当次探针请求，不落盘、不写库、不记日志；
缓存只存 usage/价格（不含 key），判定后即清理。
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(__file__))  # 确保能 import verify_probe

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import verify_probe

app = FastAPI(title="Mettle Verify", version="0.1.0-mvp")

# 内存缓存：session_id -> 探针结果（不含 key，只存 usage/价格/倍率/汇率）
probe_cache: dict[str, dict] = {}


class SendRequest(BaseModel):
    base_url: str = Field(..., min_length=1, description="中转站 base_url，如 https://www.cun.ai/v1")
    api_key: str = Field(..., min_length=1, description="中转站 API key (sk-...)，仅当次使用不落盘")
    model: str = Field(..., min_length=1, description="宣称模型名，如 gpt-4o")
    ratio: float = Field(1.0, gt=0, description="宣称倍率")
    fx: float = Field(1.0, gt=0, description="余额币种汇率：美元 1.0，人民币 7.2")
    price_input: float | None = Field(None, gt=0, description="可选：手动指定官方输入价(USD/1M)")
    price_output: float | None = Field(None, gt=0, description="可选：手动指定官方输出价(USD/1M)")


class JudgeRequest(BaseModel):
    session_id: str = Field(..., min_length=1, description="第一步返回的 session_id")
    before: float = Field(..., ge=0, description="调用前余额")
    after: float = Field(..., ge=0, description="调用后余额")


@app.post("/api/v1/probe/send")
def send_probe(req: SendRequest):
    """第一步：发探针请求，缓存 usage，返回 session_id + 探针信息"""
    prices = None
    if req.price_input and req.price_output:
        prices = {req.model: {"input": req.price_input, "output": req.price_output}}
    result = verify_probe.send_probe(
        base_url=req.base_url,
        api_key=req.api_key,
        model=req.model,
        ratio=req.ratio,
        fx=req.fx,
        prices=prices,
    )
    if "error" in result:
        return result
    session_id = uuid.uuid4().hex
    probe_cache[session_id] = result
    result["session_id"] = session_id
    return result


@app.post("/api/v1/probe/judge")
def judge_probe(req: JudgeRequest):
    """第二步：用缓存的探针结果 + 前后余额算判定"""
    cached = probe_cache.get(req.session_id)
    if cached is None:
        return {"error": "会话已过期或不存在，请重新发探针"}
    result = verify_probe.judge_probe(cached, req.before, req.after)
    probe_cache.pop(req.session_id, None)  # 判定后清理，一次性使用
    return result


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0-mvp"}


@app.get("/")
def root():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))
