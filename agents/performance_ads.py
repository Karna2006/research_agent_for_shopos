"""Agent 3: Meta Ad Library audit — ad formats, hooks, CTAs, funnel coverage."""
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote_plus

from llm.prompts import Prompts
from scrapers.meta_ads import get_ads
from scrapers.result import DataResult

if TYPE_CHECKING:
    from llm.client import GroqClient
    from scrapers.web_scraper import WebScraper
    from scrapers.search import SearchAgent as SearchAgentT

META_ADS_LIBRARY_BASE = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&country=ALL&q={q}"
)


def _fmt_search(results: list[dict], max_chars: int = 1200) -> str:
    lines = [f"- {r.get('title', '')}: {r.get('snippet', '')}" for r in results]
    return "\n".join(lines)[:max_chars]


class PerformanceAdsAgent:
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
        out: dict = {"agent": "performance_ads", "url": url}
        meta_ads_url = META_ADS_LIBRARY_BASE.format(q=quote_plus(brand_name))
        out["meta_ads_library_url"] = meta_ads_url
        sources: list[DataResult] = []

        try:
            # 1. Meta Ad Library scrape (reuse prefetched if available)
            _pre = prefetched or {}
            if isinstance(_pre.get("meta_ads"), DataResult):
                ads_result = _pre["meta_ads"]
            else:
                ads_result = await get_ads(brand_name, search_agent=self.search, llm_client=self.llm)
            sources.append(ads_result)
            ads_data = ads_result.value or {}

            # 2. Supplementary searches
            marketing_results = self.search.search(
                f"{brand_name} google ads facebook ads marketing strategy", max_results=5
            )
            creative_results = self.search.search(
                f"{brand_name} ad creative ugc influencer", max_results=4
            )
            sources.append(DataResult(
                value=marketing_results,
                source="duckduckgo_search",
                confidence="inferred",
            ))

            # 3. Describe the ad status for the LLM
            ads_status = ads_data.get("status", "unknown")
            if ads_result.error:
                ads_note = f"BLOCKED — {ads_result.error}"
                if ads_result.fallback_used:
                    ads_note += f" (fallback: {ads_result.fallback_method})"
            elif ads_status == "not_found":
                ads_note = ads_data.get("display_message", "Brand not found in Meta Ad Library")
            elif ads_status == "found_no_active":
                ads_note = ads_data.get("display_message", "Brand found but 0 active ads")
            else:
                ads_note = "OK"

            user_content = f"""BRAND: {brand_name}
URL: {url}

META AD LIBRARY DATA:
- Source URL: {meta_ads_url}
- Ads count: {ads_data.get('ads_count', 'unknown')}
- Ad status: {ads_note}
- Ad formats: {ads_data.get('ad_formats', {})}
- Sample headlines: {ads_data.get('sample_headlines', [])}
- Oldest ad: {ads_data.get('oldest_ad_date', 'unknown')}
- Newest ad: {ads_data.get('newest_ad_date', 'unknown')}
- Confidence: {ads_result.confidence}

SEARCH — marketing / ads strategy:
{_fmt_search(marketing_results)}

SEARCH — creative / UGC signals:
{_fmt_search(creative_results)}"""

            # 4. LLM call
            analysis = await self.llm.analyze_structured(
                system_prompt=Prompts.AD_AUDIT,
                user_content=user_content,
                max_tokens=1400,
            )

            fallbacks = [dr.fallback_method for dr in sources if dr.fallback_used and dr.fallback_method]
            out["ads_scrape"] = {
                "ads_count": ads_data.get("ads_count"),
                "ads_status": ads_status,
                "ad_formats": ads_data.get("ad_formats"),
                "sample_headlines": ads_data.get("sample_headlines", []),
                "scrape_confidence": ads_result.confidence,
                "scrape_error": ads_result.error or None,
                "display_message": ads_data.get("display_message"),
                "manual_check_url": ads_result.manual_check_url,
            }
            out["analysis"] = analysis
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["status"] = "partial" if ads_result.fallback_used else "complete"
            out["data_coverage"] = (
                "search_only" if ads_result.confidence == "unavailable"
                else "partial" if ads_result.fallback_used
                else "full"
            )
            out["fallbacks_used"] = fallbacks

        except Exception as exc:
            out["error"] = str(exc)
            out["status"] = "failed"
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["data_coverage"] = "unavailable"
            out["fallbacks_used"] = []

        return out
