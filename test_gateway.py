"""
机器支付网关 M1 核心逻辑测试。

覆盖：health / 402 握手 / per_call / per_token / per_month 计费 /
账单汇总 / 参数校验 / 404。

用标准库 unittest + FastAPI TestClient，不引入 pytest 依赖。
运行：.venv/bin/python -m unittest test_gateway -v
"""
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

import main

# ── 隔离数据库：指向临时目录，避免污染真实 gateway.db ──
_TMP = tempfile.mkdtemp()
main.DB_PATH = os.path.join(_TMP, "test_gateway.db")
main.init_db()

client = TestClient(main.app)


def _wipe():
    conn = main.get_db()
    conn.executescript(
        "DELETE FROM usage_records; DELETE FROM agents; DELETE FROM tools; DELETE FROM developers;"
    )
    conn.commit()
    conn.close()


class TestGateway(unittest.TestCase):
    def setUp(self):
        _wipe()

    def _create_dev(self, name="dev1", wechat_pay="wx-123"):
        r = client.post("/api/v1/developers", json={"name": name, "wechat_pay": wechat_pay})
        self.assertEqual(r.status_code, 201)
        return r.json()["id"]

    def _create_tool(self, dev_id, name="翻译工具", mode="per_call", price=100):
        r = client.post("/api/v1/tools", json={
            "developer_id": dev_id, "name": name, "billing_mode": mode, "price_cents": price,
        })
        self.assertEqual(r.status_code, 201)
        return r.json()["id"]

    def _create_agent(self, name="agent1", balance=0):
        r = client.post("/api/v1/agents", json={"name": name, "balance_cents": balance})
        self.assertEqual(r.status_code, 201)
        return r.json()["id"]

    # ── 基础 ─────────────────────────────────────────
    def test_health(self):
        r = client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    # ── 402 握手 + per_call 完整流程 ─────────────────
    def test_per_call_full_flow(self):
        dev_id = self._create_dev()
        tool_id = self._create_tool(dev_id, mode="per_call", price=100)
        agent_id = self._create_agent(balance=50)  # 额度不够

        # 额度不足 → 402 + 报价
        r = client.post(f"/api/v1/tools/{tool_id}/invoke", json={"agent_id": agent_id, "tokens": 0})
        self.assertEqual(r.status_code, 402)
        body = r.json()
        self.assertEqual(body["price_cents"], 100)
        self.assertEqual(body["agent_balance_cents"], 50)
        self.assertEqual(body["shortfall_cents"], 50)
        self.assertEqual(r.headers.get("Payment-Required"), "true")

        # 充值后 → 200 + 扣款
        conn = main.get_db()
        conn.execute("UPDATE agents SET balance_cents = 1000 WHERE id=?", (agent_id,))
        conn.commit()
        conn.close()

        r = client.post(f"/api/v1/tools/{tool_id}/invoke", json={"agent_id": agent_id, "tokens": 0})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["amount_cents"], 100)
        self.assertEqual(body["agent_balance_cents"], 900)

        # 月结账单
        r = client.get(f"/api/v1/billing?developer_id={dev_id}")
        self.assertEqual(r.status_code, 200)
        bill = r.json()[0]
        self.assertEqual(bill["call_count"], 1)
        self.assertEqual(bill["total_cents"], 100)
        self.assertEqual(bill["total_yuan"], 1.0)

    # ── per_token 计费 ───────────────────────────────
    def test_per_token_billing(self):
        dev_id = self._create_dev()
        tool_id = self._create_tool(dev_id, mode="per_token", price=100)  # 100 分 / 1000 token
        agent_id = self._create_agent(balance=1000)

        # 500 token → 50 分
        r = client.post(f"/api/v1/tools/{tool_id}/invoke", json={"agent_id": agent_id, "tokens": 500})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["amount_cents"], 50)
        self.assertEqual(r.json()["agent_balance_cents"], 950)

        # 2000 token → 200 分
        r = client.post(f"/api/v1/tools/{tool_id}/invoke", json={"agent_id": agent_id, "tokens": 2000})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["amount_cents"], 200)
        self.assertEqual(r.json()["agent_balance_cents"], 750)

    # ── per_month 计费（单次不扣，账单期结算）────────
    def test_per_month_billing(self):
        dev_id = self._create_dev()
        tool_id = self._create_tool(dev_id, mode="per_month", price=9900)  # ¥99/月
        agent_id = self._create_agent(balance=0)  # 余额为 0 也能调用（月结）

        r = client.post(f"/api/v1/tools/{tool_id}/invoke", json={"agent_id": agent_id, "tokens": 0})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["amount_cents"], 0)  # 单次不扣
        self.assertEqual(r.json()["agent_balance_cents"], 0)  # 余额不变

        # 账单记录一次调用（金额 0，月结期结算）
        r = client.get(f"/api/v1/billing?developer_id={dev_id}")
        self.assertEqual(r.json()[0]["call_count"], 1)
        self.assertEqual(r.json()[0]["total_cents"], 0)

    # ── 参数校验 ─────────────────────────────────────
    def test_invalid_billing_mode(self):
        dev_id = self._create_dev()
        r = client.post("/api/v1/tools", json={
            "developer_id": dev_id, "name": "x", "billing_mode": "per_hour", "price_cents": 10,
        })
        self.assertEqual(r.status_code, 400)

    def test_tool_not_found(self):
        agent_id = self._create_agent(balance=100)
        r = client.post("/api/v1/tools/nonexistent/invoke", json={"agent_id": agent_id, "tokens": 0})
        self.assertEqual(r.status_code, 404)

    def test_agent_not_found(self):
        dev_id = self._create_dev()
        tool_id = self._create_tool(dev_id)
        r = client.post(f"/api/v1/tools/{tool_id}/invoke", json={"agent_id": "nope", "tokens": 0})
        self.assertEqual(r.status_code, 404)

    def test_create_tool_missing_developer(self):
        r = client.post("/api/v1/tools", json={
            "developer_id": "nope", "name": "x", "billing_mode": "per_call", "price_cents": 10,
        })
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
