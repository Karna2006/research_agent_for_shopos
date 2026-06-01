"""Groq API client — Llama 3.1 70B, with retry, JSON parsing, and token counting."""
import asyncio
import json
import os
import re
import time
from typing import Any

from groq import AsyncGroq, RateLimitError, APIStatusError

MODEL = "llama-3.3-70b-versatile"

# Groq free tier: ~14,400 tokens/min for 70B.
# We track usage and warn; we never hard-block (the API will 429 before we do).
_TOKENS_USED: int = 0
_WINDOW_START: float = time.monotonic()
_TOKEN_WARN_THRESHOLD = 12_000  # warn before hitting the ceiling

_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 10.0  # seconds for rate-limit retries: 10s, 20s, 40s

# JSON fence pattern — strips ```json ... ``` wrappers the model sometimes adds
_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _track_tokens(prompt_tokens: int, completion_tokens: int) -> None:
    global _TOKENS_USED, _WINDOW_START
    now = time.monotonic()
    if now - _WINDOW_START > 60:
        _TOKENS_USED = 0
        _WINDOW_START = now
    _TOKENS_USED += prompt_tokens + completion_tokens
    if _TOKENS_USED > _TOKEN_WARN_THRESHOLD:
        print(
            f"[groq] ⚠  ~{_TOKENS_USED} tokens used this minute "
            f"(free limit ~14 400). Slowdowns likely."
        )


def _extract_json(text: str) -> str:
    """Strip markdown fences and pull out the first complete JSON object/array."""
    # Remove fences first
    fence_match = _JSON_FENCE.search(text)
    if fence_match:
        return fence_match.group(1).strip()
    # Find first { or [ and match its closing brace
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = text.find(start_char)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text[start:], start):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == start_char:
                depth += 1
            elif ch == end_char:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return text.strip()


class GroqClient:
    def __init__(self) -> None:
        self._client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])

    @staticmethod
    def _truncate_if_needed(content: str, max_chars: int = 24_000) -> str:
        """Truncate content that would exceed ~6000 tokens (estimate: chars/4)."""
        if len(content) <= max_chars:
            return content
        cutoff = max_chars - 50
        return content[:cutoff] + "\n\n[content truncated for analysis]"

    async def analyze(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        """Send a chat request; return the raw string response."""
        user_content = self._truncate_if_needed(user_content)
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                response = await self._client.chat.completions.create(
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                usage = response.usage
                if usage:
                    _track_tokens(usage.prompt_tokens, usage.completion_tokens)
                return response.choices[0].message.content or ""

            except RateLimitError:
                if attempt == _RETRY_ATTEMPTS - 1:
                    raise
                delay = _RETRY_BASE_DELAY * (2 ** attempt)  # 10s, 20s, 40s
                print(f"[groq] rate limit hit — waiting {delay:.0f}s before retry {attempt + 1}/{_RETRY_ATTEMPTS}")
                await asyncio.sleep(delay)

            except APIStatusError as exc:
                # 503 / 529 = model overloaded — same retry strategy
                if exc.status_code in (503, 529) and attempt < _RETRY_ATTEMPTS - 1:
                    delay = _RETRY_BASE_DELAY * (2 ** attempt)
                    print(f"[groq] server {exc.status_code}, retrying in {delay:.0f}s")
                    await asyncio.sleep(delay)
                else:
                    raise

        return ""

    async def analyze_structured(
        self,
        system_prompt: str,
        user_content: str,
        output_schema: dict[str, Any] | None = None,
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        """Like analyze(), but parse and return a JSON dict.

        Falls back gracefully: if JSON parsing fails after retries,
        returns {"_raw": <text>, "_parse_error": <reason>} so the
        pipeline never crashes.
        """
        # Strengthen the JSON-only instruction
        json_instruction = (
            "\n\nIMPORTANT: Your entire response must be a single valid JSON object. "
            "No markdown, no explanation, no code fences — raw JSON only."
        )
        if output_schema:
            schema_str = json.dumps(output_schema, indent=2)
            json_instruction += f"\n\nRequired schema:\n{schema_str}"

        full_system = system_prompt + json_instruction

        raw = await self.analyze(full_system, user_content, max_tokens=max_tokens)

        # Try to parse; retry once with a stricter nudge if needed
        for attempt_parse in range(2):
            candidate = _extract_json(raw)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                if attempt_parse == 0:
                    # Ask the model to fix its own output
                    raw = await self.analyze(
                        "You are a JSON fixer. The user will give you malformed JSON. "
                        "Return only the corrected, valid JSON — nothing else.",
                        f"Fix this JSON:\n{raw}",
                        max_tokens=max_tokens,
                    )
                else:
                    return {"_raw": raw, "_parse_error": "Could not parse JSON after repair attempt"}

        return {"_raw": raw, "_parse_error": "Unreachable"}

    def token_usage_this_minute(self) -> int:
        """Return estimated tokens consumed in the current 60-second window."""
        return _TOKENS_USED


# Module-level singleton — import and use directly
_default_client: GroqClient | None = None


def get_client() -> GroqClient:
    global _default_client
    if _default_client is None:
        _default_client = GroqClient()
    return _default_client
