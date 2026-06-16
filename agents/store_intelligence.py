"""Store Intelligence agent — optional deep analysis using private API connectors.

Runs only when BrandConnector row exists for the brand URL.
Fetches private Shopify + Meta Ads data and generates LLM insights.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


async def run(
    url: str,
    llm: Any,
    shopify_token: Optional[str] = None,
    shopify_store_url: Optional[str] = None,
    meta_token: Optional[str] = None,
    meta_account_id: Optional[str] = None,
) -> dict:
    """Run private-data intelligence. Returns structured findings dict."""
    raw: dict = {}

    # ── Shopify private data ─────────────────────────────────────────────────
    if shopify_token and shopify_store_url:
        try:
            from scrapers.shopify_private import fetch_private_store_data
            shopify_data = await fetch_private_store_data(shopify_store_url, shopify_token)
            raw["shopify"] = shopify_data
            logger.info("StoreIntelligence: Shopify data fetched for %s", url)
        except Exception as exc:
            raw["shopify"] = {"error": str(exc)}
            logger.warning("StoreIntelligence: Shopify fetch failed: %s", exc)

    # ── Meta Ads private data ────────────────────────────────────────────────
    if meta_token and meta_account_id:
        try:
            from scrapers.meta_ads_api import fetch_meta_ads_data
            meta_data = await fetch_meta_ads_data(meta_token, meta_account_id)
            raw["meta_ads"] = meta_data
            logger.info("StoreIntelligence: Meta Ads data fetched for %s", url)
        except Exception as exc:
            raw["meta_ads"] = {"error": str(exc)}
            logger.warning("StoreIntelligence: Meta Ads fetch failed: %s", exc)

    if not raw:
        return {"enabled": False, "reason": "no_connectors"}

    # ── LLM synthesis ────────────────────────────────────────────────────────
    summary = _build_summary(raw)
    prompt = _build_prompt(url, summary)

    try:
        response = await llm.generate_content_async(prompt)
        insights_text = response.text.strip()
    except Exception as exc:
        logger.warning("StoreIntelligence: LLM synthesis failed: %s", exc)
        insights_text = "LLM synthesis unavailable."

    return {
        "enabled": True,
        "raw": raw,
        "summary": summary,
        "insights": insights_text,
        "connectors_used": [k for k in ("shopify", "meta_ads") if k in raw and "error" not in raw[k]],
    }


def _build_summary(raw: dict) -> dict:
    summary: dict = {}

    shopify = raw.get("shopify", {})
    if shopify and "error" not in shopify:
        orders = shopify.get("orders", {})
        products = shopify.get("products", {})
        customers = shopify.get("customers", {})
        summary["shopify"] = {
            "revenue_last250_orders": orders.get("total_revenue"),
            "avg_order_value": orders.get("avg_order_value"),
            "total_orders_sample": orders.get("total"),
            "total_customers": customers.get("total"),
            "product_count": products.get("count"),
            "total_inventory": products.get("total_inventory"),
            "out_of_stock": products.get("out_of_stock_count"),
            "avg_product_price": products.get("avg_price"),
            "plan": shopify.get("shop", {}).get("plan"),
        }

    meta = raw.get("meta_ads", {})
    if meta and "error" not in meta:
        insights = meta.get("account_insights", {})
        campaigns = meta.get("campaigns", [])
        summary["meta_ads"] = {
            "spend_30d": insights.get("spend"),
            "impressions_30d": insights.get("impressions"),
            "clicks_30d": insights.get("clicks"),
            "ctr": insights.get("ctr"),
            "cpc": insights.get("cpc"),
            "cpm": insights.get("cpm"),
            "purchase_roas": insights.get("purchase_roas"),
            "active_campaigns": len([c for c in campaigns if isinstance(c, dict) and c.get("status") == "ACTIVE"]),
        }

    return summary


def _build_prompt(url: str, summary: dict) -> str:
    return f"""You are an expert ecommerce analyst with access to private backend data for {url}.

Private data summary:
{json.dumps(summary, indent=2)}

Analyze this data and provide:
1. **Revenue Health**: AOV, revenue trends, order volume assessment
2. **Inventory Risk**: Stock levels, out-of-stock impact, SKU spread
3. **Paid Ads Efficiency**: ROAS, CPC, CTR vs industry benchmarks (ecommerce avg CTR: 1.5%, ROAS: 3-5x)
4. **Customer Economics**: Customer count vs order volume (repeat purchase rate proxy)
5. **Top 3 Actionable Recommendations**: Specific, data-backed, numbered

Keep it concise — 300 words max. Use real numbers from the data."""
