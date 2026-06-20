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
    r"/products/[a-zA-Z0-9_-]",
    r"(?<!/cdn)/shop/[a-zA-Z0-9_-]",
    r"/item/[a-zA-Z0-9_-]",
    r"/p/[a-zA-Z0-9_-]",
    r"/product/[a-zA-Z0-9_-]",
    r"/catalogue/[a-zA-Z0-9_-]",
]
_IMAGE_EXT_RE = re.compile(
    r"\.(png|jpg|jpeg|gif|svg|webp|ico|avif|woff|woff2|ttf|css|js)(\?.*)?$", re.I
)
_CDN_PATH_RE = re.compile(r"/cdn/|/assets/|/static/|/images?/|/media/", re.I)
_ABOUT_SLUGS = ["/about", "/about-us", "/our-story", "/story", "/brand"]

_SHOPIFY_SIGNALS = re.compile(
    r"cdn\.shopify\.com|Shopify\.shop|shopify_section|\/cdn\/shop\/", re.I
)
_WOOCOMMERCE_SIGNALS = re.compile(r"woocommerce|wp-content\/plugins|add-to-cart=", re.I)
_MAGENTO_SIGNALS = re.compile(r"Magento|mage\/|requirejs-config", re.I)


def _detect_platform_from_html(html: str, links: list[str] | None = None) -> str:
    """Best-effort platform detection from raw HTML + link list."""
    combined = html or ""
    if links:
        combined += " " + " ".join(links)
    if not combined.strip():
        return "unknown"
    if _SHOPIFY_SIGNALS.search(combined):
        return "shopify"
    if _WOOCOMMERCE_SIGNALS.search(combined):
        return "woocommerce"
    if _MAGENTO_SIGNALS.search(combined):
        return "magento"
    return "custom"


def _find_product_urls(links: list[str], base_url: str, limit: int = 3) -> list[str]:
    base = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
    found: list[str] = []
    seen: set[str] = set()
    for link in links:
        full = link if link.startswith("http") else urljoin(base, link)
        if full in seen:
            continue
        if _IMAGE_EXT_RE.search(full) or _CDN_PATH_RE.search(full):
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


async def _fetch_shopify_pdp_json(product_url: str) -> dict | None:
    """Fetch Shopify product data via /{handle}.json — bypasses HTML scraping entirely."""
    import httpx as _httpx
    from bs4 import BeautifulSoup as _BS
    json_url = product_url.rstrip("/") + ".json"
    try:
        async with _httpx.AsyncClient(timeout=10, follow_redirects=True) as cl:
            r = await cl.get(json_url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        p = r.json().get("product", {})
        if not p:
            return None
        # Strip HTML from body_html
        body_html = p.get("body_html") or ""
        try:
            description = _BS(body_html, "html.parser").get_text(" ", strip=True)
        except Exception:
            description = body_html
        variants = p.get("variants") or [{}]
        variant = variants[0]
        price = variant.get("price", "")
        compare_price = variant.get("compare_at_price", "")
        in_stock = any(v.get("available") for v in variants) if variants else None
        images = [img["src"] for img in (p.get("images") or [])[:5] if img.get("src")]

        # Variant / option extraction
        options = p.get("options") or []
        variant_options: dict = {}
        for opt in options:
            name = opt.get("name", "")
            values = opt.get("values") or []
            if name and values:
                variant_options[name] = values
        variant_count = len(variants)

        return {
            "product_name":   p.get("title", ""),
            "price":          f"₹{price}" if price else "",
            "compare_price":  f"₹{compare_price}" if compare_price else "",
            "description":    description[:800],
            "in_stock":       in_stock,
            "cta_text":       "Add to Cart",
            "rating":         "",
            "reviews_count":  "",
            "tags":           p.get("tags", ""),
            "product_type":   p.get("product_type", ""),
            "image_urls":     images,
            "image_count":    len(images),
            "variant_count":  variant_count,
            "variant_options": variant_options,  # e.g. {"Size": ["S","M","L"], "Color": ["Black","White"]}
        }
    except Exception:
        return None


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

            # 2. Detect platform from raw HTML + all links (catches redirect-based Shopify stores)
            page_html = homepage.get("page_html", "")
            catalog_platform = _detect_platform_from_html(page_html, homepage.get("links", []))
            product_urls = _find_product_urls(homepage.get("links", []), url)
            pdp_summaries: list[str] = []

            # 3a. For Shopify (or unknown platform): try /products.json → collection scrape fallback.
            # Works for redirect-based stores (rarerabbit.in → thehouseofrare.com).
            # Many Shopify brands block /products.json — in that case we scrape a collection page
            # for product handles instead.
            if not product_urls and catalog_platform not in ("woocommerce", "magento"):
                try:
                    from agents.brand_basics import _resolve_shopify_base
                    import httpx as _httpx
                    import re as _re
                    shopify_base = await _resolve_shopify_base(url)
                    if shopify_base:
                        async with _httpx.AsyncClient(timeout=12, follow_redirects=True,
                            headers={"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
                        ) as _cl:
                            _r = await _cl.get(f"{shopify_base}/products.json?limit=3")
                            if _r.status_code == 200:
                                _prods = _r.json().get("products", [])
                                if _prods:
                                    catalog_platform = "shopify"
                                product_urls = [
                                    f"{shopify_base}/products/{p['handle']}"
                                    for p in _prods[:3] if p.get("handle")
                                ]
                                print(
                                    f"  [content_catalog] Shopify /products.json → "
                                    f"{len(product_urls)} PDP URLs",
                                    flush=True,
                                )
                            elif _r.status_code == 404:
                                # /products.json blocked — scrape a collection page for handles
                                _coll_links = _re.findall(
                                    r'href=["\']?((?:https?://[^/]+)?/collections/[^"\'?#\s]+)',
                                    page_html,
                                )
                                _coll_links = list(dict.fromkeys(
                                    (shopify_base + l if l.startswith("/") else l)
                                    for l in _coll_links
                                    if "gift" not in l.lower()
                                ))
                                for _coll_url in _coll_links[:3]:
                                    _cr = await _cl.get(_coll_url)
                                    if _cr.status_code == 200:
                                        _handles = _re.findall(
                                            r'href=["\']?/products/([a-z0-9_-]+)["\']?',
                                            _cr.text,
                                        )
                                        _handles = list(dict.fromkeys(_handles))[:3]
                                        if _handles:
                                            catalog_platform = "shopify"
                                            product_urls = [
                                                f"{shopify_base}/products/{h}"
                                                for h in _handles
                                            ]
                                            print(
                                                f"  [content_catalog] Shopify collection fallback "
                                                f"({_coll_url}) → {len(product_urls)} PDP URLs",
                                                flush=True,
                                            )
                                            break
                except Exception as _se:
                    print(f"  [content_catalog] Shopify PDP lookup failed — {_se}", flush=True)

            # 3b. Fetch up to 3 PDPs — Shopify JSON API first, HTML scraper fallback
            all_image_urls: list[str] = []
            all_pdp_dicts: list[dict] = []
            for pdp_url in product_urls[:3]:
                try:
                    pdp: dict | None = None
                    if catalog_platform == "shopify":
                        pdp = await _fetch_shopify_pdp_json(pdp_url)
                        if pdp:
                            print(
                                f"  [content_catalog] Shopify JSON PDP: {pdp['product_name'][:50]}"
                                f" ({pdp.get('image_count', 0)} images)",
                                flush=True,
                            )
                            all_image_urls.extend(pdp.pop("image_urls", []))
                    if not pdp:
                        # Non-Shopify or JSON fetch failed — fall back to HTML scraper
                        pdp_result = await self.scraper.scrape_pdp(pdp_url)
                        sources.append(pdp_result)
                        pdp = (pdp_result.value or {}) if (pdp_result.ok and pdp_result.value) else None
                    if pdp and pdp.get("product_name"):
                        pdp_summaries.append(_pdp_summary(pdp))
                        all_pdp_dicts.append(pdp)
                except Exception as _pdp_exc:
                    print(f"  [content_catalog] PDP fetch skipped — {_pdp_exc}", flush=True)

            # 4. Scrape About page if found
            about_text = ""
            about_url = _find_about_url(homepage.get("links", []), url)
            if about_url:
                try:
                    about_result = await self.scraper.scrape_page(about_url)
                    sources.append(about_result)
                    about_page = about_result.value or {}
                    about_text = about_page.get("body_text", "")[:1500]
                except Exception as _about_exc:
                    print(f"  [content_catalog] About page scrape skipped — {_about_exc}", flush=True)

            # 4.5 Extract trust signals, policy pages, cross-sell presence
            homepage_text = homepage.get("body_text", "")
            page_html_lower = (page_html or "").lower()

            _TRUST_PATTERNS = {
                "free_shipping":   re.compile(r"free\s*(?:delivery|shipping|ship)", re.I),
                "easy_returns":    re.compile(r"easy\s*return|free\s*return|hassle.free\s*return|30.day\s*return|7.day\s*return", re.I),
                "cod":             re.compile(r"\bcod\b|cash\s*on\s*delivery", re.I),
                "secure_payment":  re.compile(r"secure\s*pay|safe\s*checkout|ssl|razorpay|payu|stripe", re.I),
                "warranty":        re.compile(r"\d+\s*(?:year|month).{0,10}warranty|warranty\s*\d+", re.I),
                "exchange":        re.compile(r"\beasy\s*exchange\b|\bfree\s*exchange\b", re.I),
            }
            trust_signals: dict[str, bool] = {
                k: bool(p.search(homepage_text + " " + page_html_lower))
                for k, p in _TRUST_PATTERNS.items()
            }

            # Cross-sell / upsell presence (detectable from HTML class names)
            _CROSSSELL_RE = re.compile(
                r'class=["\'][^"\']*(?:recommend|related|you.may|frequently.bought|also.like|upsell|cross.sell)[^"\']*["\']',
                re.I,
            )
            has_cross_sell = bool(_CROSSSELL_RE.search(page_html or ""))

            # Policy pages — standard Shopify paths, also common on other platforms
            policy_text: dict[str, str] = {}
            if catalog_platform in ("shopify", "custom", "unknown"):
                try:
                    import httpx as _hx
                    from urllib.parse import urlparse as _up
                    _base = f"{_up(url).scheme}://{_up(url).netloc}"
                    async with _hx.AsyncClient(timeout=8, follow_redirects=True,
                        headers={"User-Agent": "Mozilla/5.0"}) as _pc:
                        for _slug, _key in [
                            ("/pages/shipping-policy", "shipping"),
                            ("/pages/return-policy",   "returns"),
                            ("/pages/refund-policy",   "returns"),
                        ]:
                            if _key in policy_text:
                                continue
                            try:
                                _pr = await _pc.get(_base + _slug)
                                if _pr.status_code == 200 and len(_pr.text) > 200:
                                    from bs4 import BeautifulSoup as _BS2
                                    _pt = _BS2(_pr.text, "html.parser").get_text(" ", strip=True)
                                    if len(_pt) > 100:
                                        policy_text[_key] = _pt[:600]
                            except Exception:
                                pass
                except Exception:
                    pass

            # 5. Pre-compute content signals (no LLM)
            all_pdp_text = " ".join(pdp_summaries)
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
{'---'.join(pdp_summaries) if pdp_summaries else 'No product pages found on homepage'}

PRODUCT IMAGES: {len(all_image_urls)} images found across {len(pdp_summaries)} PDPs.
{('Sample URLs: ' + ', '.join(all_image_urls[:3])) if all_image_urls else 'No product images extracted.'}

TRUST & CONVERSION SIGNALS (extracted from homepage):
{chr(10).join(f'- {k.replace("_"," ").title()}: {"YES" if v else "NO"}' for k, v in trust_signals.items())}
- Cross-sell / Upsell section present: {'YES' if has_cross_sell else 'NO'}

POLICY PAGES:
{chr(10).join(f'- {k.title()} policy found: {v[:200]}' for k, v in policy_text.items()) if policy_text else '- No policy pages found'}

VARIANT DATA (from product pages):
{chr(10).join(f"- {d.get('product_name','?')}: {d.get('variant_count','?')} variants, options={d.get('variant_options',{})}" for d in all_pdp_dicts if d.get('variant_options')) or '- No variant data extracted'}"""

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

            # Catalog coverage note — explains 0-PDP result clearly
            if len(pdp_summaries) == 0:
                if blocked:
                    catalog_status_note = "site_blocked_no_pdp_access"
                elif catalog_platform == "shopify":
                    catalog_status_note = "shopify_pdp_links_not_found_on_homepage"
                elif catalog_platform in ("woocommerce", "magento"):
                    catalog_status_note = f"{catalog_platform}_pdp_links_not_found"
                else:
                    catalog_status_note = "non_shopify_platform_catalog_not_available"
            else:
                catalog_status_note = "pdps_scraped_successfully"

            _gap_reasons = {
                "site_blocked_no_pdp_access": "Product pages are protected by Cloudflare — scraped brand info from homepage only. Product quality scoring is estimated.",
                "shopify_pdp_links_not_found_on_homepage": "Shopify store found but no product links on homepage — couldn't audit individual product pages.",
                "pdps_scraped_successfully": None,
            }
            out["data_gap_reason"] = _gap_reasons.get(catalog_status_note)

            out["catalog_platform"] = catalog_platform
            out["catalog_status_note"] = catalog_status_note
            out["product_urls_found"] = product_urls
            out["pdps_scraped"] = len(pdp_summaries)
            out["product_images_found"] = len(all_image_urls)
            out["product_image_urls"] = all_image_urls[:15]
            out["trust_signals"] = trust_signals
            out["has_cross_sell"] = has_cross_sell
            out["policy_pages"] = {k: v[:300] for k, v in policy_text.items()}
            out["pdp_details"] = [
                {
                    "product_name":    d.get("product_name"),
                    "price":           d.get("price"),
                    "compare_price":   d.get("compare_price"),
                    "in_stock":        d.get("in_stock"),
                    "variant_count":   d.get("variant_count"),
                    "variant_options": d.get("variant_options"),
                    "product_type":    d.get("product_type"),
                    "tags":            d.get("tags"),
                }
                for d in all_pdp_dicts
            ]
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
            out["data_gap_reason"] = f"Agent crashed unexpectedly: {type(exc).__name__}: {str(exc)[:150]}. Content analysis unavailable."
            out["status"] = "failed"
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["data_coverage"] = "unavailable"
            out["fallbacks_used"] = []

        return out
