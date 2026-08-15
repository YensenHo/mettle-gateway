# Mettle Gateway

**AI 计费的信任与结算基础设施** —— 人民币版 x402，计量层不碰钱。

- 品牌：Mettle（metal 硬核 + mete 计量 + mettle 胆识）
- A 线 **Gateway**：机器支付/计费网关（本仓库）
- B 线 **Verify**：额度真实性验证中间件（见 YensenHo/chinese-adversarial-eval 演进）

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
# 打开 http://127.0.0.1:8000
```

测试：`.venv/bin/python -m unittest test_gateway -v`

<!-- BEGIN:harness-engineering -->
# HARNESS ENGINEERING — MANDATORY READ BEFORE ANY CODE CHANGE

**Before modifying, creating, or reviewing ANY file in this project, you MUST:**
1. Read `HARNESS.md` in full. It contains non-negotiable product design constraints.
2. Verify your change does not violate any rule in the 🚫 PROHIBITIONS section.
3. If your change touches billing, 402 handshake, funds, or trust — check MANDATORY REQUIREMENTS too.
4. Before proposing a NEW direction (any sub-product), it must pass all 4 items of the 前景 Gate in HARNESS.md §5.
5. If you cannot determine which rules apply, ask the user before proceeding.

Violations of HARNESS.md rules = rejected changes. No exceptions.
<!-- END:harness-engineering -->
