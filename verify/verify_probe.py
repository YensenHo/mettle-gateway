#!/usr/bin/env python3
"""
Mettle Verify — 余额真实性验证探针 (V1)

检测 AI API 中转站是否「余额虚标 / 倍率造假 / 偷偷多扣」。
原理（对照法）：发一次标准探针请求，用官方单价 × 宣称倍率 算出「合理扣费」，
再与「实际扣费（前后余额差）」比对，超比例即判定造假。

用法:
  python verify_probe.py --base-url https://relay.example.com --api-key sk-xxx \\
      --model claude-sonnet-4 --ratio 1.0 --before 100.0 --after 99.7

依赖: 仅 Python 标准库 (urllib)，零第三方依赖。
"""
import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

# ── 官方价目表：每百万 token 的 USD 价格（近似值，可用 --prices 指定 JSON 文件覆盖）──
DEFAULT_PRICES = {
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-opus-4": {"input": 15.00, "output": 75.00},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "qwen-max": {"input": 2.40, "output": 9.60},
    "qwen-plus": {"input": 0.80, "output": 2.00},
    "glm-4-plus": {"input": 0.70, "output": 1.40},
}

# 标准探针 prompt：固定输入，输出长度可复现（max_tokens 封顶）
PROBE_PROMPT = "请用一句话介绍你自己，然后从 1 数到 20。"
PROBE_MAX_TOKENS = 200

FX_CNY_PER_USD = 7.2  # 默认汇率


# ── 纯函数（可单测）─────────────────────────────────────
def compute_reasonable_cost(price, prompt_tokens, completion_tokens, ratio, fx=FX_CNY_PER_USD):
    """合理扣费(元) = (官方输入价×prompt + 官方输出价×completion)/1e6 × 倍率 × 汇率"""
    usd = (price["input"] * prompt_tokens + price["output"] * completion_tokens) / 1_000_000
    return usd * ratio * fx


def judge(actual, reasonable):
    """返回 (等级, 说明)。actual=实际扣费(元), reasonable=合理扣费(元)"""
    if reasonable <= 0:
        return "UNKNOWN", "合理扣费为 0，无法判定（token 数可能为 0 或价目表缺失）"
    ratio = actual / reasonable
    if ratio < 1.1:
        return "OK", f"扣费正常（实际/合理 = {ratio:.2f}）"
    if ratio < 1.5:
        return "WARN", f"疑似多扣（实际/合理 = {ratio:.2f}，超出宣称倍率 {ratio - 1:.0%}）"
    return "FAIL", f"倍率虚标/严重造假（实际/合理 = {ratio:.2f}，多扣 {(ratio - 1) * 100:.0f}%）"


# ── HTTP ───────────────────────────────────────────────
def normalize_base_url(base_url):
    """把 base_url 标准化为 /v1/chat/completions 全路径"""
    url = base_url.rstrip("/")
    if url.endswith("/v1/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def http_post_json(url: str, payload: dict, api_key: str, timeout: int = 60, retries: int = 5) -> tuple[int | None, dict]:
    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {api_key}")
            req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (401, 404):  # 明确失败不重试
                return e.code, {"error": body[:500]}
            last_err = e  # 403 等可能是 GFW 间歇干扰，重试
        except (urllib.error.URLError, ssl.SSLError, TimeoutError) as e:
            last_err = e
        time.sleep(1.5 * (attempt + 1))  # 指数退避
    return None, {"error": f"网络错误(重试{retries}次后): {last_err}"}


# ── 主流程 ─────────────────────────────────────────────
def run_probe(base_url: str, api_key: str, model: str, ratio: float, before: float,
              after: float, fx: float = FX_CNY_PER_USD, prices: dict | None = None,
              timeout: int = 60) -> dict:
    prices = prices or DEFAULT_PRICES
    price = prices.get(model)
    if price is None:
        return {
            "error": f"模型 '{model}' 不在内置价目表。请用 --price-input/--price-output 指定官方价，"
                     f"或用 --prices prices.json 提供价目表。",
        }

    url = normalize_base_url(base_url)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": PROBE_MAX_TOKENS,
    }
    status, resp = http_post_json(url, payload, api_key, timeout=timeout)
    if status != 200:
        return {"error": f"探针请求失败 (HTTP {status}): {resp.get('error', resp)}"}

    usage = resp.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    reasonable = compute_reasonable_cost(price, prompt_tokens, completion_tokens, ratio, fx)
    actual = before - after
    level, note = judge(actual, reasonable)
    if reasonable < 0.05:  # 单次扣费太小，余额显示精度可能放大误差
        note += "（⚠️ 单次扣费过小，余额显示精度可能放大误差，建议发更大请求或多次累计）"

    return {
        "model": model,
        "http_status": status,
        "usage": usage,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "official_price_usd_per_1m": price,
        "claimed_ratio": ratio,
        "fx_cny_per_usd": fx,
        "reasonable_cost_cny": round(reasonable, 4),
        "actual_cost_cny": round(actual, 4),
        "before_cny": before,
        "after_cny": after,
        "judge_level": level,
        "judge_note": note,
    }


def main():
    ap = argparse.ArgumentParser(description="Mettle Verify — 余额真实性验证探针 (V1)")
    ap.add_argument("--base-url", required=True, help="中转站 base_url，如 https://relay.example.com")
    ap.add_argument("--api-key", required=True, help="中转站 API key (sk-...)")
    ap.add_argument("--model", required=True, help="宣称模型名，如 claude-sonnet-4")
    ap.add_argument("--ratio", type=float, default=1.0, help="宣称倍率，默认 1.0")
    ap.add_argument("--before", type=float, required=True, help="调用前余额(元)")
    ap.add_argument("--after", type=float, required=True, help="调用后余额(元)")
    ap.add_argument("--fx", type=float, default=FX_CNY_PER_USD, help=f"汇率 CNY/USD，默认 {FX_CNY_PER_USD}")
    ap.add_argument("--prices", help="可选：官方价目表 JSON 文件路径，覆盖内置价目表")
    ap.add_argument("--price-input", type=float, help="可选：手动指定官方输入价(USD/1M token)")
    ap.add_argument("--price-output", type=float, help="可选：手动指定官方输出价(USD/1M token)")
    ap.add_argument("--timeout", type=int, default=60, help="HTTP 超时秒数，默认 60")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args()

    prices = dict(DEFAULT_PRICES)
    if args.prices:
        with open(args.prices, encoding="utf-8") as f:
            prices.update(json.load(f))
    if args.price_input and args.price_output:
        prices[args.model] = {"input": args.price_input, "output": args.price_output}

    result = run_probe(
        args.base_url, args.api_key, args.model, args.ratio,
        args.before, args.after, fx=args.fx, prices=prices, timeout=args.timeout,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if "error" in result:
            print(f"❌ {result['error']}")
            sys.exit(1)
        level_icon = {"OK": "✅", "WARN": "⚠️", "FAIL": "🔴", "UNKNOWN": "❓"}[result["judge_level"]]
        print(f"{level_icon} {result['judge_level']}: {result['judge_note']}")
        print(f"  模型: {result['model']} (宣称倍率 {result['claimed_ratio']}x)")
        print(f"  探针消耗: {result['prompt_tokens']} prompt + {result['completion_tokens']} completion tokens")
        print(f"  官方价: ${result['official_price_usd_per_1m']['input']}/M in, "
              f"${result['official_price_usd_per_1m']['output']}/M out")
        print(f"  合理扣费: ¥{result['reasonable_cost_cny']}  (官方价 × 倍率 × 汇率 {result['fx_cny_per_usd']})")
        print(f"  实际扣费: ¥{result['actual_cost_cny']}  (前 {result['before_cny']} → 后 {result['after_cny']})")
    sys.exit(0 if result.get("judge_level") == "OK" else 1)


if __name__ == "__main__":
    main()
