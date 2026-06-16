"""Shopify Admin REST API client — authenticated private app / custom app access."""
from __future__ import annotations

import httpx

_API_VERSION = "2024-01"


def _base(store_url: str) -> str:
    domain = store_url.replace("https://", "").replace("http://", "").rstrip("/")
    return f"https://{domain}/admin/api/{_API_VERSION}"


async def fetch_private_store_data(store_url: str, access_token: str) -> dict:
    """Fetch private store data requiring Admin API access.

    Returns a dict with keys: orders, customers, products, inventory.
    Any section that fails gets {"error": "<msg>"} — no exceptions raised.
    """
    headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}
    base = _base(store_url)
    result: dict = {}

    async with httpx.AsyncClient(timeout=20, headers=headers) as client:

        # ── Orders (last 250) ────────────────────────────────────────────────
        try:
            r = await client.get(f"{base}/orders.json?limit=250&status=any")
            if r.status_code == 200:
                orders = r.json().get("orders", [])
                revenue = sum(float(o.get("total_price", 0)) for o in orders)
                result["orders"] = {
                    "total": len(orders),
                    "total_revenue": round(revenue, 2),
                    "avg_order_value": round(revenue / max(len(orders), 1), 2),
                    "currency": orders[0].get("currency") if orders else "USD",
                    "refund_count": sum(1 for o in orders if o.get("refunds")),
                    "fulfillment_rates": {
                        s: sum(1 for o in orders if o.get("fulfillment_status") == s)
                        for s in ("fulfilled", "unfulfilled", "partial", None)
                        if sum(1 for o in orders if o.get("fulfillment_status") == s) > 0
                    },
                }
            else:
                result["orders"] = {"error": f"HTTP {r.status_code}"}
        except Exception as exc:
            result["orders"] = {"error": str(exc)}

        # ── Customer count ───────────────────────────────────────────────────
        try:
            r = await client.get(f"{base}/customers/count.json")
            result["customers"] = (
                {"total": r.json().get("count", 0)}
                if r.status_code == 200
                else {"error": f"HTTP {r.status_code}"}
            )
        except Exception as exc:
            result["customers"] = {"error": str(exc)}

        # ── Full product catalog with inventory ──────────────────────────────
        try:
            r = await client.get(f"{base}/products.json?limit=250")
            if r.status_code == 200:
                products = r.json().get("products", [])
                total_inv = sum(
                    sum(v.get("inventory_quantity", 0) for v in p.get("variants", []))
                    for p in products
                )
                out_of_stock = sum(
                    1 for p in products
                    if all(v.get("inventory_quantity", 0) <= 0 for v in p.get("variants", []))
                )
                result["products"] = {
                    "count": len(products),
                    "total_inventory": total_inv,
                    "out_of_stock_count": out_of_stock,
                    "avg_price": round(
                        sum(float(p["variants"][0]["price"]) for p in products if p.get("variants"))
                        / max(len(products), 1),
                        2,
                    ),
                    "top_products": [
                        {"title": p["title"], "handle": p.get("handle"), "variants": len(p.get("variants", []))}
                        for p in products[:10]
                    ],
                }
            else:
                result["products"] = {"error": f"HTTP {r.status_code}"}
        except Exception as exc:
            result["products"] = {"error": str(exc)}

        # ── Shop meta (plan, country, timezone) ──────────────────────────────
        try:
            r = await client.get(f"{base}/shop.json")
            if r.status_code == 200:
                shop = r.json().get("shop", {})
                result["shop"] = {
                    "name": shop.get("name"),
                    "plan": shop.get("plan_name"),
                    "country": shop.get("country_name"),
                    "currency": shop.get("currency"),
                    "timezone": shop.get("iana_timezone"),
                    "created_at": shop.get("created_at"),
                }
            else:
                result["shop"] = {"error": f"HTTP {r.status_code}"}
        except Exception as exc:
            result["shop"] = {"error": str(exc)}

    return result


async def verify_token(store_url: str, access_token: str) -> bool:
    """Quick auth check — returns True if the token can reach the shop endpoint."""
    headers = {"X-Shopify-Access-Token": access_token}
    base = _base(store_url)
    try:
        async with httpx.AsyncClient(timeout=8, headers=headers) as client:
            r = await client.get(f"{base}/shop.json")
            return r.status_code == 200
    except Exception:
        return False
