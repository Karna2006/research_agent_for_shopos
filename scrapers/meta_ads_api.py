"""Meta Marketing API client — authenticated ad account access."""
from __future__ import annotations

import httpx

_GRAPH = "https://graph.facebook.com/v20.0"


def _account(ad_account_id: str) -> str:
    return ad_account_id if ad_account_id.startswith("act_") else f"act_{ad_account_id}"


async def fetch_meta_ads_data(access_token: str, ad_account_id: str) -> dict:
    """Fetch Meta Ads performance data from the Marketing API.

    Returns a dict with keys: account_insights, campaigns, top_ads.
    Any section that fails gets {"error": "<msg>"}.
    """
    account = _account(ad_account_id)
    params_base = {"access_token": access_token}
    result: dict = {}

    async with httpx.AsyncClient(timeout=20) as client:

        # ── Account-level insights (last 30 days) ────────────────────────────
        try:
            fields = "spend,impressions,reach,clicks,ctr,cpc,cpm,frequency,actions,cost_per_action_type,purchase_roas"
            r = await client.get(
                f"{_GRAPH}/{account}/insights",
                params={**params_base, "fields": fields, "date_preset": "last_30d", "level": "account"},
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                result["account_insights"] = data[0] if data else {}
            else:
                result["account_insights"] = {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as exc:
            result["account_insights"] = {"error": str(exc)}

        # ── Active campaigns ─────────────────────────────────────────────────
        try:
            fields = "name,status,objective,daily_budget,lifetime_budget,budget_remaining,start_time,stop_time"
            r = await client.get(
                f"{_GRAPH}/{account}/campaigns",
                params={**params_base, "fields": fields, "limit": 50, "effective_status": '["ACTIVE","PAUSED"]'},
            )
            if r.status_code == 200:
                result["campaigns"] = r.json().get("data", [])
            else:
                result["campaigns"] = {"error": f"HTTP {r.status_code}"}
        except Exception as exc:
            result["campaigns"] = {"error": str(exc)}

        # ── Top performing ads (last 30d, sorted by spend) ───────────────────
        try:
            fields = "name,status,creative{thumbnail_url,body,title},insights{spend,impressions,clicks,ctr,cpc}"
            r = await client.get(
                f"{_GRAPH}/{account}/ads",
                params={**params_base, "fields": fields, "limit": 10, "date_preset": "last_30d"},
            )
            if r.status_code == 200:
                result["top_ads"] = r.json().get("data", [])
            else:
                result["top_ads"] = {"error": f"HTTP {r.status_code}"}
        except Exception as exc:
            result["top_ads"] = {"error": str(exc)}

    return result


async def verify_token(access_token: str, ad_account_id: str) -> bool:
    """Quick auth check — returns True if the token can reach the account."""
    account = _account(ad_account_id)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{_GRAPH}/{account}",
                params={"access_token": access_token, "fields": "id,name"},
            )
            return r.status_code == 200
    except Exception:
        return False
