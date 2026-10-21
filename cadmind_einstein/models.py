"""The vision model a validator reviews with (:mod:`.review`), chosen by configuration:
``PROVIDER:MODEL`` -- ``anthropic:<model>`` (the Anthropic SDK) or ``openai:<model>`` (the OpenAI
SDK, or any server speaking its chat-completions API at ``base_url``).

Each validator pays for its own calls with its own key. The SDKs are optional dependencies
(``pip install 'cadmind-einstein[review]'``), imported only when a model is built: a validator that
does not review never needs them, and the tests use a fake (:class:`Reply` is all a model
answers).

**Cost.** Every reply carries its token counts; :class:`Spend` turns them into dollars with the
prices the operator configures (per million input and output tokens) and keeps a daily total,
refusing calls past the daily cap (an ALERT, once a day).
"""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

MAX_TOKENS = 16_000
EFFORT = "medium"


@dataclass(frozen=True)
class Reply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    model: str = ""
    """the model that answered, as the provider says (a fallback may answer)"""


class Model(Protocol):
    name: str

    def ask(self, system: str, text: str, images: Sequence[tuple[str, str, bytes]]) -> Reply: ...


class AnthropicModel:
    """Claude through the Anthropic SDK: the images as base64 blocks before the DATA text, the
    image prefix cached across a vote's samples, adaptive thinking at ``effort``, and the
    server-side refusal fallback; a refused request answers no text (an unavailable sample)."""

    def __init__(self, model: str, key: str, *, base_url: str = "", effort: str = EFFORT) -> None:
        import anthropic  # noqa: PLC0415 -- optional: only a reviewing validator needs it

        kw: dict[str, Any] = {"api_key": key}
        if base_url:
            kw["base_url"] = base_url
        self.client = anthropic.Anthropic(**kw)
        self.model, self.effort = model, effort
        self.name = f"anthropic:{model}"

    def ask(self, system: str, text: str, images: Sequence[tuple[str, str, bytes]]) -> Reply:
        content: list[dict[str, Any]] = []
        for name, media, raw in images:
            content.append({"type": "text", "text": f"image: {name}"})
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media,
                        "data": base64.standard_b64encode(raw).decode("ascii"),
                    },
                }
            )
        if content:
            content[-1]["cache_control"] = {"type": "ephemeral"}
        content.append({"type": "text", "text": text})
        r = self.client.beta.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        out = (
            ""
            if r.stop_reason == "refusal"
            else "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        )
        u = r.usage
        return Reply(
            out,
            int(getattr(u, "input_tokens", 0) or 0)
            + int(getattr(u, "cache_creation_input_tokens", 0) or 0),
            int(getattr(u, "output_tokens", 0) or 0),
            int(getattr(u, "cache_read_input_tokens", 0) or 0),
            str(getattr(r, "model", "") or self.model),
        )


class OpenAIModel:
    """A chat-completions vision model (OpenAI's SDK; ``base_url`` for a compatible server): the
    images as data URLs after the DATA text, a JSON object asked for."""

    def __init__(self, model: str, key: str, *, base_url: str = "", effort: str = EFFORT) -> None:
        import openai  # noqa: PLC0415 -- optional

        kw: dict[str, Any] = {"api_key": key}
        if base_url:
            kw["base_url"] = base_url
        self.client = openai.OpenAI(**kw)
        self.model, self.effort = model, effort
        self.name = f"openai:{model}"

    def ask(self, system: str, text: str, images: Sequence[tuple[str, str, bytes]]) -> Reply:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for name, media, raw in images:
            content.append({"type": "text", "text": f"image: {name}"})
            data = base64.standard_b64encode(raw).decode("ascii")
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}}
            )
        r = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            response_format={"type": "json_object"},
            reasoning_effort=self.effort,
        )
        u = r.usage
        details = getattr(u, "prompt_tokens_details", None)
        return Reply(
            str(r.choices[0].message.content or ""),
            int(getattr(u, "prompt_tokens", 0) or 0),
            int(getattr(u, "completion_tokens", 0) or 0),
            int(getattr(details, "cached_tokens", 0) or 0),
            str(getattr(r, "model", "") or self.model),
        )


PROVIDERS: dict[str, Callable[..., Any]] = {"anthropic": AnthropicModel, "openai": OpenAIModel}


def model_from(spec: str, key: str, *, base_url: str = "", effort: str = EFFORT) -> Model:
    """``PROVIDER:MODEL`` as a model."""
    provider, _, name = spec.partition(":")
    if provider not in PROVIDERS or not name:
        raise ValueError(f"a review model is one of {sorted(PROVIDERS)}:<model>, not {spec!r}")
    return PROVIDERS[provider](name, key, base_url=base_url, effort=effort)  # type: ignore[no-any-return]


@dataclass
class Prices:
    """US dollars per million tokens (the operator's configuration: provider prices change)."""

    input: float = 0.0
    output: float = 0.0
    cached: float = 0.0

    def cost(self, r: Reply) -> float:
        return (
            r.input_tokens * self.input
            + r.output_tokens * self.output
            + r.cached_tokens * self.cached
        ) / 1_000_000


class Spend:
    """The validator's model spend per UTC day, against ``daily_usd`` (0: no cap). Kept in the
    store (``meta/review_spend``) so a restart keeps the day's total. Past the cap every call is
    refused until the next day, and ``alert`` is told once that day."""

    def __init__(
        self,
        daily_usd: float,
        prices: Prices,
        *,
        store: Any = None,
        clock: Callable[[], float] = time.time,
        alert: Callable[[str], None] | None = None,
    ) -> None:
        self.daily_usd, self.prices, self.store, self.clock = daily_usd, prices, store, clock
        self.alert = alert or (lambda _line: None)
        self._lock = threading.Lock()
        self._mem: dict[str, Any] = {}

    def _day(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self.clock()))

    def _row(self) -> dict[str, Any]:
        row = (
            self.store.get("meta", "review_spend") if self.store is not None else self._mem
        ) or {}
        if row.get("day") != self._day():
            row = {"day": self._day(), "usd": 0.0, "calls": 0, "alerted": False}
        return dict(row)

    def _put(self, row: dict[str, Any]) -> None:
        if self.store is not None:
            self.store.put("meta", "review_spend", row)
        else:
            self._mem = row

    def today(self) -> dict[str, Any]:
        with self._lock:
            return {**self._row(), "cap_usd": self.daily_usd}

    def allow(self) -> bool:
        with self._lock:
            row = self._row()
            if not self.daily_usd or row["usd"] < self.daily_usd:
                return True
            if not row["alerted"]:
                row["alerted"] = True
                self._put(row)
                self.alert(
                    f"ALERT: the review model's daily spend cap is reached (${row['usd']:.2f} of "
                    f"${self.daily_usd:.2f} on {row['day']}): no more votes today"
                )
            return False

    def add(self, reply: Reply) -> float:
        cost = self.prices.cost(reply)
        with self._lock:
            row = self._row()
            row["usd"] = round(float(row["usd"]) + cost, 6)
            row["calls"] = int(row["calls"]) + 1
            self._put(row)
        return cost
