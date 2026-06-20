"""Tech stack detection via Wappalyzer (local, no API key) + manual signals.

Detects: ecommerce platform, payment gateways, analytics, CRM, chat, CDN, A/B testing.
Never raises — returns empty dict on failure.
"""
from __future__ import annotations

import asyncio
import re

import httpx

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,*/*",
}

# Manual signal patterns for common tools (supplements Wappalyzer)
_SIGNALS: dict[str, dict[str, re.Pattern]] = {
    "payment": {
        "Razorpay":   re.compile(r"razorpay", re.I),
        "PayU":       re.compile(r"payu\.in|payumoney", re.I),
        "Cashfree":   re.compile(r"cashfree", re.I),
        "Stripe":     re.compile(r"stripe\.com/v3|js\.stripe\.com", re.I),
        "PayPal":     re.compile(r"paypal\.com/sdk", re.I),
        "PhonePe":    re.compile(r"phonepe", re.I),
        "Juspay":     re.compile(r"juspay", re.I),
    },
    "analytics": {
        "Google Analytics 4": re.compile(r"gtag\(['\"]config['\"]|G-[A-Z0-9]{8,}", re.I),
        "Google Analytics":   re.compile(r"analytics\.js|UA-\d{7,}", re.I),
        "Meta Pixel":         re.compile(r"connect\.facebook\.net|fbq\(", re.I),
        "Hotjar":             re.compile(r"hotjar\.com|hjid", re.I),
        "Clarity":            re.compile(r"clarity\.ms", re.I),
        "Mixpanel":           re.compile(r"mixpanel\.com/track", re.I),
        "Segment":            re.compile(r"cdn\.segment\.com", re.I),
        "CleverTap":          re.compile(r"clevertap", re.I),
        "MoEngage":           re.compile(r"moengage", re.I),
        "WebEngage":          re.compile(r"webengage", re.I),
    },
    "chat_support": {
        "Intercom":   re.compile(r"intercom\.io|intercomSettings", re.I),
        "Freshchat":  re.compile(r"freshchat|freshworks", re.I),
        "Tidio":      re.compile(r"tidio", re.I),
        "Crisp":      re.compile(r"crisp\.chat|CRISP_WEBSITE_ID", re.I),
        "Drift":      re.compile(r"drift\.com", re.I),
        "Gorgias":    re.compile(r"gorgias", re.I),
        "Zendesk":    re.compile(r"zendesk\.com|zopim", re.I),
    },
    "reviews": {
        "Yotpo":      re.compile(r"yotpo\.com|yotpoWidgetsRenderV2", re.I),
        "Judge.me":   re.compile(r"judge\.me", re.I),
        "Okendo":     re.compile(r"okendo", re.I),
        "Stamped.io": re.compile(r"stamped\.io", re.I),
        "Loox":       re.compile(r"loox\.io", re.I),
    },
    "ab_testing": {
        "Google Optimize": re.compile(r"googleoptimize\.com|GTM-", re.I),
        "VWO":             re.compile(r"vwo\.com|_vwo_code", re.I),
        "Optimizely":      re.compile(r"optimizely\.com", re.I),
        "AB Tasty":        re.compile(r"abtasty\.com", re.I),
    },
    "email_marketing": {
        "Klaviyo":     re.compile(r"klaviyo\.com", re.I),
        "Mailchimp":   re.compile(r"mailchimp\.com|chimpstatic", re.I),
        "Omnisend":    re.compile(r"omnisend\.com", re.I),
        "Netcore":     re.compile(r"netcore\.co\.in|netcoresmartech", re.I),
    },
    "cdn": {
        "Cloudflare":  re.compile(r"cloudflare\.com|__cf_bm|cf-ray", re.I),
        "Fastly":      re.compile(r"fastly\.net", re.I),
        "Akamai":      re.compile(r"akamai", re.I),
    },
}


def _scan_html(html: str) -> dict[str, list[str]]:
    """Run all signal patterns against raw HTML. Returns category → [detected tools]."""
    found: dict[str, list[str]] = {}
    for category, tools in _SIGNALS.items():
        hits = [name for name, pat in tools.items() if pat.search(html)]
        if hits:
            found[category] = hits
    return found


async def _wappalyzer_scan(url: str, html: str) -> dict:
    """Run python-wappalyzer on fetched HTML. Returns raw tech dict."""
    try:
        from Wappalyzer import Wappalyzer, WebPage
        wap = await asyncio.to_thread(Wappalyzer.latest)
        page = WebPage(url, html, {})
        techs = await asyncio.to_thread(wap.analyze_with_categories, page)
        # techs = {tech_name: {categories: [...]}}
        by_category: dict[str, list[str]] = {}
        for tech, meta in techs.items():
            for cat in (meta.get("categories") or []):
                by_category.setdefault(cat, []).append(tech)
        return by_category
    except Exception:
        return {}


async def get_tech_stack(website_url: str, prefetched_html: str | None = None) -> dict:
    """Detect tech stack for a website. Never raises.

    Returns:
        {
          "platform": "Shopify" | "WooCommerce" | "Custom" | ...,
          "payment":        [...],
          "analytics":      [...],
          "chat_support":   [...],
          "reviews_tools":  [...],
          "ab_testing":     [...],
          "email_marketing":[...],
          "cdn":            [...],
          "wappalyzer_raw": {...},   # raw category → tools from Wappalyzer
          "signals_raw":    {...},   # raw from manual scan
        }
    """
    html = prefetched_html or ""
    if not html:
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                r = await client.get(website_url, headers=_HEADERS)
                if r.status_code == 200:
                    html = r.text
        except Exception:
            pass

    if not html:
        return {"error": "Could not fetch page"}

    # Run both in parallel
    manual, wap_cats = await asyncio.gather(
        asyncio.to_thread(_scan_html, html),
        _wappalyzer_scan(website_url, html),
        return_exceptions=True,
    )
    if isinstance(manual, Exception):
        manual = {}
    if isinstance(wap_cats, Exception):
        wap_cats = {}

    # Merge: Wappalyzer category names → our category keys
    _WAP_MAP = {
        "Ecommerce":         "platform",
        "Payment processors": "payment",
        "Analytics":         "analytics",
        "Live chat":         "chat_support",
        "CDN":               "cdn",
        "A/B Testing":       "ab_testing",
        "Email":             "email_marketing",
    }
    merged: dict[str, list[str]] = {}
    for wap_cat, tools in wap_cats.items():
        our_key = _WAP_MAP.get(wap_cat, wap_cat.lower().replace(" ", "_"))
        merged.setdefault(our_key, [])
        for t in tools:
            if t not in merged[our_key]:
                merged[our_key].append(t)

    # Layer manual signals on top
    for cat, tools in manual.items():
        merged.setdefault(cat, [])
        for t in tools:
            if t not in merged[cat]:
                merged[cat].append(t)

    # Determine platform
    platform_hits = merged.get("platform", [])
    if "Shopify" in platform_hits or "shopify" in html.lower():
        platform = "Shopify"
    elif "WooCommerce" in platform_hits or "woocommerce" in html.lower():
        platform = "WooCommerce"
    elif "Magento" in platform_hits or "magento" in html.lower():
        platform = "Magento"
    elif platform_hits:
        platform = platform_hits[0]
    else:
        platform = "Custom"

    return {
        "platform":         platform,
        "payment":          merged.get("payment", []),
        "analytics":        merged.get("analytics", []),
        "chat_support":     merged.get("chat_support", []),
        "reviews_tools":    merged.get("reviews", []),
        "ab_testing":       merged.get("ab_testing", []),
        "email_marketing":  merged.get("email_marketing", []),
        "cdn":              merged.get("cdn", []),
        "wappalyzer_raw":   wap_cats,
        "signals_raw":      manual,
    }
