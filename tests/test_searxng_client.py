import asyncio
import unittest

from igngbot_v3.searxng_client import SearXNGClient


class SearXNGClientTest(unittest.TestCase):
    def test_normalize_results_deduplicates_and_limits(self):
        results = SearXNGClient._normalize_results(
            [
                {"url": "https://example.test/a", "title": " A  ", "content": " first "},
                {"url": "https://example.test/a", "title": "duplicate", "content": "second"},
                {"url": "https://example.test/b", "title": "B", "content": "third"},
            ],
            1,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "A")
        self.assertEqual(results[0]["snippet"], "first")

    def test_disabled_search_does_not_make_a_request(self):
        class Config:
            SEARXNG_ENABLED = False
            SEARXNG_BASE_URL = "http://127.0.0.1:8080"
            SEARXNG_TIMEOUT = 1
            SEARXNG_LANGUAGE = "zh-CN"
            SEARXNG_CATEGORIES = "general"
            SEARXNG_ENGINES = ""
            SEARXNG_CHAT_RESULTS = 5
            SEARXNG_TASK_RESULTS = 8
            SEARXNG_MAX_QUERY_LENGTH = 500

        result = asyncio.run(SearXNGClient(Config).search("test"))
        self.assertIn("未启用", result)

    def test_format_results_keeps_sources(self):
        result = SearXNGClient._format_results(
            "query",
            [
                {
                    "title": "Result",
                    "url": "https://example.test",
                    "snippet": "Summary",
                    "engine": "bing",
                    "published": "2026-07-21",
                }
            ],
        )
        self.assertIn("https://example.test", result)
        self.assertIn("搜索源: bing", result)
        self.assertIn("时间: 2026-07-21", result)


if __name__ == "__main__":
    unittest.main()
