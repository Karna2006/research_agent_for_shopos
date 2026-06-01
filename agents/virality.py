"""Virality Predictor — scores a product's organic spread potential across social platforms."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm.client import GroqClient
    from scrapers.web_scraper import WebScraper
    from scrapers.search import SearchAgent as SearchAgentT

# ── Prompt ─────────────────────────────────────────────────────────────────────

VIRALITY_PROMPT = """\
You are a viral content strategist and consumer psychologist \
who has studied thousands of viral ecommerce products on TikTok and Instagram.

Score this product on 7 virality dimensions, each 0-10. \
Be brutally honest — most products score 3-6, only exceptional ones hit 9+.

SCORING RUBRIC (mandatory — follow exactly):
10: This dimension alone could make content go viral
7-9: Strong signal, meaningfully above average
4-6: Present but not differentiating
1-3: Weak or absent
0: Completely missing

COMPOSITE SCORE RUBRIC — overall_virality_score (0-100):
85-100: Market leader — viral machine
70-84: Strong potential, likely to spread
55-69: Moderate potential, needs the right creator
40-54: Weak signals, unlikely without paid boost
0-39: Unlikely to spread organically

Dimension weights (for overall_virality_score = weighted_sum × 10):
  emotional_trigger: 20% · visual_stopping_power: 18% · transformation_clarity: 17%
  social_currency: 15% · trend_alignment: 12% · share_trigger: 10% · hook_strength: 8%

Grade mapping (apply AFTER computing overall_virality_score):
  85-100 → S (Viral Machine)
  70-84  → A (Strong Potential)
  55-69  → B (Moderate Potential)
  40-54  → C (Weak Signals)
  0-39   → D (Unlikely to Spread)

Output ONLY valid JSON with exactly this structure:
{
  "overall_virality_score": 0,
  "grade": "S (Viral Machine) | A (Strong Potential) | B (Moderate Potential) | C (Weak Signals) | D (Unlikely to Spread)",
  "dimensions": {
    "emotional_trigger": {
      "score": 0,
      "reasoning": "what emotion does this product tap — surprise, aspiration, identity, transformation, humor?",
      "signals": ["specific phrase or feature that triggers emotion"]
    },
    "visual_stopping_power": {
      "score": 0,
      "reasoning": "does this product LOOK interesting in a 2-second scroll?",
      "signals": []
    },
    "transformation_clarity": {
      "score": 0,
      "reasoning": "how clear is the before/after — does it show obvious life improvement?",
      "signals": []
    },
    "social_currency": {
      "score": 0,
      "reasoning": "do people WANT to be seen using this — does it signal identity or taste?",
      "signals": []
    },
    "trend_alignment": {
      "score": 0,
      "reasoning": "does this fit any current trends on TikTok/Instagram/Pinterest?",
      "signals": []
    },
    "share_trigger": {
      "score": 0,
      "reasoning": "why would someone tag a friend — is it a gift? a surprise? a 'you need this'?",
      "signals": []
    },
    "hook_strength": {
      "score": 0,
      "reasoning": "the opening claim — does it stop the scroll or just describe a product?",
      "signals": []
    }
  },
  "viral_content_angles": [
    {
      "angle": "specific content idea that could go viral for this product",
      "expected_reach_multiplier": "string — e.g. '3-5x organic reach vs paid', '2x shares vs typical post'",
      "best_platform": "TikTok | Instagram Reels | YouTube Shorts | Pinterest",
      "hook_line": "the opening line or visual hook for this angle"
    }
  ],
  "ideal_creator_profile": "describe the ideal UGC creator or influencer type for this product",
  "best_platforms": ["TikTok", "Instagram Reels", "Pinterest", "YouTube Shorts"],
  "killer_hook": "THE hook — the one opening line for a video or ad that could stop a scroll",
  "risk_factors": ["what could prevent this from going viral"],
  "comparable_viral_products": ["name products in this category that went viral and why this is similar/different"]
}"""

# ── Grade thresholds ───────────────────────────────────────────────────────────

def _grade(score: int | float) -> str:
    if score >= 85:
        return "S (Viral Machine)"
    if score >= 70:
        return "A (Strong Potential)"
    if score >= 55:
        return "B (Moderate Potential)"
    if score >= 40:
        return "C (Weak Signals)"
    return "D (Unlikely to Spread)"


# Dimension weights — must sum to 1.0 (per spec)
_WEIGHTS = {
    "emotional_trigger":      0.20,
    "visual_stopping_power":  0.18,
    "transformation_clarity": 0.17,
    "social_currency":        0.15,
    "trend_alignment":        0.12,
    "share_trigger":          0.10,
    "hook_strength":          0.08,
}


def _weighted_score(dimensions: dict) -> int:
    """Recalculate overall score from dimension scores using defined weights."""
    total = 0.0
    for dim, weight in _WEIGHTS.items():
        raw = dimensions.get(dim, {})
        score = raw.get("score", 0) if isinstance(raw, dict) else 0
        total += score * weight
    # Each dimension is 0-10; multiply by 10 to get 0-100
    return round(total * 10)


def _fmt_search(results: list[dict], max_chars: int = 800) -> str:
    lines = [f"- {r.get('title', '')}: {r.get('snippet', '')}" for r in results]
    return "\n".join(lines)[:max_chars]


# ── Agent class ────────────────────────────────────────────────────────────────

class ViralityPredictor:
    def __init__(
        self,
        llm_client: "GroqClient",
        scraper: "WebScraper",
        search_agent: "SearchAgentT",
    ) -> None:
        self.llm = llm_client
        self.scraper = scraper
        self.search = search_agent

    async def predict(
        self,
        url: str | None = None,
        product_name: str | None = None,
        description: str | None = None,
        category: str | None = None,
    ) -> dict:
        """Score a product's virality potential.

        Any combination of inputs is valid — richer input = better score.
        The agent supplements whatever it receives with scraped + search data.
        """
        out: dict = {
            "agent": "virality",
            "url": url,
            "product_name": product_name,
        }

        try:
            # ── Step 1: Data gathering ─────────────────────────────────────
            scraped: dict = {}
            pdp_result = None
            if url:
                try:
                    pdp_result = await self.scraper.scrape_pdp(url)
                    scraped = pdp_result.value or {}
                    # Fall back to generic page scrape if PDP extraction failed
                    if not scraped.get("product_name") and pdp_result.ok:
                        page_result = await self.scraper.scrape_page(url)
                        page = page_result.value or {}
                        scraped.update({
                            "page_title": page.get("title", ""),
                            "page_body":  page.get("body_text", "")[:2000],
                            "headings":   page.get("headings", [])[:10],
                            "schema_ld":  page.get("schema_json_ld", []),
                        })
                except Exception as exc:
                    scraped["scrape_error"] = str(exc)

            # Merge: explicit args override scraped values
            resolved_name  = product_name or scraped.get("product_name") or "Unknown product"
            resolved_desc  = description  or scraped.get("description")  or ""
            resolved_price = scraped.get("price", "")
            resolved_rating      = scraped.get("rating", "")
            resolved_review_count = scraped.get("reviews_count", "")
            resolved_in_stock    = scraped.get("in_stock", None)
            resolved_images      = len(scraped.get("images", []))
            resolved_cta         = scraped.get("cta_text", "")

            # Category fallback — infer from name + description
            if not category:
                combined = f"{resolved_name} {resolved_desc}".lower()
                for kw in [
                    "clothing", "fashion", "shirt", "dress", "shoes", "bag",
                    "skincare", "beauty", "makeup", "fragrance", "gadget",
                    "electronics", "furniture", "food", "fitness", "jewel",
                ]:
                    if kw in combined:
                        category = kw
                        break
                category = category or "ecommerce"

            # ── Step 2: Social signal searches ────────────────────────────
            tiktok_results = self.search.search(
                f"{resolved_name} tiktok viral trend", max_results=4
            )
            insta_results = self.search.search(
                f"{resolved_name} instagram trending review", max_results=4
            )

            # Extract key claims from description (sentences ending with !
            # or containing superlatives)
            import re
            claim_pattern = re.compile(
                r"[^.!?]*(?:best|only|first|unique|revolutionary|award|#1|must-have|"
                r"sold out|viral|limited|exclusive)[^.!?]*[.!?]",
                re.IGNORECASE,
            )
            key_claims = claim_pattern.findall(resolved_desc)[:5]

            # ── Step 3: Build product_data block ──────────────────────────
            product_data = {
                "product_name":   resolved_name,
                "category":       category,
                "price":          resolved_price,
                "description":    resolved_desc[:1500],
                "key_claims":     key_claims,
                "rating":         resolved_rating,
                "review_count":   resolved_review_count,
                "in_stock":       resolved_in_stock,
                "image_count":    resolved_images,
                "cta_text":       resolved_cta,
                "url":            url or "not provided",
                "tiktok_signals": _fmt_search(tiktok_results),
                "instagram_signals": _fmt_search(insta_results),
            }

            user_content = (
                f"Product data:\n{json.dumps(product_data, indent=2, ensure_ascii=False)}"
            )

            # ── Step 4: LLM scoring ───────────────────────────────────────
            raw = await self.llm.analyze_structured(
                system_prompt=VIRALITY_PROMPT,
                user_content=user_content,
                max_tokens=2000,
            )

            # ── Step 5: Post-process ──────────────────────────────────────
            if "_parse_error" not in raw:
                # Recalculate overall score from dimensions to prevent LLM drift
                dims = raw.get("dimensions", {})
                if dims:
                    recalculated = _weighted_score(dims)
                    # Accept LLM score if within ±10 of recalculation, else override
                    llm_score = raw.get("overall_virality_score", recalculated)
                    if abs(llm_score - recalculated) > 10:
                        raw["overall_virality_score"] = recalculated
                        raw["_score_overridden"] = True

                    # Always set grade from the final score
                    raw["grade"] = _grade(raw["overall_virality_score"])

            out["score"] = raw.get("overall_virality_score")
            out["grade"] = raw.get("grade")
            out["analysis"] = raw
            out["product_data_used"] = {
                "name":     resolved_name,
                "category": category,
                "price":    resolved_price,
                "scraped":  bool(url and pdp_result is not None and pdp_result.ok),
            }

            # ── Step 6: Virality trajectory (Chronos → Prophet → numpy) ──────
            try:
                from agents.trend_predictor import get_predictor
                out["virality_trajectory"] = get_predictor().predict_virality_trajectory({
                    "review_count":       resolved_review_count or 0,
                    "rating":             resolved_rating or 4.0,
                    "description_length": len(resolved_desc),
                    "has_images":         resolved_images > 3,
                    "category":           category,
                    "virality_score":     out["score"] or 50,
                })
            except Exception as tex:
                out["virality_trajectory"] = {"error": str(tex)}

        except Exception as exc:
            out["error"] = str(exc)

        return out


# ── Module-level backward-compat shim (kept for main.py BackgroundTask) ────────

async def run(url: str, product_name: str, description: str) -> dict:
    """Thin wrapper so main.py can call run() without instantiating directly."""
    from llm.client import get_client
    from scrapers.web_scraper import WebScraper
    from scrapers.search import SearchAgent

    agent = ViralityPredictor(get_client(), WebScraper(), SearchAgent())
    return await agent.predict(url=url, product_name=product_name, description=description)
