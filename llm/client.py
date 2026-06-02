"""Groq LLM client — llama-3.3-70b-versatile primary, 8b-instant fallback.

Primary:  llama-3.3-70b-versatile — best quality, 100k tokens/day free
Fallback: llama-3.1-8b-instant    — activates on 429/rate-limit, 500k tokens/day
"""
import asyncio
import json
import os
import re
import time
from typing import Any

from groq import AsyncGroq, RateLimitError, APIStatusError

MODEL          = "llama-3.3-70b-versatile"
FALLBACK_MODEL = "llama-3.1-8b-instant"
_using_fallback = False  # flips to True once 70B is rate-limited this session

_RETRY_ATTEMPTS   = 3
_RETRY_BASE_DELAY = 10.0

# Cap concurrent LLM calls to avoid exhausting Groq free-tier TPM in burst scenarios
_LLM_SEMAPHORE = asyncio.Semaphore(3)

_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

_TOKENS_USED: int = 0
_MINUTE_START: float = time.monotonic()


def _extract_json(text: str) -> str:
    fence_match = _JSON_FENCE.search(text)
    if fence_match:
        return fence_match.group(1).strip()
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
                    return text[start: i + 1]
    return text.strip()


class GroqClient:
    def __init__(self) -> None:
        self._client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])

    @staticmethod
    def _truncate_if_needed(content: str, max_chars: int = 20_000) -> str:
        if len(content) <= max_chars:
            return content
        return content[:max_chars - 50] + "\n\n[content truncated for analysis]"

    def _active_model(self) -> str:
        return FALLBACK_MODEL if _using_fallback else MODEL

    async def analyze(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        global _using_fallback
        user_content = self._truncate_if_needed(user_content)
        for attempt in range(_RETRY_ATTEMPTS):
            model = self._active_model()
            try:
                async with _LLM_SEMAPHORE:
                    resp = await self._client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user",   "content": user_content},
                        ],
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                return resp.choices[0].message.content or ""
            except RateLimitError:
                if not _using_fallback:
                    print(f"[groq] 70B rate limit hit — switching to fallback ({FALLBACK_MODEL})", flush=True)
                    _using_fallback = True
                    continue  # retry immediately with fallback model
                if attempt < _RETRY_ATTEMPTS - 1:
                    delay = _RETRY_BASE_DELAY * (2 ** attempt)
                    print(f"[groq] fallback rate limit — waiting {delay:.0f}s before retry {attempt + 1}/{_RETRY_ATTEMPTS}", flush=True)
                    await asyncio.sleep(delay)
                else:
                    raise
            except APIStatusError as exc:
                if exc.status_code in (503, 529) and attempt < _RETRY_ATTEMPTS - 1:
                    delay = _RETRY_BASE_DELAY * (2 ** attempt)
                    print(f"[groq] server {exc.status_code}, retrying in {delay:.0f}s", flush=True)
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
        json_instruction = (
            "\n\nIMPORTANT: Your entire response must be a single valid JSON object. "
            "No markdown, no explanation, no code fences — raw JSON only."
        )
        if output_schema:
            json_instruction += f"\n\nRequired schema:\n{json.dumps(output_schema, indent=2)}"

        full_system = system_prompt + json_instruction
        raw = await self.analyze(full_system, user_content, max_tokens=max_tokens)

        for attempt_parse in range(2):
            candidate = _extract_json(raw)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                if attempt_parse == 0:
                    raw = await self.analyze(
                        "You are a JSON fixer. Return only the corrected valid JSON — nothing else.",
                        f"Fix this JSON:\n{raw}",
                        max_tokens=max_tokens,
                    )
                else:
                    return {"_raw": raw, "_parse_error": "Could not parse JSON after repair attempt"}

        return {"_raw": raw, "_parse_error": "Unreachable"}

    def token_usage_this_minute(self) -> int:
        return 0


_default_client: GroqClient | None = None


def get_client() -> GroqClient:
    global _default_client
    if _default_client is None:
        _default_client = GroqClient()
    return _default_client
