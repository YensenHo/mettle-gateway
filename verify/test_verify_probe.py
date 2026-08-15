"""Mettle Verify 探针 (V1) 核心逻辑测试 — 对照法计算 + 判定 + 端到端(mock HTTP)。

运行: cd verify && python3 -m unittest test_verify_probe -v
"""
import unittest
from unittest.mock import patch

import verify_probe


class TestPureFunctions(unittest.TestCase):
    def test_compute_reasonable_cost(self):
        # claude-sonnet-4: input $3, output $15 / 1M token
        price = {"input": 3.0, "output": 15.0}
        # 100 prompt + 100 completion = (3*100 + 15*100)/1e6 = 0.0018 USD = 0.01296 CNY (fx 7.2)
        cost = verify_probe.compute_reasonable_cost(price, 100, 100, ratio=1.0, fx=7.2)
        self.assertAlmostEqual(cost, 0.01296, places=5)

    def test_compute_reasonable_cost_with_ratio(self):
        price = {"input": 3.0, "output": 15.0}
        cost = verify_probe.compute_reasonable_cost(price, 100, 100, ratio=2.0, fx=7.2)
        self.assertAlmostEqual(cost, 0.02592, places=5)  # 倍率 2 翻倍

    def test_judge_ok(self):
        level, _ = verify_probe.judge(actual=0.010, reasonable=0.01296)  # 0.77x
        self.assertEqual(level, "OK")

    def test_judge_warn(self):
        level, _ = verify_probe.judge(actual=0.015, reasonable=0.01296)  # 1.16x
        self.assertEqual(level, "WARN")

    def test_judge_fail(self):
        level, _ = verify_probe.judge(actual=0.030, reasonable=0.01296)  # 2.31x
        self.assertEqual(level, "FAIL")

    def test_normalize_base_url(self):
        self.assertEqual(
            verify_probe.normalize_base_url("https://relay.example.com"),
            "https://relay.example.com/v1/chat/completions",
        )
        self.assertEqual(
            verify_probe.normalize_base_url("https://relay.example.com/v1"),
            "https://relay.example.com/v1/chat/completions",
        )
        self.assertEqual(
            verify_probe.normalize_base_url("https://relay.example.com/v1/chat/completions"),
            "https://relay.example.com/v1/chat/completions",
        )


class TestRunProbe(unittest.TestCase):
    @patch("verify_probe.http_post_json")
    def test_probe_detects_overcharge(self, mock_http):
        """实际扣费 0.02 vs 合理 0.01296 = 1.54x → FAIL（倍率虚标）"""
        mock_http.return_value = (200, {"usage": {"prompt_tokens": 100, "completion_tokens": 100}})
        result = verify_probe.run_probe(
            base_url="https://relay.example.com",
            api_key="sk-test",
            model="claude-sonnet-4",
            ratio=1.0,
            before=10.0,
            after=9.98,
        )
        self.assertAlmostEqual(result["reasonable_cost_cny"], 0.013, places=3)  # round 到 4 位 = 0.013
        self.assertAlmostEqual(result["actual_cost_cny"], 0.02, places=3)
        self.assertEqual(result["judge_level"], "FAIL")

    @patch("verify_probe.http_post_json")
    def test_probe_normal(self, mock_http):
        """实际扣费 ≈ 合理 → OK"""
        mock_http.return_value = (200, {"usage": {"prompt_tokens": 100, "completion_tokens": 100}})
        result = verify_probe.run_probe(
            base_url="https://relay.example.com",
            api_key="sk-test",
            model="claude-sonnet-4",
            ratio=1.0,
            before=10.0,
            after=9.987,  # 扣 0.013 ≈ 合理 0.01296
        )
        self.assertEqual(result["judge_level"], "OK")

    @patch("verify_probe.http_post_json")
    def test_probe_unknown_model(self, mock_http):
        mock_http.return_value = (200, {})
        result = verify_probe.run_probe(
            base_url="https://x", api_key="sk", model="no-such-model",
            ratio=1.0, before=10, after=9,
        )
        self.assertIn("error", result)

    @patch("verify_probe.http_post_json")
    def test_probe_http_failure(self, mock_http):
        mock_http.return_value = (500, {"error": "internal error"})
        result = verify_probe.run_probe(
            base_url="https://x", api_key="sk", model="claude-sonnet-4",
            ratio=1.0, before=10, after=9,
        )
        self.assertIn("error", result)

    def test_ssrf_http_blocked(self):
        """P0-2: 拒绝 http（明文）"""
        result = verify_probe.run_probe("http://evil.com/v1", "sk", "gpt-4o", 1.0, 10, 9)
        self.assertIn("error", result)
        self.assertIn("https", result["error"])

    def test_ssrf_private_host_blocked(self):
        """P0-2: 拒绝内网/本地地址"""
        for bad in ["https://127.0.0.1/v1", "https://192.168.1.1/v1",
                    "https://localhost/v1", "https://169.254.169.254/v1"]:
            result = verify_probe.run_probe(bad, "sk", "gpt-4o", 1.0, 10, 9)
            self.assertIn("error", result, f"{bad} 应被拦截")

    def test_negative_balance_blocked(self):
        """P0-3: after > before 负扣费拦截"""
        result = verify_probe.run_probe("https://www.cun.ai/v1", "sk", "gpt-4o", 1.0, before=10, after=11)
        self.assertIn("error", result)
        self.assertIn("填反", result["error"])


if __name__ == "__main__":
    unittest.main()
