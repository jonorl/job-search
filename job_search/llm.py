"""Thin wrapper over Gemini structured output, shared by scoring and tailoring."""

import logging
import random
import time
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client()
    return _client


def generate(model: str, system: str, prompt: str, schema: type[T], thinking: str = "LOW") -> T | None:
    """Returns the parsed result, or None if the model gave nothing usable or kept failing."""
    config = types.GenerateContentConfig(
        system_instruction=system,
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking)),
    )
    for attempt in range(5):
        try:
            response = client().models.generate_content(model=model, contents=prompt, config=config)
            break
        except errors.ClientError as e:
            # The free tier's per-minute limit is low, so back off on 429; other 4xx would fail every call.
            if e.code != 429:
                raise
            time.sleep(2**attempt * 5 + random.random())
        except errors.ServerError as e:
            log.warning("%s failed: %r", model, e)
            time.sleep(5)
    else:
        log.warning("%s gave up after repeated failures", model)
        return None
    if not isinstance(response.parsed, schema):
        reason = response.candidates[0].finish_reason if response.candidates else "no candidates"
        log.warning("%s returned nothing usable (%s)", model, reason)
        return None
    return response.parsed
