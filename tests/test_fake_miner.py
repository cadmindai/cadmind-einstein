"""The scripted miner's own: what it waits out, and what it decides when it may not ask."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from typing import Any
from unittest import mock

from cadmind_einstein import answers, fake_miner

DECISIONS = """# a comment
[decision.pad]
question = "Which pad?"
options = { a = "A", b = "B" }
by = "client"
status = "open"
assumption = "a"
because = "a common size"

[decision.mine]
question = "Which screw?"
options = { c = "C" }
by = "agent"
status = "decided"
choice = "c"
because = "it is there"
"""


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x/mcp", code, "no", {}, io.BytesIO(b""))  # type: ignore[arg-type]


class _Answer(io.BytesIO):
    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class TestRetries(unittest.TestCase):
    def rpc(self, *raised: Exception) -> tuple[Any, list[float]]:
        waits: list[float] = []
        ok = _Answer(json.dumps({"result": {"fine": True}}).encode())
        with (
            mock.patch("urllib.request.urlopen", side_effect=[*raised, ok]),
            mock.patch("time.sleep", waits.append),
        ):
            return fake_miner.Mcp("http://x", "k")._rpc("ping", {}), waits

    def test_a_proxy_error_or_a_rate_limit_is_waited_out(self) -> None:
        for code in (429, 502, 503, 504):
            with self.subTest(code=code):
                out, waits = self.rpc(_http(code), _http(code))
                self.assertEqual(out, {"fine": True})
                self.assertEqual(waits, [2, 5])

    def test_anything_else_is_final(self) -> None:
        for code in (400, 401, 404, 500):
            with self.subTest(code=code), self.assertRaises(urllib.error.HTTPError):
                self.rpc(_http(code))

    def test_it_gives_up_after_its_waits(self) -> None:
        with self.assertRaises(urllib.error.HTTPError):
            self.rpc(*[_http(502)] * 6)


class TestDecideOpen(unittest.TestCase):
    def test_an_open_client_decision_becomes_the_builders_own_on_its_assumption(self) -> None:
        out = answers.decisions_of({"spec/decisions.toml": fake_miner.decide_open(DECISIONS)})
        self.assertEqual(out["pad"]["by"], "agent")
        self.assertEqual(out["pad"]["status"], "decided")
        self.assertEqual(out["pad"]["choice"], "a")
        self.assertNotIn("assumption", out["pad"])
        self.assertEqual(out["pad"]["because"], "a common size")

    def test_nothing_else_changes_and_nothing_stays_open(self) -> None:
        said = fake_miner.decide_open(DECISIONS)
        self.assertIn("# a comment\n", said)
        self.assertEqual(fake_miner.decide_open(said), said)
        out = answers.decisions_of({"spec/decisions.toml": said})
        self.assertEqual(answers.open_client(out), [])
        self.assertEqual(out["mine"]["choice"], "c")


if __name__ == "__main__":
    unittest.main()


class TestHostRefused(unittest.TestCase):
    def test_a_watt_answer_the_host_refused_stops_the_build(self) -> None:
        mcp = fake_miner.Mcp("http://x", "k")
        refused = {
            "state": "done",
            "ok": False,
            "host_refused": {"code": "host_refused", "why": "a remote run writes only build/"},
        }
        with (
            mock.patch.object(mcp, "call", return_value=refused),
            self.assertRaises(fake_miner.HostRefusedError) as got,
        ):
            mcp.watt("ws", "test", mutate=True)
        self.assertEqual(str(got.exception), "a remote run writes only build/")
        with mock.patch.object(mcp, "call", return_value={"state": "done", "ok": False}):
            self.assertEqual(mcp.watt("ws", "test"), {"state": "done", "ok": False})
