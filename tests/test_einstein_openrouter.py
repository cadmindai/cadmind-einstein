"""The judge on OpenRouter: the base URL a validator calls, and a daily cap that counts the dollars
OpenRouter reports (or the operator's prices, or refuses to start)."""

from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from cadmind_einstein import judge as jm
from cadmind_einstein.daemon import check_review_cost, parser
from cadmind_einstein.models import (
    OPENROUTER_BASE_URL,
    OpenAIModel,
    Prices,
    Reply,
    Spend,
    base_url_for,
    cost_problem,
    reported_cost,
    reports_cost,
)


class FakeOpenAI:
    """The ``openai`` module as far as :class:`OpenAIModel` uses it: records the client's kwargs and
    each request, and answers with a usage carrying ``cost`` (or not)."""

    def __init__(self, cost: Any = 0.0123) -> None:
        self.clients: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.cost = cost
        self.module = types.ModuleType("openai")
        self.module.OpenAI = self._client  # type: ignore[attr-defined]

    def _client(self, **kw: Any) -> Any:
        self.clients.append(kw)

        def create(**req: Any) -> Any:
            self.requests.append(req)
            usage: dict[str, Any] = {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "prompt_tokens_details": SimpleNamespace(cached_tokens=0),
            }
            if self.cost is not None:
                usage["cost"] = self.cost
            msg = SimpleNamespace(content='{"ok": true}', tool_calls=None)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=msg)],
                usage=SimpleNamespace(**usage),
                model="anthropic/claude-opus-5.5",
            )

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class TestBaseURL(unittest.TestCase):
    def test_an_openai_model_without_a_base_url_goes_to_openrouter(self) -> None:
        self.assertEqual(OPENROUTER_BASE_URL, "https://openrouter.ai/api/v1")
        self.assertEqual(base_url_for("openai:anthropic/claude-opus-5.5"), OPENROUTER_BASE_URL)
        self.assertEqual(
            base_url_for("openai:x", "https://example.test/v1"), "https://example.test/v1"
        )
        self.assertEqual(base_url_for("anthropic:claude-opus-5-5"), "")

    def test_the_validator_model_never_calls_the_openai_api_by_accident(self) -> None:
        fake = FakeOpenAI()
        with mock.patch.dict(sys.modules, {"openai": fake.module}):
            m = jm.validator_model("openrouter", "openai:anthropic/claude-opus-5.5", "k")
            jm.model_for_dev("openai:anthropic/claude-opus-5.5", "k")
            jm.validator_model("openrouter", "openai:x", "k", base_url="https://example.test/v1")
        self.assertIsInstance(m, OpenAIModel)
        self.assertEqual(fake.clients[0]["base_url"], OPENROUTER_BASE_URL)
        self.assertEqual(fake.clients[1]["base_url"], OPENROUTER_BASE_URL)
        self.assertEqual(fake.clients[2]["base_url"], "https://example.test/v1")


class TestReportedCost(unittest.TestCase):
    def test_usage_cost_is_read(self) -> None:
        self.assertEqual(reported_cost(SimpleNamespace(cost=0.5)), 0.5)
        self.assertEqual(reported_cost({"cost": "0.25"}), 0.25)
        self.assertEqual(reported_cost(SimpleNamespace(cost=0)), 0.0)
        for bad in (SimpleNamespace(), SimpleNamespace(cost=None), {"cost": "x"}, {"cost": -1}):
            self.assertIsNone(reported_cost(bad))
        self.assertIsNone(reported_cost({"cost": float("nan")}))
        self.assertIsNone(reported_cost({"cost": True}))

    def test_a_reply_carries_the_cost_and_both_paths_read_it(self) -> None:
        fake = FakeOpenAI(cost=0.0123)
        with mock.patch.dict(sys.modules, {"openai": fake.module}):
            m = OpenAIModel("anthropic/claude-opus-5.5", "k", base_url=OPENROUTER_BASE_URL)
        r = m.ask("sys", "text", [])
        self.assertEqual(r.cost_usd, 0.0123)
        conv = m.converse("sys", [[{"type": "text", "text": "turn 1"}]] * 2)
        self.assertEqual([x.cost_usd for x in conv.replies], [0.0123, 0.0123])
        for req in fake.requests:
            self.assertEqual(req["response_format"], {"type": "json_object"})
            self.assertIn("reasoning_effort", req)

    def test_reports_cost_only_on_openrouter(self) -> None:
        self.assertTrue(reports_cost("openai:x", OPENROUTER_BASE_URL))
        self.assertFalse(reports_cost("openai:x", "https://api.openai.com/v1"))
        self.assertFalse(reports_cost("openai:x", "https://openrouter.ai.evil.test/v1"))
        self.assertFalse(reports_cost("anthropic:x", OPENROUTER_BASE_URL))


class TestSpendCountsRealMoney(unittest.TestCase):
    def test_the_reported_cost_wins_over_the_prices(self) -> None:
        spend = Spend(1.0, Prices(input=1.0, output=1.0), clock=Clock())
        self.assertEqual(spend.add(Reply("x", 1_000_000, 0, cost_usd=0.4)), 0.4)
        self.assertEqual(spend.today()["usd"], 0.4)

    def test_a_cap_on_openrouter_trips_on_the_reported_cost_with_no_prices(self) -> None:
        said: list[str] = []
        spend = Spend(1.0, Prices(), clock=Clock(), alert=said.append)
        spend.add(Reply("x", 20_000, 4_000, cost_usd=0.6))
        self.assertTrue(spend.allow())
        spend.add(Reply("x", 20_000, 4_000, cost_usd=0.6))
        self.assertFalse(spend.allow())
        self.assertEqual(len(said), 1)

    def test_a_missing_cost_falls_back_to_the_prices(self) -> None:
        spend = Spend(1.0, Prices(input=4.0, output=20.0), clock=Clock())
        self.assertAlmostEqual(spend.add(Reply("x", 100_000, 10_000)), 0.6)
        self.assertTrue(spend.allow())

    def test_a_missing_cost_with_no_prices_stops_the_day(self) -> None:
        said: list[str] = []
        clock = Clock()
        spend = Spend(5.0, Prices(), clock=clock, alert=said.append)
        spend.add(Reply("x", 100_000, 10_000))  # no cost, nothing to price it with
        self.assertFalse(spend.allow())
        self.assertFalse(spend.allow())
        self.assertEqual(len(said), 1)
        self.assertIn("without a cost", said[0])
        clock.t += 86_400
        self.assertTrue(spend.allow())
        # no cap: nothing to keep
        free = Spend(0.0, Prices(), clock=clock)
        free.add(Reply("x", 100_000, 10_000))
        self.assertTrue(free.allow())

    def test_cost_problem(self) -> None:
        self.assertEqual(cost_problem(0.0, Prices(), reported=False), "")
        self.assertEqual(cost_problem(10.0, Prices(), reported=True), "")
        self.assertEqual(cost_problem(10.0, Prices(input=1.0), reported=False), "")
        self.assertIn("would count $0", cost_problem(10.0, Prices(), reported=False))


class TestValidatorStart(unittest.TestCase):
    def args(self, *extra: str) -> Any:
        return parser().parse_args(
            [
                "--state-dir",
                "/nonexistent",
                "--token-file",
                "/nonexistent/t",
                "--platform",
                "http://127.0.0.1:1",
                "--operator-key-file",
                "/nonexistent/o",
                *extra,
            ]
        )

    def test_openrouter_with_a_cap_and_no_prices_starts(self) -> None:
        check_review_cost(self.args("--review-model", "openai:anthropic/claude-opus-5.5"))

    def test_another_server_with_a_cap_and_no_prices_is_refused(self) -> None:
        a = self.args("--review-model", "openai:x", "--review-base-url", "https://example.test/v1")
        with self.assertRaises(SystemExit) as stop:
            check_review_cost(a)
        self.assertIn("--review-usd-per-mtok", str(stop.exception))
        with self.assertRaises(SystemExit):
            check_review_cost(self.args("--review-model", "anthropic:claude-opus-5-5"))
        # with prices, or without a cap, it starts
        check_review_cost(
            self.args(
                "--review-model",
                "openai:x",
                "--review-base-url",
                "https://example.test/v1",
                "--review-usd-per-mtok-in",
                "4",
            )
        )
        check_review_cost(
            self.args(
                "--review-model",
                "openai:x",
                "--review-base-url",
                "https://example.test/v1",
                "--review-daily-usd",
                "0",
            )
        )

    def test_the_subscription_and_no_votes_are_not_checked(self) -> None:
        check_review_cost(
            self.args("--judge-backend", "claude-cli", "--review-model", "claude-opus-5-5")
        )
        check_review_cost(self.args("--votes", "off", "--review-model", "anthropic:x"))


if __name__ == "__main__":
    unittest.main()
