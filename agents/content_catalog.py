"""Agent 2: PDP + content audit — headline quality, benefit vs feature, CRO rewrites."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from llm.prompts import Prompts
from scrapers.result import DataResult

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

            # 5. Build user content
            site_note = f"\nNOTE: {homepage_result.error}" if homepage_result.error else ""
            user_content = f"""BRAND: {brand_name}
URL: {url}{site_note}

HOMEPAGE TITLE: {homepage.get('title', 'N/A')}
HOMEPAGE META DESCRIPTION: {homepage.get('meta_description', 'N/A')}
HOMEPAGE HEADINGS: {' | '.join(homepage.get('headings', [])[:15])}
HOMEPAGE BODY TEXT (truncated):
{homepage.get('body_text', '')[:2000]}

ABOUT PAGE TEXT:
{about_text or 'Not found'}

PRODUCT PAGES AUDITED ({len(pdp_summaries)} of {len(product_urls)} found):
{'---'.join(pdp_summaries) if pdp_summaries else 'No product pages found on homepage'}"""

            # 6. LLM call
            analysis = await self.llm.analyze_structured(
                system_prompt=Prompts.CONTENT_AUDIT,
                user_content=user_content,
                max_tokens=1800,
            )

            fallbacks = [dr.fallback_method for dr in sources if dr.fallback_used and dr.fallback_method]
            out["product_urls_found"] = product_urls
            out["pdps_scraped"] = len(pdp_summaries)
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
