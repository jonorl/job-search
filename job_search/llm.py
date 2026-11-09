"""Structured output from a chain of models, shared by scoring and tailoring.

Each step lists models in order of preference. Free-tier quotas are per model, so when one runs out
for the day (or is overloaded) the next takes over. A bare name goes to Gemini, "groq/<model>" to Groq,
and "otari/<model or policy>" to an Otari gateway, which can run its own fallbacks server-side.
"""

import json
import logging
import os
import random
import re
import threading
import time
from typing import TypeVar

import groq
import httpx
import openai
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class QuotaExhausted(Exception):
    """Every model in the chain is out of quota or unusable; retrying today won't help."""


class _Skip(Exception):
    """This model can't serve the request right now; try the next one."""

    def __init__(self, reason: str, strike: bool = False):
        super().__init__(reason)
        # Only outages count towards dropping a model for the run; rate limits pass within a minute.
        self.strike = strike


# Seconds per request; slow thinking calls finish well inside it.
CALL_TIMEOUT = 120
_lock = threading.Lock()
_gemini: genai.Client | None = None
_chat_clients: dict[str, groq.Groq | openai.OpenAI] = {}
# Models that ran out of daily quota, rejected the request or kept failing, so later calls in this run
# skip straight past them.
_dead: set[str] = set()
_strikes: dict[str, int] = {}


def reset() -> None:
    """Gives every model another chance; the web server lives across days, and quotas reset daily."""
    with _lock:
        _dead.clear()
        _strikes.clear()


def _gemini_client() -> genai.Client:
    global _gemini
    # genai.Client closes its connection when garbage-collected, so two threads racing to create one
    # would leave the loser's in-flight request on a closed client.
    with _lock:
        if _gemini is None:
            # The SDK waits forever by default, which leaves a hung call holding up the whole chain.
            _gemini = genai.Client(http_options=types.HttpOptions(timeout=CALL_TIMEOUT * 1000))
        return _gemini


def _chat_client(backend: str) -> groq.Groq | openai.OpenAI:
    with _lock:
        if backend not in _chat_clients:
            # Retries are handled here, with the fallback chain; the SDK's own would double them up.
            if backend == "groq":
                _chat_clients[backend] = groq.Groq(max_retries=0)
            else:
                api_key = os.environ.get("OTARI_API_KEY")
                if not api_key:
                    raise _Skip("OTARI_API_KEY is not set")
                _chat_clients[backend] = openai.OpenAI(
                    base_url=os.environ.get("OTARI_BASE_URL") or "https://api.otari.ai/api/v1",
                    api_key=api_key,
                    max_retries=0,
                    timeout=CALL_TIMEOUT,
                )
        return _chat_clients[backend]


def _strict_schema(schema: type[BaseModel]) -> dict:
    """Groq's strict mode needs refs inlined, every property required and no extra properties."""
    raw = schema.model_json_schema()
    defs = raw.pop("$defs", {})

    def fix(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return fix(defs[node["$ref"].rsplit("/", 1)[-1]])
            node = {k: fix(v) for k, v in node.items()}
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            return node
        if isinstance(node, list):
            return [fix(v) for v in node]
        return node

    return fix(raw)


def _wait(model: str, seconds: float | None, attempt: int) -> None:
    # Per-minute limits reset within a minute, so honour the server's hint rather than guessing.
    wait = min(seconds, 60) if seconds else 2**attempt * 5
    wait += random.random()
    log.info("%s rate-limited, waiting %.0fs", model, wait)
    time.sleep(wait)


def _call_gemini(model: str, system: str, prompt: str, schema: type[T], thinking: str) -> T | None:
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking)),
    )
    unavailable = 0
    for attempt in range(4):
        try:
            response = _gemini_client().models.generate_content(model=model, contents=prompt, config=config)
        except errors.ClientError as e:
            if e.code == 429 and "PerDay" in str(e):
                _dead.add(model)
                raise _Skip("daily quota used up")
            if e.code != 429:
                _dead.add(model)
                raise _Skip(f"rejected: {e}")
            delay = re.search(r"'retryDelay': '(\d+)s'", str(e))
            _wait(model, int(delay.group(1)) if delay else None, attempt)
            continue
        except httpx.TimeoutException:
            # Another full wait on the same model is worse than moving down the chain.
            raise _Skip(f"no answer within {CALL_TIMEOUT}s", strike=True)
        except errors.ServerError as e:
            unavailable += 1
            if unavailable >= 2:
                raise _Skip(f"unavailable ({e.code})", strike=True)
            time.sleep(5)
            continue
        if isinstance(response.parsed, schema):
            return response.parsed
        reason = response.candidates[0].finish_reason if response.candidates else "no candidates"
        log.warning("%s returned nothing usable (%s)", model, reason)
        return None
    raise _Skip("still rate-limited")


def _call_chat(backend: str, model: str, system: str, prompt: str, schema: type[T], thinking: str) -> T | None:
    """An OpenAI-style chat completion, for Groq and for Otari, whose SDKs share error types."""
    sdk = groq if backend == "groq" else openai
    name = f"{backend}/{model}"
    try:
        client = _chat_client(backend)
    except (groq.GroqError, _Skip) as e:
        _dead.add(name)
        raise _Skip(str(e))
    unavailable = 0
    for attempt in range(4):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema.__name__, "strict": True, "schema": _strict_schema(schema)},
                },
                reasoning_effort=thinking.lower(),
                # The default leaves too little room after reasoning, which truncates a full CV.
                max_completion_tokens=16000,
            )
        except sdk.RateLimitError as e:
            # Groq reports daily limits as "per day" (RPD/TPD); anything shorter is worth waiting out.
            if "per day" in str(e).lower():
                _dead.add(name)
                raise _Skip("daily quota used up")
            retry_after = e.response.headers.get("retry-after")
            _wait(name, float(retry_after) if retry_after else None, attempt)
            continue
        except sdk.APIConnectionError:
            unavailable += 1
            if unavailable >= 2:
                raise _Skip("unreachable", strike=True)
            time.sleep(5)
            continue
        except sdk.APIStatusError as e:
            if e.status_code >= 500:
                unavailable += 1
                if unavailable >= 2:
                    raise _Skip(f"unavailable ({e.status_code})", strike=True)
                time.sleep(5)
                continue
            if "json_validate_failed" in str(e):
                # A one-off bad generation, not a broken model; let the next model take this request.
                raise _Skip("output didn't match the schema")
            # Otari answers 402 for a spent budget and 424 for a provider it has no credential for;
            # neither changes within a run, nor does any other refusal.
            _dead.add(name)
            raise _Skip(f"rejected: {e}")
        choice = response.choices[0]
        if choice.finish_reason != "stop" or not choice.message.content:
            log.warning("%s returned nothing usable (%s)", name, choice.finish_reason)
            return None
        return schema.model_validate(json.loads(choice.message.content))
    raise _Skip("still rate-limited")


def generate(models: list[str], system: str, prompt: str, schema: type[T], thinking: str = "LOW") -> T | None:
    """Returns the parsed result from the first model that gives one, or None if none did.

    Raises QuotaExhausted once every model is out for the day, so callers can stop instead of
    failing every remaining job one by one.
    """
    tried = False
    for model in models:
        if model in _dead:
            continue
        tried = True
        try:
            backend, _, rest = model.partition("/")
            if backend in ("groq", "otari") and rest:
                result = _call_chat(backend, rest, system, prompt, schema, thinking)
            else:
                result = _call_gemini(model, system, prompt, schema, thinking)
        except _Skip as e:
            # An overloaded model tends to stay overloaded, and each try costs ~10s of retries.
            if e.strike:
                _strikes[model] = _strikes.get(model, 0) + 1
                if _strikes[model] >= 2:
                    _dead.add(model)
            log.warning("%s skipped%s: %s", model, " for the rest of this run" if model in _dead else "", e)
            continue
        if result is not None:
            return result
    if not tried or all(m in _dead for m in models):
        raise QuotaExhausted(", ".join(models))
    return None
