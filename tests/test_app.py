"""Offline regression tests: no browser launch, live profile, or provider calls."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_app():
    # Import from a temporary copy: import-time state/profile I/O stays isolated.
    spec = importlib.util.spec_from_file_location("monitor_test_app", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    with tempfile.TemporaryDirectory() as directory:
        module.__file__ = str(Path(directory) / "app.py")
        spec.loader.exec_module(module)
    return module


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.app = load_app()

    def test_codex_windows_use_duration_not_position(self):
        body = {"rate_limit": {"primary_window": {"limit_window_seconds": 604800, "used_percent": 30, "reset_at": 1800000000},
                               "secondary_window": {"limit_window_seconds": 18000, "used_percent": 12}}}
        result = self.app.provider_updates("codex", "https://chatgpt.com/backend-api/wham/usage", body)
        self.assertEqual(result["weekly_remaining"], 70.0)
        self.assertEqual(result["five_hour_remaining"], 88.0)
        self.assertTrue(result["weekly_reset"].endswith("+00:00"))

    def test_claude_null_windows_and_zero_usage(self):
        result = self.app.provider_updates("claude", "https://claude.ai/api/organizations/example/usage",
                                           {"five_hour": {"utilization": 0}, "seven_day": None})
        self.assertEqual(result["five_hour_remaining"], 100.0)
        self.assertNotIn("weekly_used", result)

    def test_unrelated_response_is_ignored(self):
        self.assertEqual(self.app.provider_updates("codex", "https://chatgpt.com/other", {}), {})

    def test_safe_url_removes_queries_and_challenges(self):
        self.assertEqual(self.app.safe_url("https://example.com/usage?session=example#fragment"), "https://example.com/usage")
        self.assertEqual(self.app.safe_url("https://example.com/cdn-cgi/example"), "https://example.com/<redacted-challenge>")

    def test_initial_provider_states_are_independent(self):
        state = self.app.initial_state()
        state["codex"]["weekly_used"] = 12
        self.assertIsNone(state["claude"]["weekly_used"])


class StateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = load_app()

    async def test_timestamp_only_response_does_not_change_usage_timestamp(self):
        app = self.app
        app.state = app.initial_state()
        app.state["codex"].update(five_hour_used=20.0,
                                  last_provider_update="old-response",
                                  last_detected_change="old-change")
        with tempfile.TemporaryDirectory() as directory:
            app.STATE_FILE = Path(directory) / "state.json"
            with patch.object(app, "broadcast", new_callable=AsyncMock) as broadcast:
                await app.set_provider("codex", {"five_hour_used": 20.0,
                                                  "last_provider_update": "new-response"})
                self.assertEqual(app.state["codex"]["last_detected_change"], "old-change")
                self.assertEqual(json.loads(app.STATE_FILE.read_text())["codex"]["last_provider_update"], "new-response")
                broadcast.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
