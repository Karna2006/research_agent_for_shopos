"""Agent 1: Brand snapshot — founding story, positioning, hero products, pricing tier."""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx

from llm.prompts import Prompts
from scrapers.result import DataResult
from scrapers.wayback import get_brand_longevity

if TYPE_CHECKING:
    from llm.client import GroqClient
    from scrapers.web_scraper import WebScraper
    from scrapers.search import SearchAgent as SearchAgentT


async def _resolve_shopify_base(url: str) -> str:
    """Return the base URL of the actual Shopify store, following redirects.

    Some brands use a vanity domain (e.g. rarerabbit.in) that redirects their
    homepage to the real Shopify store domain (e.g. thehouseofrare.com), but the
    /products.json path on the vanity domain returns 404. We follow the homepage
    redirect to find the real base URL, then verify it serves /products.json.
    """
    base = url.rstrip("/")
    def _is_shopify_json(r: httpx.Response) -> bool:
        return r.status_code == 200 and '"products"' in r.text[:200]

    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            # First: check if original base already serves products.json
            r = await client.get(f"{base}/products.json?limit=1")
            if _is_shopify_json(r):
                return base

            # Second: follow homepage redirect to discover actual domain
            r_home = await client.get(base)
            final_base = f"{r_home.url.scheme}://{r_home.url.host}".rstrip("/")
            if final_base != base:
                r2 = await client.get(f"{final_base}/products.json?limit=1")
                if _is_shopify_json(r2):
                    return final_base
    except Exception:
        pass
    return base


async def _fetch_shopify_catalog(url: str) -> dict:
    """Fetch /products.json + /collections.json — unauthenticated, works on all Shopify stores.

    Follows vanity-domain redirects to locate the actual Shopify base URL.
    """
    from collections import Counter
    base = await _resolve_shopify_base(url)
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            products_r, collections_r = await asyncio.gather(
                client.get(f"{base}/products.json?limit=250"),
                client.get(f"{base}/collections.json?limit=50"),
                return_exceptions=True,
            )

        products: list[dict] = []
        if not isinstance(products_r, Exception) and products_r.status_code == 200:
            try:
                products = products_r.json().get("products", [])
            except Exception:
                pass  # Non-Shopify site returned HTML for /products.json

        collections: list[dict] = []
        if not isinstance(collections_r, Exception) and collections_r.status_code == 200:
            try:
                collections = collections_r.json().get("collections", [])
            except Exception:
                pass

        if not products:
            return {}

        all_prices: list[float] = []
        for p in products:
            for v in p.get("variants", []):
                try:
                    all_prices.append(float(v.get("price") or 0))
                except (ValueError, TypeError):
                    pass

        all_tags: list[str] = []
        for p in products:
            raw = p.get("tags", "")
            if isinstance(raw, str):
                all_tags.extend(t.strip().lower() for t in raw.split(",") if t.strip())
            elif isinstance(raw, list):
                all_tags.extend(t.lower() for t in raw)
        top_tags = [t for t, _ in Counter(all_tags).most_common(15)]

        variant_counts = [len(p.get("variants", [])) for p in products]
        avg_variants = round(sum(variant_counts) / len(variant_counts), 1) if variant_counts else 0

        sorted_new = sorted(products, key=lambda p: p.get("created_at", ""), reverse=True)
        newest = [p.get("title", "") for p in sorted_new[:5]]

        return {
            "catalog_size":            len(products),
            "price_min":               min(all_prices) if all_prices else None,
            "price_max":               max(all_prices) if all_prices else None,
            "price_avg":               round(sum(all_prices) / len(all_prices)) if all_prices else None,
            "avg_variants_per_product": avg_variants,
            "collection_names":        [c.get("title", "") for c in collections[:20]],
            "top_tags":                top_tags,
            "newest_products":         newest,
            "total_images":            sum(len(p.get("images", [])) for p in products),
        }
    except Exception as exc:
        print(f"  [brand_basics] Shopify catalog fetch failed — {exc}", flush=True)
        return {}


def _fmt_search(results: list[dict], max_chars: int = 1200) -> str:
    lines = [f"- {r.get('title', '')}: {r.get('snippet', '')}" for r in results]
    return "\n".join(lines)[:max_chars]


def _data_coverage(sources: list[DataResult]) -> str:
    if all(dr.confidence == "unavailable" for dr in sources):
        return "unavailable"
    if any(dr.confidence == "unavailable" for dr in sources if dr.source == "homepage_scrape"):
        return "search_only"
    if any(dr.fallback_used for dr in sources):
        return "partial"
    return "full"


class BrandBasicsAgent:
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
        out: dict = {"agent": "brand_basics", "url": url}
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

            # 2. DuckDuckGo searches + Wayback longevity (run in parallel)
            history, linkedin, longevity = await asyncio.gather(
                asyncio.to_thread(self.search.search,
                    f"{brand_name} founded history founders revenue funding", max_results=5),
                asyncio.to_thread(self.search.search,
                    f"{brand_name} linkedin company about", max_results=3),
                get_brand_longevity(url),
                return_exceptions=True,
            )
            if isinstance(history, Exception): history = []
            if isinstance(linkedin, Exception): linkedin = []
            if isinstance(longevity, Exception): longevity = {}
            sources.append(DataResult(
                value={"history": history, "linkedin": linkedin},
                source="duckduckgo_search",
                confidence="inferred" if (history or linkedin) else "unavailable",
            ))

            # 3. Platform detection
            platform = await self.scraper.detect_platform(url)

            # 4. Confirm Shopify via /products.json
            if platform != "shopify":
                try:
                    async with httpx.AsyncClient(timeout=6) as client:
                        r = await client.get(
                            url.rstrip("/") + "/products.json?limit=1",
                            follow_redirects=True,
                        )
                        if r.status_code == 200 and "products" in r.text:
                            platform = "shopify"
                except Exception as _pj_exc:
                    print(f"  [brand_basics] /products.json check skipped — {_pj_exc}", flush=True)

            # 4b. Shopify product catalog (price range, catalog depth, categories)
            shopify_catalog: dict = {}
            if platform == "shopify":
                shopify_catalog = await _fetch_shopify_catalog(url)
                if shopify_catalog:
                    sources.append(DataResult(
                        value=shopify_catalog,
                        source="shopify_products_json",
                        confidence="verified",
                    ))
                    print(
                        f"  [brand_basics] Shopify catalog: {shopify_catalog['catalog_size']} products, "
                        f"₹{shopify_catalog['price_min']}–₹{shopify_catalog['price_max']}",
                        flush=True,
                    )

            # 5. Confidence scoring
            signals = sum([
                not blocked,
                len(history) >= 3,
                bool(homepage.get("schema_json_ld")),
                len(linkedin) >= 1,
            ])
            source_confidence = "high" if signals >= 3 else ("medium" if signals >= 2 else "low")

            # 6. Build user content — handle value=None gracefully
            site_note = ""
            if homepage_result.error:
                site_note = f"\nNOTE: {homepage_result.error} — analysis based on search data only."

            user_content = f"""BRAND: {brand_name}
URL: {url}
PLATFORM: {platform}
HOMEPAGE ACCESSIBLE: {not blocked}{site_note}

TITLE: {homepage.get('title', 'N/A')}
META DESCRIPTION: {homepage.get('meta_description', 'N/A')}
HEADINGS: {' | '.join(homepage.get('headings', [])[:12])}

HOMEPAGE BODY TEXT (truncated):
{homepage.get('body_text', '')[:3500]}

SCHEMA.ORG DATA:
{json.dumps(homepage.get('schema_json_ld', [])[:2], indent=2)[:600]}

SEARCH — history / founders / revenue:
{_fmt_search(history)}

SEARCH — LinkedIn:
{_fmt_search(linkedin)}

WAYBACK MACHINE — domain longevity:
First seen: {longevity.get('first_seen', 'unknown')} | Years online: {longevity.get('years_online', '?')} | Signal: {longevity.get('longevity_signal', 'unknown')} | Crawl frequency: {longevity.get('crawl_frequency', 'unknown')}"""

            if shopify_catalog:
                collections_str = ", ".join(shopify_catalog.get("collection_names", [])[:12])
                tags_str        = ", ".join(shopify_catalog.get("top_tags", [])[:15])
                newest_str      = ", ".join(shopify_catalog.get("newest_products", [])[:5])
                user_content += f"""

SHOPIFY PRODUCT CATALOG:
Total products: {shopify_catalog.get('catalog_size')}
Price range: ₹{shopify_catalog.get('price_min')} – ₹{shopify_catalog.get('price_max')} (avg ₹{shopify_catalog.get('price_avg')})
Avg variants per product: {shopify_catalog.get('avg_variants_per_product')}
Collections: {collections_str}
Top tags: {tags_str}
Newest launches: {newest_str}"""

            # 7. LLM call
            analysis = await self.llm.analyze_structured(
                system_prompt=Prompts.BRAND_BASICS,
                user_content=user_content,
                max_tokens=1200,
            )

            coverage = _data_coverage(sources)
            fallbacks = [dr.fallback_method for dr in sources if dr.fallback_used and dr.fallback_method]

            out["source_confidence"] = source_confidence
            out["platform"] = platform
            out["longevity"] = longevity if longevity else {}
            out["shopify_catalog"] = shopify_catalog
            out["analysis"] = analysis
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["status"] = "partial" if blocked else "complete"
            out["data_coverage"] = coverage
            out["fallbacks_used"] = fallbacks

        except Exception as exc:
            out["error"] = str(exc)
            out["status"] = "failed"
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["data_coverage"] = "unavailable"
            out["fallbacks_used"] = []

        return out
