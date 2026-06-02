"""Virality Predictor — URL-aware, signal-driven virality scoring.

Flow when URL is provided:
  1. Scrape PDP → images, description, rating, price
  2. Download up to 3 images → PIL analysis (color, size, lifestyle detection)
  3. Extract text signals (benefit ratio, review count, etc.) — no LLM
  4. Feed full signal map to LLM as primary evidence → scored dimensions
  5. Each dimension includes an "evidence" field citing actual scraped data

Flow when URL is missing or fails:
  ⚠ Text-only mode — description scored as-is, no image signals.
"""
from __future__ import annotations

import asyncio
import colorsys
import io
import json
import re
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm.client import GroqClient
    from scrapers.web_scraper import WebScraper
    from scrapers.search import SearchAgent as SearchAgentT

# ── Keyword patterns ────────────────────────────────────────────────────────────

_LIFESTYLE_RE = re.compile(
    r"lifestyle|model|worn|wearing|person|people|girl|boy|woman|man|human|use|using|action",
    re.IGNORECASE,
)
_BEFORE_AFTER_RE = re.compile(
    r"before|after|result|transformation|comparison|compare|progress",
    re.IGNORECASE,
)
_VIDEO_RE = re.compile(r"<video|youtube\.com|youtu\.be|vimeo\.com", re.IGNORECASE)

# ── Benefit vs feature word lists ───────────────────────────────────────────────

_BENEFIT_WORDS = frozenset({
    "feel", "look", "transform", "glow", "wake", "energy", "confident",
    "beautiful", "powerful", "amazing", "love", "perfect", "best",
    "incredible", "effortless", "radiant", "revitalize", "boost", "smooth",
    "natural", "instant", "visible", "results", "difference", "change",
    "better", "improve", "enhance", "flawless", "fresh", "vibrant",
    "hydrate", "nourish", "rejuvenate", "soften", "calm", "soothe",
    "dream", "luxury", "premium", "exclusive", "unique", "rare",
})

_FEATURE_WORDS = frozenset({
    "weight", "dimensions", "material", "cotton", "polyester", "nylon",
    "specification", "contains", "ingredients", "formula", "compound",
    "percentage", "concentration", "manufacture", "fabric", "thread",
    "count", "gsm", "ply", "grade", "certified", "composition",
    "blend", "weave", "gauge", "denier", "dpf", "micron",
})

# ── Prompt ──────────────────────────────────────────────────────────────────────

VIRALITY_PROMPT = """\
You are a viral content strategist and consumer psychologist \
who has studied thousands of viral ecommerce products on TikTok and Instagram.

Score this product on 7 virality dimensions, each 0-10. \
Be brutally honest — most products score 3-6, only exceptional ones hit 9+.

You are given OBJECTIVE SCRAPED SIGNALS. Use these as PRIMARY EVIDENCE. \
Do not guess or assume — base every score on the actual data provided.

SCORING RUBRIC (mandatory):
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

Dimension weights (overall_virality_score = weighted_sum × 10):
  emotional_trigger: 20% · visual_stopping_power: 18% · transformation_clarity: 17%
  social_currency: 15% · trend_alignment: 12% · share_trigger: 10% · hook_strength: 8%

Grade mapping:
  85-100 → S (Viral Machine) | 70-84 → A (Strong Potential)
  55-69  → B (Moderate Potential) | 40-54 → C (Weak Signals) | 0-39 → D (Unlikely to Spread)

For EVERY dimension include an "evidence" field: \
one concise sentence that cites the specific scraped signals that drove the score. \
Example: "8 product images found · lifestyle shot detected · dominant color: coral pink"

Output ONLY valid JSON:
{
  "overall_virality_score": 0,
  "grade": "A (Strong Potential)",
  "dimensions": {
    "emotional_trigger":      {"score":0,"reasoning":"...","signals":[],"evidence":"..."},
    "visual_stopping_power":  {"score":0,"reasoning":"...","signals":[],"evidence":"..."},
    "transformation_clarity": {"score":0,"reasoning":"...","signals":[],"evidence":"..."},
    "social_currency":        {"score":0,"reasoning":"...","signals":[],"evidence":"..."},
    "trend_alignment":        {"score":0,"reasoning":"...","signals":[],"evidence":"..."},
    "share_trigger":          {"score":0,"reasoning":"...","signals":[],"evidence":"..."},
    "hook_strength":          {"score":0,"reasoning":"...","signals":[],"evidence":"..."}
  },
  "viral_content_angles": [
    {
      "angle": "specific content idea",
      "expected_reach_multiplier": "e.g. '3-5x organic reach vs paid'",
      "best_platform": "TikTok | Instagram Reels | YouTube Shorts | Pinterest",
      "hook_line": "the opening line for this angle"
    }
  ],
  "ideal_creator_profile": "describe ideal UGC creator type",
  "best_platforms": ["TikTok","Instagram Reels"],
  "killer_hook": "the one opening line that stops a scroll",
  "risk_factors": ["what could prevent this from going viral"],
  "comparable_viral_products": ["products that went viral and why similar/different"]
}"""

# ── Grade + weight helpers ──────────────────────────────────────────────────────

def _grade(score: int | float) -> str:
    if score >= 85: return "S (Viral Machine)"
    if score >= 70: return "A (Strong Potential)"
    if score >= 55: return "B (Moderate Potential)"
    if score >= 40: return "C (Weak Signals)"
    return "D (Unlikely to Spread)"


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
    total = 0.0
    for dim, weight in _WEIGHTS.items():
        raw = dimensions.get(dim, {})
        score = raw.get("score", 0) if isinstance(raw, dict) else 0
        total += score * weight
    return round(total * 10)


def _fmt_search(results: list[dict], max_chars: int = 800) -> str:
    lines = [f"- {r.get('title', '')}: {r.get('snippet', '')}" for r in results]
    return "\n".join(lines)[:max_chars]


# ── Image download + PIL analysis ───────────────────────────────────────────────

async def _download_image(url: str, timeout: float = 5.0) -> bytes | None:
    """Download image bytes with a short timeout. Returns None on any failure."""
    import httpx
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ShoposBot/1.0)"},
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and len(resp.content) > 1024:
                return resp.content
    except Exception:
        pass
    return None


def _analyse_image_pil(img_bytes: bytes) -> dict:
    """Extract dominant color, white-background flag, and colorfulness via PIL."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        size_kb = len(img_bytes) / 1024

        # Small thumbnail for fast pixel analysis
        thumb = img.resize((50, 50), Image.LANCZOS)
        pixels: list[tuple[int, int, int]] = list(thumb.getdata())  # type: ignore[assignment]

        # Dominant color via 32-step quantisation
        quantized = [(r // 32 * 32, g // 32 * 32, b // 32 * 32) for r, g, b in pixels]
        dom_r, dom_g, dom_b = Counter(quantized).most_common(1)[0][0]

        # Map to human color name via HSV
        h, s, v = colorsys.rgb_to_hsv(dom_r / 255, dom_g / 255, dom_b / 255)
        if v < 0.20:
            color_name = "black"
        elif v > 0.88 and s < 0.12:
            color_name = "white"
        elif s < 0.15:
            color_name = "grey"
        else:
            h_deg = h * 360
            color_name = (
                "red"    if h_deg < 15 or h_deg >= 345 else
                "orange" if h_deg < 35  else
                "yellow" if h_deg < 65  else
                "green"  if h_deg < 150 else
                "cyan"   if h_deg < 200 else
                "blue"   if h_deg < 260 else
                "purple" if h_deg < 300 else
                "pink"
            )

        background_is_white = dom_r > 200 and dom_g > 200 and dom_b > 200

        # Colorfulness: average channel spread across all pixels
        avg_r = sum(p[0] for p in pixels) / len(pixels)
        avg_g = sum(p[1] for p in pixels) / len(pixels)
        avg_b = sum(p[2] for p in pixels) / len(pixels)
        channel_spread = max(avg_r, avg_g, avg_b) - min(avg_r, avg_g, avg_b)
        visual_contrast_score = min(10, round(channel_spread / 25.5))  # 255 → 10

        return {
            "color_name": color_name,
            "background_is_white": background_is_white,
            "size_kb": round(size_kb, 1),
            "visual_contrast_score": visual_contrast_score,
        }
    except Exception:
        return {
            "color_name": "unknown",
            "background_is_white": False,
            "size_kb": 0.0,
            "visual_contrast_score": 5,
        }


async def _extract_visual_signals(image_urls: list[str], scraped: dict) -> dict:
    """Download up to 3 product images and return objective visual signals."""
    urls_to_fetch = [
        u for u in (image_urls or [])
        if u.startswith(("http://", "https://"))
    ][:3]

    # Keyword scan on image src URLs (proxy for alt text — filenames often reveal intent)
    src_text = " ".join(urls_to_fetch).lower()
    desc_text = scraped.get("description", "").lower()
    combined_text = src_text + " " + desc_text

    has_lifestyle_shot = bool(_LIFESTYLE_RE.search(combined_text))
    has_before_after   = bool(_BEFORE_AFTER_RE.search(combined_text))
    has_video          = bool(_VIDEO_RE.search(desc_text))

    # Download images concurrently — capped at 5s each
    image_analyses: list[dict] = []
    if urls_to_fetch:
        tasks = [_download_image(u) for u in urls_to_fetch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for raw in results:
            if isinstance(raw, bytes) and raw:
                image_analyses.append(_analyse_image_pil(raw))

    image_count   = len(image_urls or [])
    images_done   = len(image_analyses)
    avg_size_kb   = (sum(a["size_kb"] for a in image_analyses) / images_done) if images_done else 0.0
    primary_color = image_analyses[0]["color_name"] if image_analyses else "unknown"
    bg_is_white   = image_analyses[0]["background_is_white"] if image_analyses else False
    avg_contrast  = (sum(a["visual_contrast_score"] for a in image_analyses) / images_done) if images_done else 5.0

    # Derived 0-10 scores
    image_richness     = min(10, round(
        image_count * 1.0
        + (2 if has_lifestyle_shot else 0)
        + (2 if has_video else 0)
        + (1 if image_count >= 5 else 0)
    ))
    visual_contrast    = max(0, round(avg_contrast) - (3 if bg_is_white else 0))
    image_quality_px   = min(10, round(avg_size_kb / 50)) if avg_size_kb > 0 else 5

    return {
        "image_count":         image_count,
        "images_analyzed":     images_done,
        "has_lifestyle_shot":  has_lifestyle_shot,
        "has_before_after":    has_before_after,
        "has_video":           has_video,
        "primary_color_name":  primary_color,
        "background_is_white": bg_is_white,
        "avg_image_size_kb":   round(avg_size_kb, 1),
        "image_richness":      image_richness,
        "visual_contrast":     visual_contrast,
        "transformation_evidence": has_before_after,
        "video_present":       has_video,
        "image_quality_proxy": image_quality_px,
    }


# ── Text signal extraction ──────────────────────────────────────────────────────

def _extract_text_signals(
    name: str,
    description: str,
    price: str,
    rating: str,
    reviews_count: str,
) -> dict:
    """Extract objective text signals — pure Python, no LLM."""
    words = re.findall(r"\b\w+\b", f"{name} {description}".lower())
    benefit_n = sum(1 for w in words if w in _BENEFIT_WORDS)
    feature_n = sum(1 for w in words if w in _FEATURE_WORDS)
    total_sig = benefit_n + feature_n
    benefit_ratio = benefit_n / total_sig if total_sig > 0 else 0.5

    # Parse review count
    review_count = 0
    for m in re.finditer(r"\d[\d,]*", str(reviews_count)):
        try:
            review_count = int(m.group().replace(",", "")); break
        except ValueError:
            pass

    # Parse rating (0–5 scale)
    avg_rating = 0.0
    for m in re.finditer(r"\d\.?\d*", str(rating)):
        try:
            v = float(m.group())
            if 0 < v <= 5:
                avg_rating = v; break
        except ValueError:
            pass

    # Parse price
    price_numeric = 0.0
    for m in re.finditer(r"[\d,]+(?:\.\d+)?", str(price).replace(",", "")):
        try:
            price_numeric = float(m.group())
            if price_numeric > 0: break
        except ValueError:
            pass

    return {
        "description_length":  len(words),
        "benefit_word_count":  benefit_n,
        "feature_word_count":  feature_n,
        "benefit_ratio":       round(benefit_ratio, 2),
        "has_social_proof":    review_count > 0 or avg_rating > 0,
        "review_count":        review_count,
        "avg_rating":          avg_rating,
        "price_point":         price,
        "price_numeric":       price_numeric,
        "headline_word_count": len(re.findall(r"\b\w+\b", name)),
    }


def _build_signal_user_content(
    resolved_name: str,
    url: str | None,
    category: str,
    resolved_desc: str,
    resolved_price: str,
    vs: dict,
    ts: dict,
    tiktok_signals: str,
    insta_signals: str,
) -> str:
    bg_label = "white/studio background" if vs.get("background_is_white") else "colored/lifestyle background"
    return f"""Product: {resolved_name}
URL: {url or 'not provided'}
Category: {category}

=== SCRAPED SIGNALS — use these as PRIMARY EVIDENCE for scoring ===

VISUAL SIGNALS (from {vs.get('images_analyzed', 0)} images downloaded + page analysis):
- Product images available: {vs.get('image_count', 0)}
- Lifestyle / model shots detected: {'Yes' if vs.get('has_lifestyle_shot') else 'No'}
- Before/after transformation imagery: {'Yes' if vs.get('has_before_after') else 'No'}
- Video content present: {'Yes' if vs.get('has_video') else 'No'}
- Dominant color of first image: {vs.get('primary_color_name', 'unknown')}
- Background type: {bg_label}
- Average image file size: {vs.get('avg_image_size_kb', 0):.1f} KB (larger = higher resolution)
- Image richness score: {vs.get('image_richness', 0)}/10
- Visual contrast score: {vs.get('visual_contrast', 0)}/10
- Image quality proxy: {vs.get('image_quality_proxy', 0)}/10

TEXT SIGNALS (from scraped page content):
- Description length: {ts.get('description_length', 0)} words
- Benefit-oriented language: {ts.get('benefit_ratio', 0.5):.0%} of signal words
- Benefit signal word count: {ts.get('benefit_word_count', 0)}
- Feature/spec word count: {ts.get('feature_word_count', 0)}
- Product title length: {ts.get('headline_word_count', 0)} words

SOCIAL PROOF:
- Reviews found: {ts.get('review_count', 0)}
- Average rating: {ts.get('avg_rating', 0) or 'not found'} / 5.0

PRICE: {resolved_price or 'not found'}

=== USER-PROVIDED CONTEXT ===
{resolved_desc[:500]}

=== SOCIAL TREND SIGNALS ===
TikTok: {tiktok_signals}
Instagram: {insta_signals}

Score all 7 dimensions using the SCRAPED SIGNALS above as primary evidence.
In each "evidence" field, cite the specific scraped signals that drove the score.
"""


# ── Agent class ─────────────────────────────────────────────────────────────────

class ViralityPredictor:
    def __init__(
        self,
        llm_client: "GroqClient",
        scraper: "WebScraper",
        search_agent: "SearchAgentT",
    ) -> None:
        self.llm    = llm_client
        self.scraper = scraper
        self.search  = search_agent

    async def predict(
        self,
        url: str | None = None,
        product_name: str | None = None,
        description: str | None = None,
        category: str | None = None,
    ) -> dict:
        """Score a product's virality potential.

        With a URL: scrapes the product page, downloads images, extracts
        visual + text signals, then passes them as primary evidence to the LLM.
        Without a URL: text-only fallback with a warning label.
        """
        out: dict = {"agent": "virality", "url": url, "product_name": product_name}

        try:
            # ── STEP 1: Scrape product page ────────────────────────────────────
            scraped:    dict          = {}
            pdp_result                = None
            scrape_mode: str          = "text_only"

            if url:
                try:
                    pdp_result = await self.scraper.scrape_pdp(url)
                    if pdp_result.ok:
                        scraped     = pdp_result.value or {}
                        scrape_mode = "url_based"
                    else:
                        scrape_mode = "scrape_failed"
                    # Augment with generic page scrape if PDP missed product name
                    if not scraped.get("product_name") and pdp_result.ok:
                        page_result = await self.scraper.scrape_page(url)
                        page = page_result.value or {}
                        scraped.setdefault("page_title", page.get("title", ""))
                        scraped.setdefault("page_body",  page.get("body_text", "")[:2000])
                except Exception as exc:
                    scraped["scrape_error"] = str(exc)
                    scrape_mode = "scrape_failed"

            # Resolve fields — explicit args override scraped values
            resolved_name  = product_name or scraped.get("product_name") or "Unknown product"
            resolved_desc  = description  or scraped.get("description")  or ""
            resolved_price = scraped.get("price", "")
            resolved_rating       = scraped.get("rating", "")
            resolved_review_count = scraped.get("reviews_count", "")
            resolved_images       = scraped.get("images", [])
            resolved_cta          = scraped.get("cta_text", "")

            if not category:
                combined = f"{resolved_name} {resolved_desc}".lower()
                for kw in ["clothing","fashion","shirt","dress","shoes","bag",
                           "skincare","beauty","makeup","fragrance","gadget",
                           "electronics","furniture","food","fitness","jewel"]:
                    if kw in combined:
                        category = kw; break
                category = category or "ecommerce"

            # ── STEP 2: Extract visual + text signals ──────────────────────────
            visual_signals: dict = {}
            text_signals:   dict = {}

            if scrape_mode == "url_based":
                print(
                    f"  [virality] Extracting visual signals from "
                    f"{len(resolved_images)} image URLs…", flush=True,
                )
                visual_signals = await _extract_visual_signals(resolved_images, scraped)
                text_signals   = _extract_text_signals(
                    resolved_name, resolved_desc, resolved_price,
                    resolved_rating, resolved_review_count,
                )
                print(
                    f"  [virality] Visual: {visual_signals.get('image_count')} images, "
                    f"{visual_signals.get('images_analyzed')} downloaded, "
                    f"color={visual_signals.get('primary_color_name')}, "
                    f"lifestyle={visual_signals.get('has_lifestyle_shot')}", flush=True,
                )

            # ── STEP 3: Social search signals ──────────────────────────────────
            tiktok_results = self.search.search(
                f"{resolved_name} tiktok viral trend", max_results=4
            )
            insta_results = self.search.search(
                f"{resolved_name} instagram trending review", max_results=4
            )

            # Key claims from description
            claim_pat = re.compile(
                r"[^.!?]*(?:best|only|first|unique|revolutionary|award|#1|must-have|"
                r"sold out|viral|limited|exclusive)[^.!?]*[.!?]",
                re.IGNORECASE,
            )
            key_claims = claim_pat.findall(resolved_desc)[:5]

            # ── STEP 4: Build LLM user content ────────────────────────────────
            if scrape_mode == "url_based" and visual_signals:
                user_content = _build_signal_user_content(
                    resolved_name, url, category, resolved_desc, resolved_price,
                    visual_signals, text_signals,
                    _fmt_search(tiktok_results), _fmt_search(insta_results),
                )
            else:
                # Text-only fallback
                product_data = {
                    "product_name":      resolved_name,
                    "category":          category,
                    "price":             resolved_price,
                    "description":       resolved_desc[:1500],
                    "key_claims":        key_claims,
                    "rating":            resolved_rating,
                    "review_count":      resolved_review_count,
                    "image_count":       len(resolved_images),
                    "cta_text":          resolved_cta,
                    "url":               url or "not provided",
                    "tiktok_signals":    _fmt_search(tiktok_results),
                    "instagram_signals": _fmt_search(insta_results),
                }
                user_content = (
                    f"Product data:\n{json.dumps(product_data, indent=2, ensure_ascii=False)}"
                )

            # ── STEP 5: LLM scoring ────────────────────────────────────────────
            raw = await self.llm.analyze_structured(
                system_prompt=VIRALITY_PROMPT,
                user_content=user_content,
                max_tokens=2500,
            )

            # ── STEP 6: Post-process ───────────────────────────────────────────
            if "_parse_error" not in raw:
                dims = raw.get("dimensions", {})
                if dims:
                    recalculated = _weighted_score(dims)
                    llm_score = raw.get("overall_virality_score", recalculated)
                    if abs(llm_score - recalculated) > 10:
                        raw["overall_virality_score"] = recalculated
                        raw["_score_overridden"] = True
                    raw["grade"] = _grade(raw["overall_virality_score"])

                # Attach fallback warning if scraping failed
                if scrape_mode == "scrape_failed":
                    raw["_fallback_warning"] = (
                        "⚠ URL not accessible — scored from description only. "
                        "Provide a working product URL for image-based analysis."
                    )
                elif scrape_mode == "text_only":
                    raw["_fallback_warning"] = (
                        "ℹ No URL provided — scored from description only. "
                        "Add a product URL for image-based visual analysis."
                    )

            out["score"]          = raw.get("overall_virality_score")
            out["grade"]          = raw.get("grade")
            out["analysis"]       = raw
            out["scrape_mode"]    = scrape_mode
            out["visual_signals"] = visual_signals
            out["text_signals"]   = text_signals
            out["product_data_used"] = {
                "name":     resolved_name,
                "category": category,
                "price":    resolved_price,
                "scraped":  scrape_mode == "url_based",
            }

            # ── STEP 7: Virality trajectory ────────────────────────────────────
            try:
                from agents.trend_predictor import get_predictor
                out["virality_trajectory"] = get_predictor().predict_virality_trajectory({
                    "review_count":       resolved_review_count or 0,
                    "rating":             resolved_rating or 4.0,
                    "description_length": len(resolved_desc),
                    "has_images":         len(resolved_images) > 3,
                    "category":           category,
                    "virality_score":     out["score"] or 50,
                })
            except Exception as tex:
                out["virality_trajectory"] = {"error": str(tex)}

        except Exception as exc:
            out["error"] = str(exc)

        return out


# ── Backward-compat shim ────────────────────────────────────────────────────────

async def run(url: str, product_name: str, description: str) -> dict:
    from llm.client import get_client
    from scrapers.web_scraper import WebScraper
    from scrapers.search import SearchAgent
    agent = ViralityPredictor(get_client(), WebScraper(), SearchAgent())
    return await agent.predict(url=url, product_name=product_name, description=description)
