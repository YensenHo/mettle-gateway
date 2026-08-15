"""
机器支付网关 (人民币版 x402) — M1 原型
计量层不碰钱：网关做「计量 + 402握手 + 对账」，钱走微信/支付宝直连工具开发者。

核心流程：
  Agent 调用工具 → 网关校验虚拟额度 → 够则放行(200)+计量，不够则 402 + 报价
"""
import sqlite3
import uuid
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "gateway.db")

app = FastAPI(title="机器支付网关 (人民币版 x402)", version="0.1.0-m1")


# ── 数据库 ─────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS developers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        wechat_pay TEXT,              -- 微信/支付宝收款账号(月结用)
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS tools (
        id TEXT PRIMARY KEY,
        developer_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        mcp_endpoint TEXT,             -- 工具/MCP 端点
        billing_mode TEXT NOT NULL,    -- per_call | per_token | per_month
        price_cents INTEGER NOT NULL,  -- 单价(分)：per_call=每次, per_token=每1k token, per_month=每月
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(developer_id) REFERENCES developers(id)
    );
    CREATE TABLE IF NOT EXISTS agents (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        balance_cents INTEGER NOT NULL DEFAULT 0,  -- 虚拟额度(分)，非真钱
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS usage_records (
        id TEXT PRIMARY KEY,
        tool_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        tokens INTEGER DEFAULT 0,      -- 本次调用 token 数
        amount_cents INTEGER NOT NULL, -- 本次计费(分)
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(tool_id) REFERENCES tools(id),
        FOREIGN KEY(agent_id) REFERENCES agents(id)
    );
    """)
    conn.commit()
    conn.close()


# ── Pydantic 模型 ─────────────────────────────────
class DeveloperCreate(BaseModel):
    name: str = Field(..., min_length=1)
    wechat_pay: Optional[str] = None


class ToolCreate(BaseModel):
    developer_id: str
    name: str = Field(..., min_length=1)
    description: Optional[str] = None
    mcp_endpoint: Optional[str] = None
    billing_mode: str = Field(..., description="per_call | per_token | per_month")
    price_cents: int = Field(..., gt=0, description="单价(分)")


class AgentCreate(BaseModel):
    name: str = Field(..., min_length=1)
    balance_cents: int = Field(0, ge=0, description="初始虚拟额度(分)")


class InvokeRequest(BaseModel):
    agent_id: str
    tokens: int = Field(0, ge=0, description="本次调用消耗的 token 数(per_token 模式用)")


# ── API ───────────────────────────────────────────
@app.post("/api/v1/developers", status_code=201)
def create_developer(req: DeveloperCreate):
    conn = get_db()
    dev_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO developers (id, name, wechat_pay) VALUES (?, ?, ?)",
        (dev_id, req.name, req.wechat_pay),
    )
    conn.commit()
    conn.close()
    return {"id": dev_id, "name": req.name, "wechat_pay": req.wechat_pay}


@app.get("/api/v1/developers")
def list_developers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM developers ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/v1/tools", status_code=201)
def create_tool(req: ToolCreate):
    if req.billing_mode not in ("per_call", "per_token", "per_month"):
        raise HTTPException(400, "billing_mode 必须是 per_call/per_token/per_month")
    conn = get_db()
    dev = conn.execute("SELECT id FROM developers WHERE id=?", (req.developer_id,)).fetchone()
    if not dev:
        conn.close()
        raise HTTPException(404, "开发者不存在")
    tool_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO tools (id, developer_id, name, description, mcp_endpoint, billing_mode, price_cents) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tool_id, req.developer_id, req.name, req.description, req.mcp_endpoint, req.billing_mode, req.price_cents),
    )
    conn.commit()
    conn.close()
    return {"id": tool_id, **req.model_dump()}


@app.get("/api/v1/tools")
def list_tools():
    conn = get_db()
    rows = conn.execute(
        "SELECT t.*, d.name as developer_name FROM tools t JOIN developers d ON t.developer_id=d.id "
        "ORDER BY t.created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/v1/agents", status_code=201)
def create_agent(req: AgentCreate):
    conn = get_db()
    agent_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO agents (id, name, balance_cents) VALUES (?, ?, ?)",
        (agent_id, req.name, req.balance_cents),
    )
    conn.commit()
    conn.close()
    return {"id": agent_id, "name": req.name, "balance_cents": req.balance_cents}


@app.get("/api/v1/agents")
def list_agents():
    conn = get_db()
    rows = conn.execute("SELECT * FROM agents ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/v1/tools/{tool_id}/invoke")
def invoke_tool(tool_id: str, req: InvokeRequest):
    """核心：402 握手。agent 调用工具，网关校验额度。"""
    conn = get_db()
    tool = conn.execute("SELECT * FROM tools WHERE id=?", (tool_id,)).fetchone()
    if not tool:
        conn.close()
        raise HTTPException(404, "工具不存在")
    agent = conn.execute("SELECT * FROM agents WHERE id=?", (req.agent_id,)).fetchone()
    if not agent:
        conn.close()
        raise HTTPException(404, "Agent 不存在")

    # 计算本次费用
    if tool["billing_mode"] == "per_call":
        amount = tool["price_cents"]
    elif tool["billing_mode"] == "per_token":
        amount = int(tool["price_cents"] * req.tokens / 1000)
    elif tool["billing_mode"] == "per_month":
        amount = 0  # 月结模式单次不扣，账单期结算
    else:
        conn.close()
        raise HTTPException(400, "未知计费模式")

    # 402 握手：额度不够 → 返回 402 + 报价
    if agent["balance_cents"] < amount and tool["billing_mode"] != "per_month":
        conn.close()
        return JSONResponse(
            status_code=402,
            headers={"Payment-Required": "true"},
            content={
                "detail": "Payment Required",
                "tool": tool["name"],
                "price_cents": amount,
                "billing_mode": tool["billing_mode"],
                "agent_balance_cents": agent["balance_cents"],
                "shortfall_cents": amount - agent["balance_cents"],
                "hint": "额度不足，请月结后充值（人民币版：钱直接付给工具开发者，网关不碰钱）",
            },
        )

    # 额度够 → 扣额度 + 计量
    if tool["billing_mode"] != "per_month":
        conn.execute(
            "UPDATE agents SET balance_cents = balance_cents - ? WHERE id=?",
            (amount, req.agent_id),
        )
    rec_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO usage_records (id, tool_id, agent_id, tokens, amount_cents) VALUES (?, ?, ?, ?, ?)",
        (rec_id, tool_id, req.agent_id, req.tokens, amount),
    )
    conn.commit()

    new_balance = conn.execute("SELECT balance_cents FROM agents WHERE id=?", (req.agent_id,)).fetchone()[0]
    conn.close()
    return {
        "status": "ok",
        "tool": tool["name"],
        "amount_cents": amount,
        "billing_mode": tool["billing_mode"],
        "agent_balance_cents": new_balance,
        "usage_id": rec_id,
    }


@app.get("/api/v1/usage")
def list_usage(agent_id: Optional[str] = None, tool_id: Optional[str] = None):
    conn = get_db()
    q = ("SELECT u.*, t.name as tool_name, a.name as agent_name "
         "FROM usage_records u JOIN tools t ON u.tool_id=t.id JOIN agents a ON u.agent_id=a.id")
    conds, params = [], []
    if agent_id:
        conds.append("u.agent_id=?")
        params.append(agent_id)
    if tool_id:
        conds.append("u.tool_id=?")
        params.append(tool_id)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY u.created_at DESC LIMIT 200"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/v1/billing")
def billing(developer_id: Optional[str] = None):
    """月结账单：按工具开发者汇总计量金额。网关只出账单，钱走微信/支付宝。"""
    conn = get_db()
    q = ("SELECT d.id as developer_id, d.name as developer_name, d.wechat_pay, "
         "COUNT(u.id) as call_count, COALESCE(SUM(u.amount_cents), 0) as total_cents "
         "FROM developers d "
         "LEFT JOIN tools t ON t.developer_id = d.id "
         "LEFT JOIN usage_records u ON u.tool_id = t.id")
    if developer_id:
        q += " WHERE d.id=?"
        rows = conn.execute(q + " GROUP BY d.id", (developer_id,)).fetchall()
    else:
        rows = conn.execute(q + " GROUP BY d.id").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["total_yuan"] = round(d["total_cents"] / 100, 2)
        result.append(d)
    return result


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0-m1"}


@app.get("/")
def root():
    index = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"name": "机器支付网关", "docs": "/docs"}


init_db()
