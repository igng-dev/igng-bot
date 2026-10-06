import unittest
from unittest.mock import patch

from igngbot_v3.config import Config
from igngbot_v4 import newapi_billing

BREAKDOWN = {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 50,
             "cache_write_tokens": 10, "reasoning_tokens": 5,
             "prompt_tokens": 160, "completion_tokens": 25, "total_tokens": 185, "cached_tokens": 60}


class ComputeQuotaTest(unittest.TestCase):
    def setUp(self):
        newapi_billing.reset()
        newapi_billing._catalog.update({
            "fetched": 1.0, "quota_per_unit": 500000, "group_ratio": {"default": 1.0},
            "models": {"m": {"quota_type": 0, "model_ratio": 0.5, "completion_ratio": 4.0,
                             "cache_ratio": 0.1, "create_cache_ratio": 1.25, "model_price": 0.0}},
            "error": "",
        })

    def tearDown(self):
        newapi_billing.reset()

    def test_token_billed_matches_newapi_settlement(self):
        # (100 + 50*0.1 + 10*1.25 + 25*4.0) * 0.5 = 108.75 -> half away from zero
        result = newapi_billing.compute_quota(BREAKDOWN, "m")
        self.assertEqual(result["quota"], 109)
        self.assertEqual(result["source"], "newapi-pricing")

    def test_observed_group_ratio_from_real_logs_wins(self):
        newapi_billing._observed["m"] = {"group_ratio": 2.0}
        self.assertEqual(newapi_billing.compute_quota(BREAKDOWN, "m")["quota"], 218)

    def test_fixed_price_model_bills_model_price(self):
        newapi_billing._catalog["models"]["fixed"] = {"quota_type": 1, "model_price": 0.002,
                                                      "model_ratio": None, "completion_ratio": None,
                                                      "cache_ratio": None, "create_cache_ratio": None}
        self.assertEqual(newapi_billing.compute_quota(BREAKDOWN, "fixed")["quota"], 1000)

    def test_unpriced_model_is_unknown_not_zero(self):
        self.assertIsNone(newapi_billing.compute_quota(BREAKDOWN, "missing"))

    def test_requested_model_is_tried_when_the_served_name_is_unpriced(self):
        self.assertEqual(newapi_billing.compute_quota(BREAKDOWN, "served-name", "m")["quota"], 109)
        self.assertEqual(newapi_billing.compute_quota(BREAKDOWN, "served-name", "m")["pricing_model"], "m")

    def test_zero_usage_is_zero_quota(self):
        zero = dict(BREAKDOWN, input_tokens=0, output_tokens=0, cache_read_tokens=0,
                    cache_write_tokens=0, reasoning_tokens=0, prompt_tokens=0,
                    completion_tokens=0, total_tokens=0, cached_tokens=0)
        self.assertEqual(newapi_billing.compute_quota(zero, "m")["quota"], 0)

    def test_positive_ratio_never_bills_zero(self):
        newapi_billing._catalog["models"]["tiny"] = {"quota_type": 0, "model_ratio": 0.0000001,
                                                     "completion_ratio": 1.0, "cache_ratio": None,
                                                     "create_cache_ratio": None, "model_price": 0.0}
        tiny = dict(BREAKDOWN, input_tokens=1, cache_read_tokens=0, cache_write_tokens=0,
                    completion_tokens=0, reasoning_tokens=0)
        self.assertEqual(newapi_billing.compute_quota(tiny, "tiny")["quota"], 1)


class LogTest(unittest.TestCase):
    def setUp(self):
        newapi_billing.reset()

    def tearDown(self):
        newapi_billing.reset()

    def test_normalize_log_parses_other_and_learns_ratios(self):
        log = newapi_billing.normalize_log({
            "id": 7, "model_name": "cc/deepseek/deepseek-v4.1-flash", "quota": 321,
            "prompt_tokens": 160, "completion_tokens": 25, "created_at": 1700000000,
            "other": '{"model_ratio": 0.15, "group_ratio": 2.0, "completion_ratio": 4.0, "cache_ratio": 0.02}',
        })
        self.assertEqual(log["quota"], 321)
        self.assertEqual(log["other"]["cache_ratio"], 0.02)
        newapi_billing.remember_ratios(log)
        self.assertEqual(newapi_billing._observed[log["model"]]["group_ratio"], 2.0)

    def test_log_match_prefers_closest_time_and_exact_tokens(self):
        newapi_billing._logs["items"] = [
            {"id": 1, "model": "m", "prompt_tokens": 160, "completion_tokens": 25, "created_at": 1700000000, "quota": 100},
            {"id": 2, "model": "m", "prompt_tokens": 160, "completion_tokens": 25, "created_at": 1700000050, "quota": 200},
            {"id": 3, "model": "m", "prompt_tokens": 1, "completion_tokens": 1, "created_at": 1700000050, "quota": 999},
        ]
        matched = newapi_billing._match_log(BREAKDOWN, ("m", None), 1700000051 * 1000)
        self.assertEqual(matched["id"], 2)

    def test_log_match_accepts_the_requested_alias(self):
        newapi_billing._logs["items"] = [
            {"id": 4, "model": "billing-name", "prompt_tokens": 160, "completion_tokens": 25, "created_at": 1700000000, "quota": 777},
        ]
        self.assertEqual(newapi_billing._match_log(BREAKDOWN, ("served-name", "billing-name"), 1700000000 * 1000)["id"], 4)

    def test_log_match_ignores_logs_outside_the_window(self):
        newapi_billing._logs["items"] = [
            {"id": 1, "model": "m", "prompt_tokens": 160, "completion_tokens": 25, "created_at": 1700000000, "quota": 100},
        ]
        self.assertIsNone(newapi_billing._match_log(BREAKDOWN, ("m", None), 1700009999 * 1000))


class CostForTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        newapi_billing.reset()
        newapi_billing._catalog.update({
            "fetched": 1.0, "quota_per_unit": 500000, "group_ratio": {"default": 1.0},
            "models": {"m": {"quota_type": 0, "model_ratio": 0.5, "completion_ratio": 4.0,
                             "cache_ratio": 0.1, "create_cache_ratio": 1.25, "model_price": 0.0}},
            "error": "",
        })

    def tearDown(self):
        newapi_billing.reset()

    async def test_real_log_quota_wins_over_computed_quota(self):
        newapi_billing._logs["items"] = [
            {"id": 9, "model": "m", "prompt_tokens": 160, "completion_tokens": 25, "created_at": 1700000000, "quota": 4242},
        ]
        with patch.object(Config, "NEWAPI_BASE_URL", "https://newapi.invalid"), \
             patch.object(newapi_billing, "refresh_catalog", new=_noop), \
             patch.object(newapi_billing, "refresh_logs", new=_noop):
            cost = await newapi_billing.cost_for(BREAKDOWN, "m", 1700000000 * 1000)
        self.assertEqual(cost["source"], "newapi-log")
        self.assertEqual(cost["quota"], 4242)
        self.assertAlmostEqual(cost["usd"], 4242 / 500000)

    async def test_disabled_adapter_is_unknown(self):
        with patch.object(Config, "NEWAPI_BASE_URL", ""):
            self.assertIsNone(await newapi_billing.cost_for(BREAKDOWN, "m"))

    async def test_real_log_quota_matches_the_requested_alias(self):
        newapi_billing._logs["items"] = [
            {"id": 11, "model": "billing-name", "prompt_tokens": 160, "completion_tokens": 25, "created_at": 1700000000, "quota": 555},
        ]
        with patch.object(Config, "NEWAPI_BASE_URL", "https://newapi.invalid"), \
             patch.object(newapi_billing, "refresh_catalog", new=_noop), \
             patch.object(newapi_billing, "refresh_logs", new=_noop):
            cost = await newapi_billing.cost_for(BREAKDOWN, "served-name", 1700000000 * 1000, "billing-name")
        self.assertEqual(cost["source"], "newapi-log")
        self.assertEqual(cost["quota"], 555)
        self.assertEqual(cost["pricing_model"], "billing-name")


async def _noop(*args, **kwargs):
    return None


if __name__ == "__main__":
    unittest.main()
