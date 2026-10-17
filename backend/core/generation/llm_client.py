"""
llm_client.py — one text-completion entry point over the configured LLM provider.

    complete(messages, temperature=None, max_tokens=None) -> str

`messages` use the generic chat shape [{"role": "system"|"user"|"assistant",
"content": str}, ...]. settings.LLM_PROVIDER picks the backend:

  "groq"   — Groq's OpenAI-compatible chat API (settings.GROQ_MODEL)
  "gemini" — Google Gemini (settings.LLM_MODEL)

Both are rate-limited on free tiers. Per-minute limits are waited out;
anything else (bad key, per-day quota) raises LLMError, which callers turn
into a refusal or an HTTP 503 rather than a bare 500.
"""

from __future__ import annotations

import re
import time
from typing import List, Optional

from loguru import logger

from config import settings


class LLMError(RuntimeError):
    """The provider couldn't produce a completion."""


_RETRY_DELAY_RE = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)
_MAX_ATTEMPTS = 4


def complete(
    messages: List[dict],
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> str:
    temperature = settings.TEMPERATURE if temperature is None else temperature
    max_tokens = max_tokens or settings.MAX_TOKENS

    provider = settings.LLM_PROVIDER.lower()
    if provider == "groq":
        return _complete_groq(messages, temperature, max_tokens)
    if provider == "gemini":
        return _complete_gemini(messages, temperature, max_tokens)
    raise LLMError(f"Unknown LLM_PROVIDER '{settings.LLM_PROVIDER}' — use 'groq' or 'gemini'")


def model_name() -> str:
    return settings.GROQ_MODEL if settings.LLM_PROVIDER.lower() == "groq" else settings.LLM_MODEL


# ── Groq ──────────────────────────────────────────────────────────────────────

_groq_client = None


def _complete_groq(messages: List[dict], temperature: float, max_tokens: int) -> str:
    global _groq_client
    import groq

    if not settings.GROQ_API_KEY:
        raise LLMError("LLM_PROVIDER is 'groq' but GROQ_API_KEY is not set in backend/.env")
    if _groq_client is None:
        # the SDK retries 429s itself, honoring the server's retry-after header
        _groq_client = groq.Groq(api_key=settings.GROQ_API_KEY, max_retries=_MAX_ATTEMPTS)

    kwargs = {}
    if settings.GROQ_MODEL.startswith("openai/gpt-oss"):
        # reasoning model: its hidden reasoning counts against max tokens;
        # retrieval-grounded answers don't need much of it
        kwargs["reasoning_effort"] = "low"

    try:
        response = _groq_client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=messages,
            temperature=temperature,
            max_completion_tokens=max_tokens,
            **kwargs,
        )
    except groq.APIError as exc:
        raise LLMError(f"Groq request failed: {exc}") from exc

    choice = response.choices[0]
    if choice.finish_reason == "length":
        logger.warning(f"Answer truncated at max_tokens={max_tokens} (usage: {response.usage.total_tokens} tokens)")
    return (choice.message.content or "").strip()


# ── Gemini ────────────────────────────────────────────────────────────────────

def _complete_gemini(messages: List[dict], temperature: float, max_tokens: int) -> str:
    import google.generativeai as genai
    from google.api_core.exceptions import GoogleAPIError, ResourceExhausted

    if not settings.GEMINI_API_KEY:
        raise LLMError("LLM_PROVIDER is 'gemini' but GEMINI_API_KEY is not set in backend/.env")
    genai.configure(api_key=settings.GEMINI_API_KEY)

    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    contents = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in messages if m["role"] != "system"
    ]
    model = genai.GenerativeModel(
        model_name=settings.LLM_MODEL,
        system_instruction=system or None,
        generation_config=genai.types.GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,   # gemini-2.5 thinking tokens count against this too
            candidate_count=1,
        ),
    )

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = model.generate_content(contents)
            break
        except ResourceExhausted as exc:
            # a per-DAY quota won't reset within any reasonable wait
            if attempt == _MAX_ATTEMPTS or "PerDay" in str(exc):
                raise LLMError(f"Gemini quota exhausted: {exc}") from exc
            match = _RETRY_DELAY_RE.search(str(exc))
            delay = min(float(match.group(1)) + 1 if match else 15.0 * attempt, 65.0)
            logger.warning(f"Gemini rate-limited (attempt {attempt}/{_MAX_ATTEMPTS}) — retrying in {delay:.0f}s")
            time.sleep(delay)
        except GoogleAPIError as exc:
            raise LLMError(f"Gemini request failed: {exc}") from exc

    if response.candidates and response.candidates[0].finish_reason.name == "MAX_TOKENS":
        logger.warning(
            f"Answer truncated at max_tokens={max_tokens} "
            f"(usage: {response.usage_metadata.total_token_count} tokens incl. thinking)"
        )
    return response.text.strip()
