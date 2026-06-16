"""Agent 5: Technical + CRO audit — PageSpeed, trust signals, funnel friction."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import httpx

from llm.prompts import Prompts
from scrapers.pagespeed import get_scores
from scrapers.result import DataResult

if TYPE_CHECKING:
    from llm.client import GroqClient
    from scrapers.web_scraper import WebScraper
    from scrapers.search import SearchAgent as SearchAgentT

_TRUST_PATTERNS = {
    "Norton / McAfee SSL badge": r"norton|mcafee|ssl.secure|trust.badge|secured.by",
    "Money-back guarantee": r"money.back|return.policy|refund.guarantee|easy.returns",
    "COD / Cash on delivery": r"cash.on.delivery|cod|pay.on.delivery",
    "Authenticity guarantee": r"authentic|genuine|original.product",
    "Customer support chat": r"livechat|live.chat|whatsapp|chat.with.us|support.chat",
}
_REVIEW_PATTERNS = {
    "Yotpo": r"yotpo",
    "Stamped.io": r"stamped\.io",
    "Judge.me": r"judge\.me",
    "Okendo": r"okendo",
    "Native reviews": r"review-container|product-reviews|customer-reviews",
}
_EMAIL_PATTERNS = r"newsletter|subscribe|email.signup|popup|flyout|klaviyo|mailchimp|omnisend"
_PAYMENT_PATTERNS = {
    "Razorpay": r"razorpay",
    "PayU": r"payu",
    "PayPal": r"paypal",
    "UPI": r"\bupi\b|gpay|phonepe|paytm",
    "Stripe": r"stripe",
    "EMI / No cost EMI": r"emi|no.cost.emi|easy.installment",
    "COD": r"cash.on.delivery|cod",
}
_STICKY_ATC_PATTERNS = r"sticky.*add.to.cart|fixed.*add.to.cart|sticky.*atc|position:sticky.*button"
_CROSS_SELL_PATTERNS = r"frequently.bought|you.may.also|customers.also|related.products|complete.the.look"
_WHATSAPP_PATTERNS   = r"wa\.me|whatsapp\.com|whatsapp\.chat|api\.whatsapp|chat.*whatsapp|whatsapp.*shop"
_SIZE_GUIDE_PATTERNS = r"size.guide|size.chart|sizing.guide|fit.guide|measurement.guide"
_WISHLIST_PATTERNS   = r"wishlist|add.to.wishlist|save.for.later|favourite|saved.items"
_LOYALTY_PATTERNS    = r"loyalty.program|reward.points|earn.points|refer.*earn|loyalty.reward|cashback"
_EMAIL_CRM_PATTERNS  = {
    "Klaviyo": r"klaviyo",
    "Mailchimp": r"mailchimp",
    "Omnisend": r"omnisend",
    "WebEngage": r"webengage",
    "MoEngage": r"moengage",
    "Clevertap": r"clevertap",
    "Shopify Email": r"shopify.*email|email.*shopify",
}


async def _fetch_shopify_catalog(base_url: str) -> dict:
    """Fetch structured product + collection data from public Shopify JSON endpoints.

    Almost all Shopify stores expose these unauthenticated endpoints:
      /products.json?limit=250  → full product catalog with prices and variants
      /collections.json         → site taxonomy / navigation

    Returns a dict with product_count, price_range, price_tiers,
    collections, and top_products. Returns {} on non-Shopify or blocked stores.
    """
    origin = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ShoposBot/1.0)"}
    result: dict = {}

    async with httpx.AsyncClient(
        timeout=12.0, follow_redirects=True, headers=headers
    ) as client:
        # Products JSON
        try:
            r = await client.get(f"{origin}/products.json?limit=250")
            if r.status_code == 200:
                data = r.json()
                products = data.get("products", [])
                if products:
                    prices = []
                    for p in products:
                        for v in p.get("variants", []):
                            try:
                                prices.append(float(v.get("price", 0)))
                            except (ValueError, TypeError):
                                pass

                    if prices:
                        prices.sort()
                        budget = sum(1 for p in prices if p < 1000)
                        mid = sum(1 for p in prices if 1000 <= p < 3000)
                        premium = sum(1 for p in prices if 3000 <= p < 8000)
                        luxury = sum(1 for p in prices if p >= 8000)
                        total = len(prices)
                        result["price_tiers"] = {
                            "budget_pct":  round(budget  / total * 100),
                            "mid_pct":     round(mid     / total * 100),
                            "premium_pct": round(premium / total * 100),
                            "luxury_pct":  round(luxury  / total * 100),
                        }
                        result["price_range"] = {
                            "min": min(prices),
                            "max": max(prices),
                            "median": prices[len(prices) // 2],
                            "currency": "INR",
                        }

                    result["product_count"] = len(products)
                    result["top_products"] = [
                        {
                            "title": p.get("title", ""),
                            "price": p.get("variants", [{}])[0].get("price"),
                            "variants": len(p.get("variants", [])),
                        }
                        for p in products[:8]
                    ]
        except Exception:
            pass

        # Collections JSON
        try:
            r = await client.get(f"{origin}/collections.json")
            if r.status_code == 200:
                data = r.json()
                collections = data.get("collections", [])
                result["collections"] = [c.get("title", "") for c in collections[:20]]
                result["collection_count"] = len(collections)
        except Exception:
            pass

    return result


def _format_shopify_catalog(catalog: dict) -> str:
    if not catalog:
        return "Not available (non-Shopify or endpoints blocked)"
    lines = []
    if "product_count" in catalog:
        lines.append(f"Total products: {catalog['product_count']}")
    if "price_range" in catalog:
        pr = catalog["price_range"]
        lines.append(
            f"Price range: ₹{pr['min']} – ₹{pr['max']} (median ₹{pr['median']})"
        )
    if "price_tiers" in catalog:
        pt = catalog["price_tiers"]
        lines.append(
            f"Price tiers: Budget(<₹1k)={pt['budget_pct']}% | "
            f"Mid(₹1k-3k)={pt['mid_pct']}% | "
            f"Premium(₹3k-8k)={pt['premium_pct']}% | "
            f"Luxury(>₹8k)={pt['luxury_pct']}%"
        )
    if "collections" in catalog:
        lines.append(f"Collections ({catalog.get('collection_count',0)}): {', '.join(catalog['collections'][:10])}")
    if "top_products" in catalog:
        lines.append("Top products (title | price | variants):")
        for p in catalog["top_products"]:
            lines.append(f"  • {p['title']} | ₹{p['price']} | {p['variants']} variants")
    return "\n".join(lines) if lines else "No data extracted"


def _detect_signals(html: str, patterns: dict) -> list[str]:
    found = []
    for label, pat in patterns.items():
        if re.search(pat, html, re.IGNORECASE):
            found.append(label)
    return found


def _detect_bool(html: str, pattern: str) -> bool:
    return bool(re.search(pattern, html, re.IGNORECASE))


class StoreCROAgent:
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
        out: dict = {"agent": "store_cro", "url": url}
        sources: list[DataResult] = []

        try:
            _pre = prefetched or {}

            # 1. PageSpeed (mobile + desktop) — reuse prefetched if available
            if isinstance(_pre.get("pagespeed"), DataResult):
                ps_result = _pre["pagespeed"]
            else:
                ps_result = await get_scores(url)
            sources.append(ps_result)
            pagespeed = ps_result.value or {}

            # 2. Scrape homepage HTML for CRO signals (reuse prefetched if available)
            if isinstance(_pre.get("homepage"), DataResult):
                homepage_result = _pre["homepage"]
            else:
                homepage_result = await self.scraper.scrape_page(url)
            sources.append(homepage_result)
            homepage = homepage_result.value or {}
            html = homepage.get("page_html", "")
            blocked = homepage_result.confidence == "unavailable"

            # 3. Platform detection for non-Shopify note
            platform = await self.scraper.detect_platform(url)
            non_shopify_note = ""
            if platform != "shopify":
                non_shopify_note = (
                    f"\nPLATFORM NOTE: {platform.title()} detected — "
                    "Shopify-specific recommendations not applicable."
                )

            # 3b. Shopify free JSON catalog (zero API key needed)
            shopify_catalog: dict = {}
            if platform == "shopify":
                try:
                    shopify_catalog = await _fetch_shopify_catalog(url)
                    print(
                        f"  [store_cro] Shopify JSON: {shopify_catalog.get('product_count', 0)} products, "
                        f"{shopify_catalog.get('collection_count', 0)} collections",
                        flush=True,
                    )
                except Exception as _se:
                    print(f"  [store_cro] Shopify JSON fetch failed: {_se}", flush=True)

            trust_signals = _detect_signals(html, _TRUST_PATTERNS)
            review_widgets = _detect_signals(html, _REVIEW_PATTERNS)
            payment_options = _detect_signals(html, _PAYMENT_PATTERNS)
            email_capture = _detect_bool(html, _EMAIL_PATTERNS)
            sticky_atc = _detect_bool(html, _STICKY_ATC_PATTERNS)
            cross_sell = _detect_bool(html, _CROSS_SELL_PATTERNS)
            whatsapp_detected = _detect_bool(html, _WHATSAPP_PATTERNS)
            size_guide_detected = _detect_bool(html, _SIZE_GUIDE_PATTERNS)
            wishlist_detected = _detect_bool(html, _WISHLIST_PATTERNS)
            loyalty_detected = _detect_bool(html, _LOYALTY_PATTERNS)
            email_crm_platforms = _detect_signals(html, _EMAIL_CRM_PATTERNS)

            # 4. Scrape /cart page for friction signals
            cart_text = ""
            cart_url = urljoin(f"{urlparse(url).scheme}://{urlparse(url).netloc}", "/cart")
            try:
                cart_result = await self.scraper.scrape_page(cart_url)
                sources.append(cart_result)
                cart_page = cart_result.value or {}
                cart_text = cart_page.get("body_text", "")[:1200]
            except Exception as _cart_exc:
                print(f"  [store_cro] cart page scrape skipped — {_cart_exc}", flush=True)

            # 5. PageSpeed availability note
            ps_note = ""
            if ps_result.error:
                ps_note = f"\nPAGESPEED NOTE: {ps_result.error}"
                if ps_result.manual_check_url:
                    ps_note += f" — check manually at {ps_result.manual_check_url}"

            # 6. Build user content
            site_note = f"\nNOTE: {homepage_result.error}" if homepage_result.error else ""
            user_content = f"""BRAND: {brand_name}
URL: {url}
PLATFORM: {platform}{non_shopify_note}{site_note}

PAGESPEED SCORES:{ps_note}
- Mobile: {pagespeed.get('mobile_score', 'N/A')} ({pagespeed.get('mobile_label', '')})
- Desktop: {pagespeed.get('desktop_score', 'N/A')} ({pagespeed.get('desktop_label', '')})
- LCP: {pagespeed.get('lcp', 'N/A')}
- CLS: {pagespeed.get('cls', 'N/A')}
- FID/TBT: {pagespeed.get('fid', 'N/A')}
- TTFB: {pagespeed.get('ttfb', 'N/A')}
- Top recommendations: {[r['title'] for r in pagespeed.get('recommendations', [])]}

CRO SIGNAL AUDIT:
- Trust signals detected: {trust_signals or ['None detected']}
- Review widgets: {review_widgets or ['None detected']}
- Payment options: {payment_options or ['None detected']}
- Email capture / popup: {email_capture}
- Sticky Add-to-Cart: {sticky_atc}
- Cross-sell / upsell: {cross_sell}

UX SIGNAL AUDIT:
- WhatsApp commerce detected: {whatsapp_detected}
- Size guide detected: {size_guide_detected}
- Wishlist feature detected: {wishlist_detected}
- Loyalty program detected: {loyalty_detected}
- Email/CRM platforms detected: {email_crm_platforms or ['None detected']}

HOMEPAGE HEADINGS: {' | '.join(homepage.get('headings', [])[:10])}
HOMEPAGE BODY (truncated): {homepage.get('body_text', '')[:1500]}

CART PAGE TEXT:
{cart_text or 'Could not access /cart'}

SHOPIFY PRODUCT CATALOG (live JSON endpoint data):
{_format_shopify_catalog(shopify_catalog)}"""

            # 7. LLM call
            analysis = await self.llm.analyze_structured(
                system_prompt=Prompts.STORE_CRO,
                user_content=user_content,
                max_tokens=1600,
            )

            fallbacks = [dr.fallback_method for dr in sources if dr.fallback_used and dr.fallback_method]
            out["platform_detected"] = platform
            out["non_shopify"] = platform != "shopify"
            out["shopify_catalog"] = shopify_catalog if shopify_catalog else None
            out["pagespeed"] = pagespeed
            out["cro_signals"] = {
                "trust_signals": trust_signals,
                "review_widgets": review_widgets,
                "payment_options": payment_options,
                "email_capture": email_capture,
                "sticky_atc": sticky_atc,
                "cross_sell": cross_sell,
                "whatsapp_detected": whatsapp_detected,
                "size_guide_detected": size_guide_detected,
                "wishlist_detected": wishlist_detected,
                "loyalty_detected": loyalty_detected,
                "email_crm_platforms": email_crm_platforms,
            }
            out["analysis"] = analysis
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["status"] = "partial" if (blocked or ps_result.error) else "complete"
            out["data_coverage"] = (
                "unavailable" if (blocked and ps_result.error)
                else "partial" if (blocked or ps_result.error)
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
