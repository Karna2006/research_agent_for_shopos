"""Agent 6: Competitive intelligence — rivals, positioning gaps, market opportunities."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from llm.prompts import Prompts
from scrapers.result import DataResult
from scrapers.trends import get_brand_trends
from agents.tracxn_researcher import fetch_tracxn_profile

if TYPE_CHECKING:
    from llm.client import GroqClient
    from scrapers.web_scraper import WebScraper
    from scrapers.search import SearchAgent as SearchAgentT

_CATEGORY_KEYWORDS = [
    "clothing", "fashion", "apparel", "menswear", "womenswear",
    "shoes", "footwear", "bags", "accessories", "jewellery", "jewelry",
    "skincare", "beauty", "cosmetics", "grooming",
    "electronics", "gadgets", "furniture", "home decor",
    "fitness", "sports", "activewear", "food", "nutrition",
]


def _infer_category(text: str) -> str:
    lower = text.lower()
    for cat in _CATEGORY_KEYWORDS:
        if cat in lower:
            return cat
    return "ecommerce"


def _fmt_block(label: str, results: list[dict], max_chars: int = 900) -> str:
    lines = [f"- {r.get('title', '')}: {r.get('snippet', '')[:150]}" for r in results]
    body = "\n".join(lines)[:max_chars]
    return f"{label} ({len(results)} results):\n{body}"


class ResearchAgent:
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
        context: dict | None = None,
    ) -> dict:
        out: dict = {"agent": "research", "url": url}
        sources: list[DataResult] = []

        try:
            # Infer category from prefetched homepage or context; fall back to light scrape
            _pre = prefetched or {}
            _ctx = context or {}
            try:
                if isinstance(_pre.get("homepage"), DataResult):
                    homepage_result = _pre["homepage"]
                else:
                    homepage_result = await self.scraper.scrape_page(url)
                sources.append(homepage_result)
                homepage = homepage_result.value or {}
                # Use context category if provided (from brand_basics), else infer
                raw_category = _ctx.get("category")
                if isinstance(raw_category, list):
                    raw_category = raw_category[0] if raw_category else None
                category = raw_category or _infer_category(
                    " ".join(homepage.get("headings", []))
                    + homepage.get("body_text", "")[:1000]
                )
            except Exception:
                category = "ecommerce"

            # Four targeted searches + Google Trends + Tracxn (all parallel)
            (
                competitors_results,
                market_results,
                trends_results,
                reddit_results,
                google_trends,
                tracxn_data,
            ) = await asyncio.gather(
                asyncio.to_thread(self.search.search,
                    f"{brand_name} competitors alternative brands India", max_results=8),
                asyncio.to_thread(self.search.search,
                    f"{brand_name} market position India 2024 2025", max_results=5),
                asyncio.to_thread(self.search.search,
                    f"{category} market trends India 2025", max_results=5),
                asyncio.to_thread(self.search.search,
                    f"{brand_name} reviews reddit honest opinion", max_results=5),
                get_brand_trends(brand_name, geo="IN"),
                fetch_tracxn_profile(url),
                return_exceptions=True,
            )
            if isinstance(competitors_results, Exception): competitors_results = []
            if isinstance(market_results, Exception): market_results = []
            if isinstance(trends_results, Exception): trends_results = []
            if isinstance(reddit_results, Exception): reddit_results = []
            if isinstance(google_trends, Exception): google_trends = {}
            if isinstance(tracxn_data, Exception): tracxn_data = {}
            sources.append(DataResult(
                value={
                    "competitors": competitors_results,
                    "market": market_results,
                    "trends": trends_results,
                    "reddit": reddit_results,
                },
                source="duckduckgo_search",
                confidence="inferred",
            ))

            # Format Google Trends signal
            _gt_line = ""
            if google_trends and not google_trends.get("error"):
                _gt_line = (
                    f"\nGOOGLE TRENDS (India): relative_interest={google_trends.get('relative_interest','?')}/100 "
                    f"| direction={google_trends.get('trend_direction','?')} "
                    f"| peak_week={google_trends.get('peak_week','?')}"
                )

            # Format Tracxn funding signal (if key is set)
            _tx_line = ""
            if tracxn_data and not tracxn_data.get("note") and tracxn_data.get("company_name"):
                _tx_line = (
                    f"\nTRACXN FUNDING: stage={tracxn_data.get('stage','?')} "
                    f"| total={tracxn_data.get('funding_display','undisclosed')} "
                    f"| investors={','.join((tracxn_data.get('investors') or [])[:3])} "
                    f"| founded={tracxn_data.get('founded','?')}"
                )

            user_content = f"""BRAND: {brand_name}
URL: {url}
INFERRED CATEGORY: {category}{_gt_line}{_tx_line}

{_fmt_block('COMPETITOR / ALTERNATIVE SEARCH', competitors_results)}

{_fmt_block('MARKET POSITION / GROWTH SIGNALS', market_results)}

{_fmt_block('CATEGORY TRENDS 2025', trends_results)}

{_fmt_block('REDDIT / COMMUNITY SENTIMENT', reddit_results)}"""

            analysis = await self.llm.analyze_structured(
                system_prompt=Prompts.COMPETITIVE_RESEARCH,
                user_content=user_content,
                max_tokens=1800,
            )

            # ── Whitespace Score (rules-based, no LLM) ────────────────────────
            competitors_found = len(analysis.get("top_competitors") or [])
            research_score    = float(analysis.get("research_score") or 5)
            # Strong research_score → brand holds its own → more whitespace
            # More competitors → less whitespace
            avg_competitor_strength = min(10, research_score * 0.6 + 3)
            whitespace_raw = (
                100
                - (competitors_found * 8)
                - (avg_competitor_strength * 3)
            )
            whitespace_score = max(0, min(100, round(whitespace_raw)))
            if whitespace_score >= 70:
                whitespace_zone = "Blue Ocean"
                whitespace_msg  = (
                    f"Limited direct competition detected. "
                    f"{competitors_found} competitor{'s' if competitors_found != 1 else ''} identified. "
                    "High opportunity to own the positioning."
                )
            elif whitespace_score >= 40:
                whitespace_zone = "Contested"
                whitespace_msg  = (
                    f"Several strong players present. "
                    f"{competitors_found} competitor{'s' if competitors_found != 1 else ''} identified. "
                    "Differentiation must be crystal clear."
                )
            else:
                whitespace_zone = "Red Ocean"
                whitespace_msg  = (
                    f"Crowded market with {competitors_found} identified competitors. "
                    "Compete on a specific niche or risk being undifferentiated."
                )
            out["whitespace"] = {
                "score":   whitespace_score,
                "zone":    whitespace_zone,
                "message": whitespace_msg,
                "competitors_counted": competitors_found,
            }

            out["category_inferred"] = category
            out["google_trends"] = google_trends if isinstance(google_trends, dict) else {}
            out["tracxn"] = tracxn_data if isinstance(tracxn_data, dict) else {}
            out["search_counts"] = {
                "competitors": len(competitors_results),
                "market": len(market_results),
                "trends": len(trends_results),
                "reddit": len(reddit_results),
            }
            out["analysis"] = analysis

            # ── Market forecast (Chronos → Prophet → numpy) ───────────────────
            try:
                from agents.trend_predictor import get_predictor
                predictor = get_predictor()

                comp_list = analysis.get("top_competitors") or []
                price_hints: list[float] = []
                for comp in comp_list[:3]:
                    pr = comp.get("price_range", "") or ""
                    import re as _re
                    nums = _re.findall(r"\d[\d,]*", pr.replace(",", ""))
                    parsed = [float(n) for n in nums if float(n) > 50]
                    if parsed:
                        price_hints.append(sum(parsed) / len(parsed))

                price_pred = predictor.predict_price_trajectory(
                    price_history=price_hints if len(price_hints) >= 4 else None,
                    category=category,
                )
                review_pred = predictor.predict_review_velocity(category=category)
                out["market_forecast"] = {
                    "price_trend": price_pred,
                    "review_velocity": review_pred,
                    "category": category,
                    "label": "AI-Powered Market Forecast (Next 30 Days)",
                    "model_note": (
                        "Chronos (Amazon's time-series foundation model) → "
                        "Prophet → polynomial regression"
                    ),
                }
            except Exception as fex:
                out["market_forecast"] = {"error": str(fex)}

            fallbacks = [dr.fallback_method for dr in sources if dr.fallback_used and dr.fallback_method]
            blocked = any(dr.confidence == "unavailable" and dr.source == "homepage_scrape" for dr in sources)
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["status"] = "partial" if blocked else "complete"
            out["data_coverage"] = "search_only" if blocked else "full"
            out["fallbacks_used"] = fallbacks

        except Exception as exc:
            out["error"] = str(exc)
            out["status"] = "failed"
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["data_coverage"] = "unavailable"
            out["fallbacks_used"] = []

        return out
