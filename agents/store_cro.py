"""Agent 5: Technical + CRO audit — PageSpeed, trust signals, funnel friction."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

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

            trust_signals = _detect_signals(html, _TRUST_PATTERNS)
            review_widgets = _detect_signals(html, _REVIEW_PATTERNS)
            payment_options = _detect_signals(html, _PAYMENT_PATTERNS)
            email_capture = _detect_bool(html, _EMAIL_PATTERNS)
            sticky_atc = _detect_bool(html, _STICKY_ATC_PATTERNS)
            cross_sell = _detect_bool(html, _CROSS_SELL_PATTERNS)

            # 4. Scrape /cart page for friction signals
            cart_text = ""
            cart_url = urljoin(f"{urlparse(url).scheme}://{urlparse(url).netloc}", "/cart")
            try:
                cart_result = await self.scraper.scrape_page(cart_url)
                sources.append(cart_result)
                cart_page = cart_result.value or {}
                cart_text = cart_page.get("body_text", "")[:1200]
            except Exception:
                pass

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

HOMEPAGE HEADINGS: {' | '.join(homepage.get('headings', [])[:10])}
HOMEPAGE BODY (truncated): {homepage.get('body_text', '')[:1500]}

CART PAGE TEXT:
{cart_text or 'Could not access /cart'}"""

            # 7. LLM call
            analysis = await self.llm.analyze_structured(
                system_prompt=Prompts.STORE_CRO,
                user_content=user_content,
                max_tokens=1600,
            )

            fallbacks = [dr.fallback_method for dr in sources if dr.fallback_used and dr.fallback_method]
            out["platform_detected"] = platform
            out["non_shopify"] = platform != "shopify"
            out["pagespeed"] = pagespeed
            out["cro_signals"] = {
                "trust_signals": trust_signals,
                "review_widgets": review_widgets,
                "payment_options": payment_options,
                "email_capture": email_capture,
                "sticky_atc": sticky_atc,
                "cross_sell": cross_sell,
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
