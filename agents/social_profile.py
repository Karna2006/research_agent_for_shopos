"""Agent 7: Social & Brand Presence — Instagram, LinkedIn, Meta Ads creative intelligence."""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from typing import TYPE_CHECKING

from scrapers.result import DataResult
from scrapers.instagram_scraper import scrape_instagram_profile
from scrapers.instagram_handle_finder import discover_handle
from scrapers.ig_handle_cache import get_cached_handle, store_handle

if TYPE_CHECKING:
    from llm.client import GroqClient
    from scrapers.web_scraper import WebScraper
    from scrapers.search import SearchAgent as SearchAgentT

try:
    from scrapling.fetchers import StealthyFetcher as _StealthyFetcher
    _SCRAPLING_AVAILABLE = True
except ImportError:
    _SCRAPLING_AVAILABLE = False


_AD_CREATIVE_PROMPT = """\
You are an ad creative strategist. Analyze this ad image and output ONLY valid JSON:
{
  "hook_type": "emotion|problem|product|offer|ugc",
  "visual_hook_strength": 0,
  "has_human": false,
  "dominant_emotion": "string"
}
hook_type must be exactly one of: emotion, problem, product, offer, ugc.
visual_hook_strength: integer 0-10.
dominant_emotion: single word (e.g. curiosity, joy, fear, trust).
"""

_AD_HOOK_TEXT_PROMPT = """\
You are an ad copywriter. Classify each headline's hook type.
Output ONLY valid JSON: {"classified": [{"text": "headline", "hook_type": "emotion|problem|product|offer|ugc"}]}
Hook types: emotion (triggers feelings), problem (leads with pain point), product (features/benefits),
offer (discount/deal/free), ugc (testimonial/user story style).
"""

_SOCIAL_SYNTHESIS_PROMPT = """\
You are a social media strategist. Given a brand's social media data, output ONLY valid JSON:
{
  "social_presence_score": 0,
  "social_score_reasoning": "1-2 sentence explanation",
  "top_3_social_improvements": [
    "specific improvement 1",
    "specific improvement 2",
    "specific improvement 3"
  ]
}
social_presence_score: integer 0-10. Score 8-10 only for brands with 50k+ Instagram followers,
consistent posting (3+ posts/week), and a diverse active ad library (10+ ads).
Score 5-7 for moderate presence (5k-50k followers, occasional posting, some ads).
Score 1-4 for weak presence (under 5k followers, infrequent posting, no ads).
top_3_social_improvements must be specific and actionable (not generic advice).
"""


def _extract_ig_username(search_results: list[dict]) -> str | None:
    """Extract Instagram username from DDG search results."""
    for r in search_results:
        for field in ("url", "href", "link"):
            url = r.get(field, "")
            m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)/?(?:\?|$)", url)
            if m:
                u = m.group(1)
                if u.lower() not in {"p", "explore", "reel", "reels", "stories", "tv", "accounts"}:
                    return u
        for field in ("title", "snippet", "body"):
            text = r.get(field, "")
            m = re.search(r"@([A-Za-z0-9_.]{2,30})", text)
            if m:
                return m.group(1)
    return None


def _extract_linkedin_url(search_results: list[dict]) -> str | None:
    """Extract LinkedIn company page URL from DDG search results."""
    for r in search_results:
        for field in ("url", "href", "link"):
            url = r.get(field, "")
            m = re.search(r"(https?://(?:www\.)?linkedin\.com/company/[^/?#\s]+)", url)
            if m:
                return m.group(1)
    return None



async def _fetch_linkedin(url: str) -> dict:
    """Fetch LinkedIn company page via StealthyFetcher; graceful fallback on failure."""
    if not _SCRAPLING_AVAILABLE:
        return {"confidence": "unavailable", "error": "scrapling not installed"}
    try:
        page = await _StealthyFetcher.fetch(url, headless=True, solve_cloudflare=True)
        if page is None or page.status not in (200, 999):
            return {"confidence": "unavailable", "error": f"HTTP {getattr(page, 'status', '?')}"}
        soup = page.soup

        name_el = soup.find("h1")
        company_name = name_el.get_text(strip=True) if name_el else ""

        employees = ""
        for el in soup.find_all(["span", "div", "p"]):
            text = el.get_text(strip=True)
            if re.search(r"\d[\d,]+\s+employee", text, re.IGNORECASE):
                employees = text[:80]
                break

        industry = ""
        for el in soup.find_all(["span", "div", "li"]):
            text = el.get_text(strip=True)
            if 3 < len(text) < 60 and any(
                kw in text.lower() for kw in ["industry", "retail", "consumer", "ecommerce", "fashion", "health"]
            ):
                industry = text
                break

        desc_el = soup.find(attrs={"class": re.compile(r"description|about", re.IGNORECASE)})
        description = desc_el.get_text(strip=True)[:400] if desc_el else ""
        if not description:
            body = soup.get_text(separator=" ")
            description = re.sub(r"\s{3,}", " ", body).strip()[:400]

        return {
            "company_name": company_name,
            "employees": employees,
            "industry": industry,
            "description": description,
            "confidence": "verified" if company_name else "inferred",
        }
    except Exception as exc:
        return {"confidence": "unavailable", "error": str(exc)}


async def _classify_ad_hooks(headlines: list[str], llm) -> list[dict]:
    """Use LLM to classify hook types from ad headlines (text-only fallback)."""
    if not headlines:
        return []
    user_content = "Headlines:\n" + "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines))
    try:
        result = await llm.analyze_structured(
            system_prompt=_AD_HOOK_TEXT_PROMPT,
            user_content=user_content,
            max_tokens=400,
        )
        classified = result.get("classified") or []
        return [
            {
                "ad_headline": item.get("text", h),
                "hook_type": item.get("hook_type", "product"),
                "visual_hook_strength": 0,
                "has_human": False,
                "dominant_emotion": "",
            }
            for item, h in zip(classified, headlines)
        ]
    except Exception:
        return [
            {"ad_headline": h, "hook_type": "product", "visual_hook_strength": 0,
             "has_human": False, "dominant_emotion": ""}
            for h in headlines
        ]


class SocialProfileAgent:
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
        out: dict = {"agent": "social_profile", "url": url}
        sources: list[DataResult] = []

        try:
            # ── Step 1: Find Instagram username (cache → multi-strategy) ─────
            _cached = get_cached_handle(url)
            if _cached:
                username, ig_confidence = _cached
                print(f"  [social_profile] IG handle cache hit: @{username} ({ig_confidence})", flush=True)
            else:
                username, ig_confidence = await discover_handle(
                    brand_name=brand_name,
                    website_url=url,
                    search_agent=self.search,
                )
                store_handle(url, username, ig_confidence)

            # ── Step 2: Fetch Instagram profile via mobile API + Playwright ────
            # Uses scrapers/instagram_scraper.py — no instaloader, no auth required.
            # Attempt order: mobile API → Playwright browser → empty result.
            instagram: dict = {}
            ig_post_images: list[str] = []
            if username:
                try:
                    ig_raw = await scrape_instagram_profile(username)
                    # Promote scrapling bio-only result: try DDG search to fill missing followers
                    if not ig_raw.get("followers") and ig_raw.get("bio") and username:
                        try:
                            _ddg_snippets = self.search.search(
                                f"{brand_name} instagram followers site:instagram.com OR site:socialblade.com",
                                max_results=5,
                            )
                            from scrapers.instagram_scraper import _parse_followers_from_og_desc, _parse_ig_number
                            import re as _re
                            _FOLLOWERS_PATTERN = _re.compile(
                                r"([\d,\.]+[KMBkmb]?)\s*(?:followers|Followers)",
                                _re.IGNORECASE,
                            )
                            for _r in _ddg_snippets:
                                _text = " ".join(str(_r.get(f, "")) for f in ("title", "snippet", "body"))
                                _m = _FOLLOWERS_PATTERN.search(_text)
                                if _m:
                                    _estimated = _parse_ig_number(_m.group(1))
                                    if _estimated:
                                        ig_raw = dict(ig_raw)
                                        ig_raw["followers"] = _estimated
                                        ig_raw["confidence"] = "inferred"
                                        break
                        except Exception:
                            pass

                    if ig_raw.get("followers") or ig_raw.get("posts_count"):
                        # Map fields to the shape the rest of Agent 7 expects
                        posts = ig_raw.get("recent_posts", [])
                        dates = [p.get("timestamp") for p in posts if p.get("timestamp")]
                        posting_freq = ""
                        if len(dates) >= 2:
                            from datetime import datetime as _dt
                            d0 = _dt.fromtimestamp(min(dates))
                            d1 = _dt.fromtimestamp(max(dates))
                            days_span = max(1, abs((d1 - d0).days))
                            posts_per_week = round(len(dates) / days_span * 7, 1)
                            posting_freq = f"{posts_per_week} posts/week"

                        all_likes = [p.get("like_count") or 0 for p in posts if p.get("like_count")]
                        all_comments = [p.get("comment_count") or 0 for p in posts if p.get("comment_count")]
                        avg_interact = (
                            (sum(all_likes) + sum(all_comments)) / len(posts)
                            if posts and ig_raw.get("followers")
                            else 0
                        )
                        eng_rate = round(avg_interact / ig_raw["followers"] * 100, 2) if ig_raw.get("followers") else 0

                        all_tags: list[str] = []
                        for p in posts:
                            all_tags.extend(p.get("hashtags", []))
                        top_hashtags = [t for t, _ in Counter(all_tags).most_common(10)]

                        video_count = sum(1 for p in posts if p.get("is_video"))
                        photo_count = len(posts) - video_count
                        total = len(posts) or 1
                        content_mix = {
                            "photo_pct":    round(photo_count / total * 100),
                            "video_pct":    round(video_count / total * 100),
                            "carousel_pct": 0,
                        }

                        instagram = {
                            "username":               ig_raw.get("username", username),
                            "followers":              ig_raw.get("followers"),
                            "following":              ig_raw.get("following"),
                            "posts_count":            ig_raw.get("posts_count"),
                            "verified":               ig_raw.get("is_verified", False),
                            "is_business":            ig_raw.get("is_business", False),
                            "bio":                    ig_raw.get("bio", ""),
                            "posting_frequency":      posting_freq,
                            "content_mix":            content_mix,
                            "engagement_estimate":    f"{eng_rate}%" if eng_rate else "unknown",
                            "top_hashtags":           top_hashtags,
                            "recent_captions_sample": [
                                p.get("caption", "")[:200] for p in posts[:3] if p.get("caption")
                            ],
                            "data_source": ig_raw.get("source", "instagram_api"),
                            "confidence":  "verified",
                        }
                        ig_post_images = [
                            p.get("image_url") or p.get("thumbnail_url") for p in posts
                            if (p.get("image_url") or p.get("thumbnail_url"))
                            and str(p.get("image_url") or p.get("thumbnail_url", "")).startswith("http")
                        ][:6]
                        sources.append(DataResult(
                            value=instagram,
                            source=ig_raw.get("source", "instagram_api"),
                            source_url=f"https://www.instagram.com/{username}/",
                            confidence="verified",
                        ))
                    elif ig_raw.get("bio"):
                        # Scrapling got bio/profile but no follower count
                        instagram = {
                            "username": username,
                            "bio": ig_raw.get("bio", ""),
                            "profile_pic_url": ig_raw.get("profile_pic_url"),
                            "followers": ig_raw.get("followers"),  # may be None
                            "confidence": "partial",
                            "data_source": ig_raw.get("source", "scrapling"),
                        }
                        sources.append(DataResult(
                            value=instagram, source="scrapling",
                            source_url=f"https://www.instagram.com/{username}/",
                            confidence="partial",
                        ))
                    else:
                        instagram = {
                            "username": username, "error": ig_raw.get("error"),
                            "confidence": "unavailable", "data_source": ig_raw.get("source", "failed"),
                        }
                        sources.append(DataResult(
                            value={}, source="instagram_api",
                            source_url=f"https://www.instagram.com/{username}/",
                            confidence="unavailable", error=ig_raw.get("error"),
                        ))
                except Exception as exc:
                    instagram = {"username": username, "error": str(exc), "confidence": "unavailable"}
                    sources.append(DataResult(
                        value={}, source="instagram_api", confidence="unavailable", error=str(exc)
                    ))
            else:
                sources.append(DataResult(
                    value={}, source="instagram_api", confidence="unavailable",
                    error="Instagram handle not found via search",
                ))

            # ── Step 3: Find + fetch LinkedIn ─────────────────────────────────
            li_results = self.search.search(
                f"{brand_name} linkedin company page", max_results=5
            )
            linkedin_url = _extract_linkedin_url(li_results)

            linkedin: dict = {}
            if linkedin_url:
                linkedin = await _fetch_linkedin(linkedin_url)
                sources.append(DataResult(
                    value=linkedin, source="linkedin_scrapling",
                    source_url=linkedin_url,
                    confidence=linkedin.get("confidence", "inferred"),
                    error=linkedin.get("error"),
                ))
            else:
                # Use DDG search snippets as fallback — better than nothing
                snippet = " ".join(r.get("snippet") or r.get("body", "") for r in li_results[:2])[:300]
                linkedin = {
                    "description": snippet, "confidence": "inferred",
                    "company_name": "", "employees": "", "industry": "",
                }
                sources.append(DataResult(
                    value=linkedin, source="duckduckgo_search",
                    confidence="inferred" if snippet else "unavailable",
                ))

            # ── Step 4: Meta Ads (all platforms + Instagram-specific) ──────────
            from scrapers.meta_ads import get_ads
            ads_result: DataResult = await get_ads(
                brand_name, search_agent=self.search, llm_client=self.llm
            )
            # Also run an Instagram-platform-filtered query to get IG-specific ads
            ig_ads_result: DataResult = await get_ads(
                brand_name, search_agent=self.search, llm_client=self.llm,
                instagram_only=True,
            )
            # Prefer Instagram-only if it found more headlines
            ig_ads_data = ig_ads_result.value or {}
            _best_ads = (
                ig_ads_result
                if len(ig_ads_data.get("sample_headlines") or []) >
                   len((ads_result.value or {}).get("sample_headlines") or [])
                else ads_result
            )
            sources.append(DataResult(
                value=_best_ads.value or {}, source="meta_ads_library",
                source_url=_best_ads.source_url,
                confidence=_best_ads.confidence,
                error=_best_ads.error,
                manual_check_url=_best_ads.manual_check_url,
            ))

            ads_data = _best_ads.value or {}
            ad_headlines = (ads_data.get("sample_headlines") or [])[:5]
            ads_count = ads_data.get("ads_count", 0)
            active_ads = ads_count if isinstance(ads_count, int) else len(ad_headlines)

            # Attach Instagram ad image URLs from the IG-filtered scrape
            ig_ad_image_urls = ig_ads_data.get("ad_image_urls") or []
            if not ig_ad_image_urls and ig_post_images:
                # Use scraped Instagram post thumbnails as proxy for IG ad creatives
                ig_ad_image_urls = ig_post_images

            # ── Step 5: Ad creative intelligence ──────────────────────────────
            # Use IG post/ad images when available (scraped from instagram.com
            # or from Meta Ads Library IG-filtered scrape)
            ad_image_urls = ig_ad_image_urls or ads_data.get("ad_image_urls") or []
            creative_analysis: list[dict] = []
            hook_types: list[str] = []

            if ad_image_urls:
                # Vision path: Llama 4 Scout per image
                for i, img_url in enumerate(ad_image_urls[:3]):
                    if not img_url or not str(img_url).startswith(("http://", "https://")):
                        continue
                    headline = ad_headlines[i] if i < len(ad_headlines) else ""
                    try:
                        raw = await self.llm.analyze_image(
                            system_prompt=_AD_CREATIVE_PROMPT,
                            image_url=img_url,
                            text_prompt=f"Analyze this ad creative for {brand_name}.",
                        )
                        clean = re.sub(
                            r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE
                        ).strip()
                        va = json.loads(clean or "{}")
                        vhs = va.get("visual_hook_strength")
                        if isinstance(vhs, str):
                            m = re.search(r"\d+", vhs)
                            va["visual_hook_strength"] = int(m.group()) if m else 0
                        hook_types.append(va.get("hook_type", ""))
                        creative_analysis.append({
                            "ad_headline": headline,
                            "hook_type": va.get("hook_type", ""),
                            "visual_hook_strength": va.get("visual_hook_strength", 0),
                            "has_human": va.get("has_human", False),
                            "dominant_emotion": va.get("dominant_emotion", ""),
                        })
                    except Exception:
                        hook_types.append("product")
                        creative_analysis.append({
                            "ad_headline": headline, "hook_type": "product",
                            "visual_hook_strength": 0, "has_human": False, "dominant_emotion": "",
                        })
            elif ad_headlines:
                # Text-only path: LLM hook classification from headlines
                creative_analysis = await _classify_ad_hooks(ad_headlines, self.llm)
                hook_types = [c["hook_type"] for c in creative_analysis]

            dominant_hook_type = (
                Counter(hook_types).most_common(1)[0][0] if hook_types else ""
            )

            ad_creative_intelligence = {
                "active_ads": active_ads,
                "dominant_hook_type": dominant_hook_type,
                "creative_analysis": creative_analysis,
                "brand_voice_consistency": ads_data.get("brand_voice_consistency", ""),
                "funnel_coverage": ads_data.get("funnel_coverage", ""),
                "ad_formats": ads_data.get("ad_formats", {}),
                "oldest_ad_date": ads_data.get("oldest_ad_date"),
                "newest_ad_date": ads_data.get("newest_ad_date"),
            }

            # ── Step 6: LLM synthesis for score + improvements ─────────────────
            ig_summary = {
                k: v for k, v in instagram.items()
                if k in ("username", "followers", "posting_frequency", "engagement_estimate",
                         "content_mix", "verified", "is_business", "confidence")
            }
            li_summary = {
                k: v for k, v in linkedin.items()
                if k in ("company_name", "employees", "industry", "confidence")
            }
            synthesis = await self.llm.analyze_structured(
                system_prompt=_SOCIAL_SYNTHESIS_PROMPT,
                user_content=(
                    f"Brand: {brand_name}\n"
                    f"Instagram: {json.dumps(ig_summary, default=str)}\n"
                    f"LinkedIn: {json.dumps(li_summary, default=str)}\n"
                    f"Ads: active_ads={active_ads}, dominant_hook={dominant_hook_type}, "
                    f"headlines={ad_headlines[:3]}"
                ),
                max_tokens=500,
            )

            # ── Assemble ───────────────────────────────────────────────────────
            fallbacks = [
                dr.fallback_method for dr in sources if dr.fallback_used and dr.fallback_method
            ]
            has_data = bool(
                instagram.get("followers") or linkedin.get("company_name") or active_ads
            )
            any_unavailable = any(dr.confidence == "unavailable" for dr in sources)
            coverage = (
                "unavailable" if not has_data
                else "partial" if any_unavailable
                else "full"
            )

            # Attach clickable profile URLs to output dicts
            if instagram.get("username"):
                instagram["profile_url"] = f"https://www.instagram.com/{instagram['username']}/"
            if linkedin_url:
                linkedin["profile_url"] = linkedin_url
            out["instagram"] = instagram
            out["linkedin"] = linkedin
            out["ad_creative_intelligence"] = ad_creative_intelligence
            out["social_presence_score"] = synthesis.get("social_presence_score", 0)
            out["social_score_reasoning"] = synthesis.get("social_score_reasoning", "")
            out["top_3_social_improvements"] = synthesis.get("top_3_social_improvements") or []
            out["analysis"] = synthesis
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["status"] = "complete"
            out["data_coverage"] = coverage
            out["fallbacks_used"] = fallbacks

        except Exception as exc:
            out["error"] = str(exc)
            out["status"] = "failed"
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["data_coverage"] = "unavailable"
            out["fallbacks_used"] = []

        return out
