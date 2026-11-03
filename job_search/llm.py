"""Structured output from a chain of models, shared by scoring and tailoring.

Each step lists models in order of preference. Free-tier quotas are per model, so when one runs out
for the day (or is overloaded) the next takes over; "groq/<model>" entries go to Groq instead of Gemini.
"""

import json
import logging
import random
import threading
import time
from typing import TypeVar

import groq
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class QuotaExhausted(Exception):
    """Every model in the chain is out of quota or unusable; retrying today won't help."""


class _Skip(Exception):
    """This model can't serve the request right now; try the next one."""


_lock = threading.Lock()
_gemini: genai.Client | None = None
_groq: groq.Groq | None = None
# Models that ran out of daily quota, rejected the request or kept failing, so later calls in this run
# skip straight past them.
_dead: set[str] = set()
_strikes: dict[str, int] = {}


def _gemini_client() -> genai.Client:
    global _gemini
    # genai.Client closes its connection when garbage-collected, so two threads racing to create one
    # would leave the loser's in-flight request on a closed client.
    with _lock:
        if _gemini is None:
            _gemini = genai.Client()
        return _gemini


def _groq_client() -> groq.Groq:
    global _groq
    with _lock:
        if _groq is None:
            _groq = groq.Groq()
        return _groq


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


def _call_gemini(model: str, system: str, prompt: str, schema: type[T], thinking: str) -> T | None:
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking)),
    )
    for attempt in range(3):
        try:
            response = _gemini_client().models.generate_content(model=model, contents=prompt, config=config)
        except errors.ClientError as e:
            if e.code == 429 and "PerDay" in str(e):
                _dead.add(model)
                raise _Skip("daily quota used up")
            if e.code != 429:
                _dead.add(model)
                raise _Skip(f"rejected: {e}")
            wait = 2**attempt * 5 + random.random()
            log.info("%s rate-limited, waiting %.0fs", model, wait)
            time.sleep(wait)
            continue
        except errors.ServerError as e:
            log.info("%s unavailable (%s), retrying", model, e.code)
            time.sleep(5)
            continue
        if isinstance(response.parsed, schema):
            return response.parsed
        reason = response.candidates[0].finish_reason if response.candidates else "no candidates"
        log.warning("%s returned nothing usable (%s)", model, reason)
        return None
    raise _Skip("still rate-limited or unavailable")


def _call_groq(model: str, system: str, prompt: str, schema: type[T], thinking: str) -> T | None:
    try:
        client = _groq_client()
    except groq.GroqError as e:
        _dead.add(f"groq/{model}")
        raise _Skip(str(e))
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema.__name__, "strict": True, "schema": _strict_schema(schema)},
                },
                reasoning_effort=thinking.lower(),
            )
        except groq.RateLimitError as e:
            # Groq reports daily limits as "per day" (RPD/TPD); anything shorter is worth waiting out.
            if "per day" in str(e).lower():
                _dead.add(f"groq/{model}")
                raise _Skip("daily quota used up")
            wait = 2**attempt * 5 + random.random()
            log.info("groq/%s rate-limited, waiting %.0fs", model, wait)
            time.sleep(wait)
            continue
        except groq.APIConnectionError:
            time.sleep(5)
            continue
        except groq.APIStatusError as e:
            if e.status_code >= 500:
                time.sleep(5)
                continue
            _dead.add(f"groq/{model}")
            raise _Skip(f"rejected: {e}")
        choice = response.choices[0]
        if choice.finish_reason != "stop" or not choice.message.content:
            log.warning("groq/%s returned nothing usable (%s)", model, choice.finish_reason)
            return None
        return schema.model_validate(json.loads(choice.message.content))
    raise _Skip("still rate-limited or unavailable")


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
            if model.startswith("groq/"):
                result = _call_groq(model.removeprefix("groq/"), system, prompt, schema, thinking)
            else:
                result = _call_gemini(model, system, prompt, schema, thinking)
        except _Skip as e:
            # An overloaded model tends to stay overloaded, and each try costs ~25s of retries.
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
