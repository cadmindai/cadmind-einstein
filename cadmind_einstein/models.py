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

**Conversations** (:meth:`AnthropicModel.converse`, :meth:`OpenAIModel.converse`): the device judge
(:mod:`.judge`) holds ONE conversation of two turns per sample. Each turn is a list of blocks
(``{"type": "text", "text"}`` or ``{"type": "image", "media", "data"}``); turn 2 is sent with turn 1
and its answer, so the model has its own blind list in front of it. A :class:`Conversation` carries
each turn's answer and every request's :class:`Reply` (tokens, for :class:`Spend`). OpenRouter is
the OpenAI path (``openai:<model>`` with ``base_url https://openrouter.ai/api/v1``); a validator's
judge runs on it by default; ``--judge-backend claude-cli`` runs it on a Claude subscription
instead (:class:`.judge.ClaudeCLI`), whose dollars are notional (:meth:`Spend.add_usd`).
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

MAX_TOKENS = 16_000
EFFORT = "medium"
TOOL_ROUNDS_SLACK = 3
"""tool-call rounds past the instruments' budget before the tools are withdrawn (refused calls
cost a call each, so a model that keeps asking is made to answer)"""


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


Turn = Sequence[Mapping[str, Any]]
"""One user turn: ``{"type": "text", "text"}`` and ``{"type": "image", "media", "data": bytes}``
blocks, in order."""


@dataclass
class Conversation:
    """One judge sample's conversation, as a transport answers it."""

    texts: list[str]
    """the model's final answer to each turn, in order (fewer than the turns: it stopped early)"""
    replies: list[Reply] = field(default_factory=list)
    """every request made, with its tokens (a turn with tool calls makes several)"""
    seconds: float = 0.0
    error: str = ""
    """a transport failure that is not an exception (the CLI's exit); '' when none"""
    usd: float | None = None
    """the cost the transport itself reported (the CLI), or None: priced by :class:`Spend`"""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """``{turn, tool, input, ok, compute_ms}`` per instrument call (the tools judge only)"""

    @property
    def answered_by(self) -> str:
        return next((r.model for r in reversed(self.replies) if r.model), "")


class Converser(Protocol):
    name: str

    def converse(self, system: str, turns: Sequence[Turn], tools: Any = None) -> Conversation: ...


def _anthropic_blocks(turn: Turn) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in turn:
        if b["type"] == "text":
            out.append({"type": "text", "text": str(b["text"])})
        else:
            data = base64.standard_b64encode(bytes(b["data"])).decode("ascii")
            out.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": b["media"], "data": data},
                }
            )
    return out


def _openai_blocks(turn: Turn) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in turn:
        if b["type"] == "text":
            out.append({"type": "text", "text": str(b["text"])})
        else:
            data = base64.standard_b64encode(bytes(b["data"])).decode("ascii")
            url = f"data:{b['media']};base64,{data}"
            out.append({"type": "image_url", "image_url": {"url": url}})
    return out


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

    def converse(self, system: str, turns: Sequence[Turn], tools: Any = None) -> Conversation:
        """The judge's two turns as two requests, the second carrying the first answer. No tools:
        the tools judge runs on ``openai:`` (OpenRouter) or the subscription CLI."""
        if tools is not None:
            raise ValueError("the tools judge runs on openai:<model> (OpenRouter) or claude-cli")
        messages: list[dict[str, Any]] = []
        out = Conversation([])
        t0 = time.monotonic()
        for turn in turns:
            content = _anthropic_blocks(turn)
            if content:
                content[-1]["cache_control"] = {"type": "ephemeral"}
            messages.append({"role": "user", "content": content})
            r = self.client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=messages,
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            text = (
                ""
                if r.stop_reason == "refusal"
                else "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
            )
            u = r.usage
            out.replies.append(
                Reply(
                    text,
                    int(getattr(u, "input_tokens", 0) or 0)
                    + int(getattr(u, "cache_creation_input_tokens", 0) or 0),
                    int(getattr(u, "output_tokens", 0) or 0),
                    int(getattr(u, "cache_read_input_tokens", 0) or 0),
                    str(getattr(r, "model", "") or self.model),
                )
            )
            out.texts.append(text)
            messages.append({"role": "assistant", "content": text or "{}"})
        out.seconds = time.monotonic() - t0
        return out


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

    def _reply(self, r: Any) -> Reply:
        u = r.usage
        details = getattr(u, "prompt_tokens_details", None)
        return Reply(
            str(r.choices[0].message.content or ""),
            int(getattr(u, "prompt_tokens", 0) or 0),
            int(getattr(u, "completion_tokens", 0) or 0),
            int(getattr(details, "cached_tokens", 0) or 0),
            str(getattr(r, "model", "") or self.model),
        )

    def converse(self, system: str, turns: Sequence[Turn], tools: Any = None) -> Conversation:
        """The judge's two turns as chat requests (OpenRouter: the validator's path), the second
        carrying the first answer. With ``tools`` (an :class:`.judge_tools.Instruments`, the tools
        judge) the LAST turn may call them: they are not offered before it, and the instruments'
        gate opens only once turn 1 is answered. A tool's image comes back as a user message right
        after the tool results (a chat tool result carries text only)."""
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        out = Conversation([])
        t0 = time.monotonic()
        for k, turn in enumerate(turns):
            last = k == len(turns) - 1
            offer = tools is not None and last
            if offer:
                tools.open()
            messages.append({"role": "user", "content": _openai_blocks(turn)})
            rounds = 0
            while True:
                rounds += 1
                if offer and rounds > tools.budget + TOOL_ROUNDS_SLACK:
                    offer = False  # a model that keeps calling past its budget answers now
                kw: dict[str, Any] = {"reasoning_effort": self.effort}
                if offer:
                    kw["tools"] = tools.functions()
                else:
                    kw["response_format"] = {"type": "json_object"}
                r = self.client.chat.completions.create(model=self.model, messages=messages, **kw)
                out.replies.append(self._reply(r))
                msg = r.choices[0].message
                calls = list(getattr(msg, "tool_calls", None) or []) if offer else []
                if not calls:
                    text = str(msg.content or "")
                    out.texts.append(text)
                    messages.append({"role": "assistant", "content": text or "{}"})
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {
                                    "name": c.function.name,
                                    "arguments": c.function.arguments or "{}",
                                },
                            }
                            for c in calls
                        ],
                    }
                )
                images: list[dict[str, Any]] = []
                for c in calls:
                    try:
                        args = json.loads(c.function.arguments or "{}")
                    except ValueError:
                        args = {}
                    got = tools.call(str(c.function.name), args if isinstance(args, dict) else {})
                    out.tool_calls.append({"turn": k + 1, **got.record})
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": got.text})
                    images += [
                        {"type": "image", "media": "image/png", "data": png} for png in got.images
                    ]
                if images:
                    note = {"type": "text", "text": "The images the instruments returned:"}
                    messages.append({"role": "user", "content": _openai_blocks([note, *images])})
        out.seconds = time.monotonic() - t0
        return out


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
        return self.add_usd(self.prices.cost(reply))

    def add_usd(self, cost: float) -> float:
        """Dollars a transport reported itself (the subscription CLI's notional list price),
        counted toward the same daily cap."""
        with self._lock:
            row = self._row()
            row["usd"] = round(float(row["usd"]) + cost, 6)
            row["calls"] = int(row["calls"]) + 1
            self._put(row)
        return cost
