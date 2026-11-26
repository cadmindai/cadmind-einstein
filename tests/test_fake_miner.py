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


class TestMain(unittest.TestCase):
    """The scripted miner's loop against a scripted validator."""

    def run_miner(self, admission: dict[str, Any], works: list[dict[str, Any]]) -> list[tuple]:
        import tempfile
        from pathlib import Path

        sent: list[tuple[str, dict[str, Any]]] = []
        queue = list(works)

        def signed(_kp: Any, _url: str, path: str, body: dict[str, Any], *_a: Any) -> tuple:
            sent.append((path, dict(body)))
            if path == "/v1/miners/admission":
                return 200, dict(admission)
            if path == "/v1/jobs/work":
                return 200, (queue.pop(0) if queue else {"job": None})
            return 200, {}

        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(fake_miner, "signed", signed),
            mock.patch.object(fake_miner, "ask_key", lambda *_a, **_k: (200, {"key_id": "k1"})),
            mock.patch.object(fake_miner, "Mcp", mock.MagicMock()),
            mock.patch.object(fake_miner, "build", lambda *_a, **_k: "sha256:d"),
            mock.patch.object(fake_miner.time, "sleep", lambda _s: None),
            mock.patch("sys.stderr", io.StringIO()),
        ):
            key = Path(d) / "agent.key"
            key.write_text("k\n")
            code = fake_miner.main(
                [
                    "--uri", "//Alice", "--validator", "v", "--validator-hotkey", "h",
                    "--platform", "p", "--agent-key-file", str(key), "--project", d,
                    "--jobs", "1", "--idle-s", "0.2",
                ]
            )  # fmt: skip
        self.assertEqual(code, 0)
        return sent

    def test_a_retired_entry_test_is_not_waited_for(self) -> None:
        retired = {"admitted": True, "retired": True, "lane": "improvement", "rating": 0.0}
        job = {
            "job": "imp-000000000001",
            "workspace": "ag-1",
            "improvement": True,
            "no_questions": True,
            "base": {"kind": "delivered", "workspace": "ag-2"},
        }
        sent = self.run_miner(retired, [job, {"job": "never", "workspace": "ag-9"}])
        # one job asked for, one built: with no entry test, the improvement job is that one
        paths = [(p, b.get("job")) for p, b in sent if p != "/v1/jobs/work"]
        self.assertEqual(
            paths,
            [
                ("/v1/miners/admission", None),
                ("/v1/jobs/accept", "imp-000000000001"),
                ("/v1/jobs/submit", "imp-000000000001"),
            ],
        )
        submit = next(b for p, b in sent if p == "/v1/jobs/submit")
        self.assertEqual((submit["workspace"], submit["digest"]), ("ag-1", "sha256:d"))

    def test_with_an_entry_test_it_is_built_first(self) -> None:
        jobs = [
            {"job": "admission-1", "workspace": "ag-1", "no_questions": True},
            {"job": "j1", "workspace": "ag-2"},
        ]
        sent = self.run_miner({"job": "admission-1", "state": "queued"}, jobs)
        self.assertEqual(
            [b["job"] for p, b in sent if p == "/v1/jobs/submit"], ["admission-1", "j1"]
        )
