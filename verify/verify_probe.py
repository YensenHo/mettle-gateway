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
import ipaddress
import urllib.error
import urllib.request
from urllib.parse import urlparse

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

# 标准探针 prompt：要求模型输出长文，确保产生足够 token 让扣费达到可测级别
PROBE_PROMPT = (
    "请写一篇中文文章，主题是「人工智能的发展历程与未来趋势」，"
    "要求至少 800 字，分 5 个以上段落详细阐述，内容要具体、有信息量，避免空话。"
)
PROBE_MAX_TOKENS = 1500

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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止跟随重定向：防止 302 跨主机时把 Authorization 泄露给第三方。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(NoRedirect)


def http_post_json(url: str, payload: dict, api_key: str, timeout: int = 60, retries: int = 5) -> tuple[int | None, dict]:
    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {api_key}")
            req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
            with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (301, 302, 303, 307, 308):  # 重定向被拒绝，防止 key 泄露
                return e.code, {"error": f"中转站返回重定向({e.code})，已拒绝跟随以防 key 泄露"}
            if e.code in (401, 404):  # 明确失败不重试
                return e.code, {"error": body[:500]}
            last_err = e  # 403 等可能是 GFW 间歇干扰，重试
        except (urllib.error.URLError, ssl.SSLError, TimeoutError) as e:
            last_err = e
        time.sleep(1.5 * (attempt + 1))  # 指数退避
    return None, {"error": f"网络错误(重试{retries}次后): {last_err}"}


# ── 主流程 ─────────────────────────────────────────────
def _is_private_or_local(host: str) -> bool:
    """判断 host 是否本地/内网地址（SSRF 防护）"""
    host = host.lower()
    if host in ("localhost", "0.0.0.0", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    except ValueError:
        return False  # 域名放行（DNS rebinding 风险 MVP 阶段暂不处理）


def run_probe(base_url: str, api_key: str, model: str, ratio: float, before: float,
              after: float, fx: float = FX_CNY_PER_USD, prices: dict | None = None,
              timeout: int = 60) -> dict:
    # SSRF 防护：只允许 https，拒绝本地/内网地址
    parsed = urlparse(base_url)
    if parsed.scheme != "https":
        return {"error": "base_url 必须使用 https"}
    if _is_private_or_local(parsed.hostname or ""):
        return {"error": "base_url 不能指向本地/内网地址"}

    # 前后余额校验：after 不能大于 before（否则负扣费误判）
    if after > before:
        return {"error": f"调用后余额({after})大于调用前余额({before})，请检查是否填反"}

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
    if reasonable < 0.01:  # 扣费小于余额显示精度，对照法完全不可靠
        level = "UNKNOWN"
        note = f"单次扣费({reasonable:.4f})小于余额显示精度(0.01)，无法可靠判定。建议发更大请求或多次调用累计后再测。"
    elif reasonable < 0.05:  # 5 倍精度内，可能被四舍五入放大
        note += "（⚠️ 单次扣费偏小，余额显示精度可能放大误差，建议发更大请求或多次累计）"

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
