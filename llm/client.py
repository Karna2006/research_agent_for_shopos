"""LLM clients — OpenRouter Kimi K2 primary, Groq fallback, Gemini third-tier, MiniMax vision.

Text tier 1: moonshotai/kimi-k2 (OpenRouter) — 131K ctx, agentic, strong JSON
Text tier 2: llama-3.3-70b-versatile (Groq) — on Kimi K2 rate limit / OpenRouter error
Text tier 3: llama-3.1-8b-instant (Groq) — on 70B rate limit
Text tier 4: gemini-2.0-flash (Google) — on all Groq tiers rate-limited
Vision: Llama 4 Scout (Groq) primary, MiniMax-VL-01 fallback on error/missing key
"""
import asyncio
import json
import logging
import os
import re
import time
from typing import Any

_logger = logging.getLogger(__name__)

import httpx
from groq import AsyncGroq, RateLimitError, APIStatusError

# ── OpenRouter (Kimi K2 primary) ──────────────────────────────────────────────
_OPENROUTER_BASE_URL  = "https://openrouter.ai/api/v1"
_OPENROUTER_MODEL     = "moonshotai/kimi-k2"
_using_openrouter_err = False   # flips True when OpenRouter returns error; retries Groq
_openrouter_err_since: float = 0.0
_OPENROUTER_RESET_SECS = 120.0  # retry OpenRouter after this many seconds

async def _openrouter_analyze(system_prompt: str, user_content: str, max_tokens: int = 2000) -> str:
    """Call Kimi K2 via OpenRouter OpenAI-compatible endpoint."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        return ""
    payload = {
        "model": _OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{_OPENROUTER_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type":  "application/json",
            },
            json=payload,
        )
        if resp.status_code == 429:
            raise RuntimeError("OpenRouter rate limit")
        if resp.status_code != 200:
            raise RuntimeError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()["choices"][0]["message"]["content"] or ""

# ── Groq (fallback tier 2 + 3) ────────────────────────────────────────────────
MODEL          = "llama-3.3-70b-versatile"
FALLBACK_MODEL = "llama-3.1-8b-instant"
_using_fallback = False  # flips to True once 70B is rate-limited this session
_fallback_since: float = 0.0  # timestamp when fallback mode started
_FALLBACK_RESET_SECS = 90.0   # retry 70B after this many seconds

# Gemini fourth-tier: activated when all Groq tiers are rate-limited
_using_gemini = False
_gemini_since: float = 0.0
_GEMINI_RESET_SECS = 120.0  # retry Groq after this many seconds

_RETRY_ATTEMPTS   = 3
_RETRY_BASE_DELAY = 10.0

# Cap concurrent LLM calls — 2 concurrent with 3s spacing = 20 req/min max, under Groq free-tier 30/min.
_LLM_SEMAPHORE = asyncio.Semaphore(2)

# Enforce minimum spacing between successive Groq calls to stay under RPM limits
_LAST_CALL_TS: float = 0.0
_MIN_CALL_SPACING = 3.0  # seconds — 2 concurrent × 3s = 20 req/min, leaves headroom for retries

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_MODEL    = "gemini-2.0-flash"


async def _gemini_analyze(system_prompt: str, user_content: str, max_tokens: int = 2000) -> str:
    """Call Gemini 2.0 Flash via OpenAI-compatible endpoint."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return ""
    payload = {
        "model": _GEMINI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_content},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{_GEMINI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code == 429:
            raise RuntimeError("Gemini rate limit")
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"] or ""

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
        global _using_fallback, _fallback_since
        if _using_fallback and (time.monotonic() - _fallback_since) > _FALLBACK_RESET_SECS:
            _using_fallback = False
            _logger.info("rate limit window passed — retrying %s", MODEL)
        return FALLBACK_MODEL if _using_fallback else MODEL

    async def analyze(
        self,
        system_prompt: str,
        user_content: str,
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        global _using_fallback, _using_gemini, _gemini_since
        global _using_openrouter_err, _openrouter_err_since
        user_content = self._truncate_if_needed(user_content)

        # Tier 1: OpenRouter Kimi K2 — primary when key is set and not in error window
        or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if or_key:
            if _using_openrouter_err and (time.monotonic() - _openrouter_err_since) > _OPENROUTER_RESET_SECS:
                _using_openrouter_err = False
                _logger.info("openrouter error window over — retrying Kimi K2")
            if not _using_openrouter_err:
                try:
                    result = await _openrouter_analyze(system_prompt, user_content, max_tokens)
                    if result.strip():
                        return result
                    _logger.warning("openrouter empty response — falling back to Groq")
                except Exception as exc:
                    _logger.warning("openrouter error (%s: %s) — falling back to Groq", type(exc).__name__, exc)
                    _using_openrouter_err = True
                    _openrouter_err_since = time.monotonic()

        # Tier 4: Gemini active — try it first, fall back to Groq if it clears
        if _using_gemini:
            if (time.monotonic() - _gemini_since) > _GEMINI_RESET_SECS:
                _using_gemini = False
                _logger.info("Gemini window over — retrying Groq")
            else:
                try:
                    return await _gemini_analyze(system_prompt, user_content, max_tokens)
                except Exception as exc:
                    _logger.warning("gemini error (%s) — falling back to Groq", type(exc).__name__)
                    _using_gemini = False

        for attempt in range(_RETRY_ATTEMPTS):
            model = self._active_model()
            try:
                async with _LLM_SEMAPHORE:
                    global _LAST_CALL_TS
                    now = time.monotonic()
                    gap = _MIN_CALL_SPACING - (now - _LAST_CALL_TS)
                    if gap > 0:
                        await asyncio.sleep(gap)
                    _LAST_CALL_TS = time.monotonic()
                    resp = await self._client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user",   "content": user_content},
                        ],
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                content = resp.choices[0].message.content or ""
                if not content.strip():
                    # Empty body despite HTTP 200 — Groq silently throttling under quota pressure
                    if attempt < _RETRY_ATTEMPTS - 1:
                        wait = _RETRY_BASE_DELAY * (2 ** attempt)
                        _logger.warning("groq empty response (attempt %d/%d) — waiting %.0fs", attempt+1, _RETRY_ATTEMPTS, wait)
                        await asyncio.sleep(wait)
                        continue
                    return ""
                return content
            except RateLimitError:
                if not _using_fallback:
                    _logger.warning("groq %s rate limit — switching to fallback (%s)", MODEL, FALLBACK_MODEL)
                    _using_fallback = True
                    _fallback_since = time.monotonic()
                    continue  # retry immediately with fallback model
                # Both Groq tiers rate-limited — escalate to Gemini
                if not _using_gemini and os.environ.get("GEMINI_API_KEY"):
                    _logger.warning("groq both tiers rate-limited — switching to Gemini 2.0 Flash")
                    _using_gemini = True
                    _gemini_since = time.monotonic()
                    try:
                        return await _gemini_analyze(system_prompt, user_content, max_tokens)
                    except Exception as gem_exc:
                        _logger.warning("gemini error (%s) — waiting for Groq", type(gem_exc).__name__)
                        _using_gemini = False
                if attempt < _RETRY_ATTEMPTS - 1:
                    delay = _RETRY_BASE_DELAY * (2 ** attempt)
                    _logger.warning("groq fallback rate limit — waiting %.0fs before retry %d/%d", delay, attempt+1, _RETRY_ATTEMPTS)
                    await asyncio.sleep(delay)
                else:
                    raise
            except APIStatusError as exc:
                if exc.status_code in (503, 529) and attempt < _RETRY_ATTEMPTS - 1:
                    delay = _RETRY_BASE_DELAY * (2 ** attempt)
                    _logger.warning("groq server %s, retrying in %.0fs", exc.status_code, delay)
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

        # Outer retry: if analyze() returns empty (rate-limit silent fail), wait and try again
        raw = ""
        for _outer in range(3):
            raw = await self.analyze(full_system, user_content, max_tokens=max_tokens)
            if raw.strip():
                break
            if _outer < 2:
                wait = 25 * (2 ** _outer)  # 25s, 50s
                _logger.warning("analyze_structured got empty response (%d/3) — waiting %ds", _outer+1, wait)
                await asyncio.sleep(wait)

        if not raw.strip():
            return {"_raw": "", "_parse_error": "LLM returned empty after 3 outer attempts — quota exhausted"}

        for attempt_parse in range(2):
            candidate = _extract_json(raw)
            try:
                parsed = json.loads(candidate)
                # Unwrap list-wrapped responses (some LLMs return [{...}] instead of {...})
                if isinstance(parsed, list):
                    parsed = parsed[0] if parsed else {}
                return parsed if isinstance(parsed, dict) else {"_value": parsed}
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

    async def analyze_image(
        self,
        system_prompt: str,
        image_url: str,
        text_prompt: str,
        max_tokens: int = 1000,
    ) -> str:
        """Single image + text. MiniMax via NVIDIA NIM is primary; Llama 4 Scout is fallback."""
        mm = get_minimax_client()
        if mm:
            try:
                return await mm.analyze_image(system_prompt, image_url, text_prompt, max_tokens)
            except Exception as exc:
                _logger.warning("minimax-vision error (%s) — falling back to Llama 4 Scout", type(exc).__name__)

        VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
        async with _LLM_SEMAPHORE:
            resp = await self._client.chat.completions.create(
                model=VISION_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image_url}},
                            {"type": "text", "text": text_prompt},
                        ],
                    },
                ],
                max_tokens=max_tokens,
                temperature=0.3,
            )
        return resp.choices[0].message.content or ""

    async def analyze_image_batch(
        self,
        system_prompt: str,
        images_b64: list[str],
        text_prompt: str,
        max_tokens: int = 1000,
    ) -> str:
        """Multiple base64 images. MiniMax via NVIDIA NIM is primary; Llama 4 Scout is fallback."""
        mm = get_minimax_client()
        if mm:
            try:
                return await mm.analyze_image_batch(system_prompt, images_b64, text_prompt, max_tokens)
            except Exception as exc:
                _logger.warning("minimax-vision-batch error (%s) — falling back to Llama 4 Scout", type(exc).__name__)

        VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
        content: list[dict] = []
        for b64 in images_b64[:6]:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        content.append({"type": "text", "text": text_prompt})
        async with _LLM_SEMAPHORE:
            resp = await self._client.chat.completions.create(
                model=VISION_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
                max_tokens=max_tokens,
                temperature=0.3,
            )
        return resp.choices[0].message.content or ""

    def token_usage_this_minute(self) -> int:
        return 0


# ── NVIDIA NIM vision client (free MiniMax M2.7 + 40 other models) ─────────────
# Get a free API key at build.nvidia.com → Profile → API Keys
# Free tier: 41 models including MiniMax, DeepSeek, Kimi K2 — unlimited during promo

_NVIDIA_BASE_URL   = "https://integrate.api.nvidia.com/v1"
_NVIDIA_VL_MODEL   = "minimax/minimax-vl-01"   # MiniMax vision on NVIDIA NIM


class MiniMaxClient:
    """Vision client backed by NVIDIA NIM (free MiniMax M2.7 via build.nvidia.com).

    OpenAI-compatible endpoint — same message format as Groq, different base URL + key.
    Rename/swap the model string to use any of the 41 free NVIDIA NIM models.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    async def _chat(self, messages: list[dict], max_tokens: int = 1000) -> str:
        payload = {
            "model": _NVIDIA_VL_MODEL,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{_NVIDIA_BASE_URL}/chat/completions",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
        return data["choices"][0]["message"]["content"] or ""

    async def analyze_image(
        self,
        system_prompt: str,
        image_url: str,
        text_prompt: str,
        max_tokens: int = 1000,
    ) -> str:
        """Single image + text — forwarded to MiniMax VL via NVIDIA NIM."""
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": text_prompt},
                ],
            },
        ]
        return await self._chat(messages, max_tokens)

    async def analyze_image_batch(
        self,
        system_prompt: str,
        images_b64: list[str],
        text_prompt: str,
        max_tokens: int = 1000,
    ) -> str:
        """Multiple base64 images in one call — forwarded to MiniMax VL via NVIDIA NIM."""
        content: list[dict] = []
        for b64 in images_b64[:6]:
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        content.append({"type": "text", "text": text_prompt})
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ]
        return await self._chat(messages, max_tokens)


# ── Singletons ─────────────────────────────────────────────────────────────────

_default_client: GroqClient | None = None
_minimax_client: MiniMaxClient | None = None


def get_client() -> GroqClient:
    global _default_client
    if _default_client is None:
        _default_client = GroqClient()
    return _default_client


def get_minimax_client() -> MiniMaxClient | None:
    """Returns MiniMaxClient (NVIDIA NIM backend) if NVIDIA_API_KEY is set, else None."""
    global _minimax_client
    if _minimax_client is None:
        key = os.environ.get("NVIDIA_API_KEY", "").strip()
        if key:
            _minimax_client = MiniMaxClient(key)
    return _minimax_client
