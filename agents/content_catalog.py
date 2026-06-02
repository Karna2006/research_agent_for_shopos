"""Agent 2: PDP + content audit — headline quality, benefit vs feature, CRO rewrites."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from llm.prompts import Prompts
from scrapers.result import DataResult

# ── Pre-compute signals (no LLM required) ─────────────────────────────────────

_BENEFIT_WORDS = frozenset({
    "feel", "look", "transform", "confident", "glow", "energy", "comfortable",
    "easy", "quick", "save", "love", "enjoy", "perfect", "soft", "smooth",
    "beautiful", "amazing", "incredible", "effortless", "radiant", "boost",
    "natural", "instant", "visible", "results", "difference", "better",
    "improve", "enhance", "flawless", "fresh", "vibrant", "hydrate", "nourish",
    "rejuvenate", "calm", "soothe", "dream", "luxury", "premium",
})
_FEATURE_WORDS = frozenset({
    "cotton", "polyester", "cm", "kg", "ml", "gram", "gsm", "thread",
    "diameter", "dimensions", "size", "weight", "nylon", "material",
    "specification", "contains", "ingredients", "formula", "composition",
    "blend", "weave", "gauge", "denier", "micron", "percentage", "concentration",
})


def _benefit_feature_ratio(text: str) -> tuple[float, int, int]:
    """Return (ratio 0-1, benefit_count, feature_count) from text."""
    words = re.findall(r"\b\w+\b", text.lower())
    b = sum(1 for w in words if w in _BENEFIT_WORDS)
    f = sum(1 for w in words if w in _FEATURE_WORDS)
    total = b + f
    return (b / total if total > 0 else 0.5), b, f


def _vader_sentiment(text: str) -> dict:
    """Run VADER sentiment on text. Returns compound score + label."""
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
        _analyzer = SentimentIntensityAnalyzer()
        scores = _analyzer.polarity_scores(text[:3000])
        compound = scores["compound"]
        label = "Positive" if compound > 0.05 else ("Negative" if compound < -0.05 else "Neutral")
        return {"compound": round(compound, 3), "label": label, "scores": scores}
    except ImportError:
        return {"compound": 0.0, "label": "N/A", "scores": {}}

if TYPE_CHECKING:
    from llm.client import GroqClient
    from scrapers.web_scraper import WebScraper
    from scrapers.search import SearchAgent as SearchAgentT

_PRODUCT_PATTERNS = [
    r"/products/[^?#\"'\s]+",
    r"/shop/[^?#\"'\s]+",
    r"/item/[^?#\"'\s]+",
]
_ABOUT_SLUGS = ["/about", "/about-us", "/our-story", "/story", "/brand"]


def _find_product_urls(links: list[str], base_url: str, limit: int = 3) -> list[str]:
    base = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
    found: list[str] = []
    seen: set[str] = set()
    for link in links:
        full = link if link.startswith("http") else urljoin(base, link)
        if full in seen:
            continue
        if any(re.search(p, full) for p in _PRODUCT_PATTERNS):
            found.append(full)
            seen.add(full)
        if len(found) >= limit:
            break
    return found


def _find_about_url(links: list[str], base_url: str) -> str | None:
    base = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
    for link in links:
        full = link if link.startswith("http") else urljoin(base, link)
        path = urlparse(full).path.lower().rstrip("/")
        if any(path.endswith(slug) for slug in _ABOUT_SLUGS):
            return full
    return None


def _pdp_summary(pdp: dict) -> str:
    return (
        f"PRODUCT: {pdp.get('product_name', 'unknown')}\n"
        f"PRICE: {pdp.get('price', '')}\n"
        f"CTA: {pdp.get('cta_text', '')}\n"
        f"RATING: {pdp.get('rating', '')} ({pdp.get('reviews_count', '')} reviews)\n"
        f"IN STOCK: {pdp.get('in_stock', '')}\n"
        f"DESCRIPTION (truncated):\n{pdp.get('description', '')[:600]}\n"
    )


class ContentCatalogAgent:
    def __init__(
        self,
        llm_client: "GroqClient",
        scraper: "WebScraper",
        search_agent: "SearchAgentT",
    ) -> None:
        self.llm = llm_client
        self.scraper = scraper
        self.search = search_agent

    async def run(
        self,
        url: str,
        brand_name: str,
        prefetched: dict | None = None,
    ) -> dict:
        out: dict = {"agent": "content_catalog", "url": url}
        sources: list[DataResult] = []

        try:
            # 1. Scrape homepage (reuse prefetched if available)
            _pre = prefetched or {}
            if isinstance(_pre.get("homepage"), DataResult):
                homepage_result = _pre["homepage"]
            else:
                homepage_result = await self.scraper.scrape_page(url)
            sources.append(homepage_result)
            homepage = homepage_result.value or {}
            blocked = homepage_result.confidence == "unavailable"

            # 2. Find product URLs from homepage links
            product_urls = _find_product_urls(homepage.get("links", []), url)
            pdp_summaries: list[str] = []

            # 3. Scrape up to 3 PDPs
            for pdp_url in product_urls[:3]:
                try:
                    pdp_result = await self.scraper.scrape_pdp(pdp_url)
                    sources.append(pdp_result)
                    pdp = pdp_result.value or {}
                    if pdp_result.ok and pdp.get("product_name"):
                        pdp_summaries.append(_pdp_summary(pdp))
                except Exception:
                    pass

            # 4. Scrape About page if found
            about_text = ""
            about_url = _find_about_url(homepage.get("links", []), url)
            if about_url:
                try:
                    about_result = await self.scraper.scrape_page(about_url)
                    sources.append(about_result)
                    about_page = about_result.value or {}
                    about_text = about_page.get("body_text", "")[:1500]
                except Exception:
                    pass

            # 5. Pre-compute content signals (no LLM)
            all_pdp_text = " ".join(pdp_summaries)
            homepage_text = homepage.get("body_text", "")
            combined_text = all_pdp_text + " " + homepage_text

            bf_ratio, b_count, f_count = _benefit_feature_ratio(combined_text)
            bf_label = f"{round(bf_ratio * 100)}% benefit, {round((1-bf_ratio)*100)}% feature"

            # VADER on visible body copy (approximates review + description sentiment)
            sentiment = _vader_sentiment(combined_text)

            # 6. Build user content (signals injected as hard facts — LLM grades and rewrites)
            site_note = f"\nNOTE: {homepage_result.error}" if homepage_result.error else ""
            user_content = f"""BRAND: {brand_name}
URL: {url}{site_note}

PRE-COMPUTED SIGNALS (objective — use as primary evidence):
- Benefit-vs-Feature ratio: {bf_label} ({b_count} benefit words / {f_count} feature words found)
  Use this ratio directly for the "benefit_vs_feature" field in your output.
- Copy sentiment (VADER): {sentiment['label']} (compound: {sentiment['compound']})
  Positive = customer-first language · Negative = complaints/caveats · Neutral = feature listing

HOMEPAGE TITLE: {homepage.get('title', 'N/A')}
HOMEPAGE META DESCRIPTION: {homepage.get('meta_description', 'N/A')}
HOMEPAGE HEADINGS: {' | '.join(homepage.get('headings', [])[:15])}
HOMEPAGE BODY TEXT (truncated):
{homepage.get('body_text', '')[:2000]}

ABOUT PAGE TEXT:
{about_text or 'Not found'}

PRODUCT PAGES AUDITED ({len(pdp_summaries)} of {len(product_urls)} found):
{'---'.join(pdp_summaries) if pdp_summaries else 'No product pages found on homepage'}"""

            # 7. LLM call
            analysis = await self.llm.analyze_structured(
                system_prompt=Prompts.CONTENT_AUDIT,
                user_content=user_content,
                max_tokens=1800,
            )

            # Ensure benefit_vs_feature is always set (even if LLM skips it)
            if isinstance(analysis, dict) and not analysis.get("benefit_vs_feature"):
                analysis["benefit_vs_feature"] = bf_label

            fallbacks = [dr.fallback_method for dr in sources if dr.fallback_used and dr.fallback_method]
            out["product_urls_found"] = product_urls
            out["pdps_scraped"] = len(pdp_summaries)
            out["precomputed_signals"] = {
                "benefit_vs_feature": bf_label,
                "benefit_ratio": round(bf_ratio, 2),
                "copy_sentiment": sentiment["label"],
                "sentiment_compound": sentiment["compound"],
            }
            out["analysis"] = analysis
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["status"] = "partial" if blocked else "complete"
            out["data_coverage"] = "search_only" if blocked else ("partial" if fallbacks else "full")
            out["fallbacks_used"] = fallbacks

        except Exception as exc:
            out["error"] = str(exc)
            out["status"] = "failed"
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["data_coverage"] = "unavailable"
            out["fallbacks_used"] = []

        return out
