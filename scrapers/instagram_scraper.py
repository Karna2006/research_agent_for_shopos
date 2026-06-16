"""Instagram public profile scraper — no auth, public profiles only."""
from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING

import httpx
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

if TYPE_CHECKING:
    pass

# ── Constants ────────────────────────────────────────────────────────────────

_PROFILE_URL = "https://www.instagram.com/{username}/"
_API_URL = "https://i.instagram.com/api/v1/users/web_profile_info/?username={username}"

_MOBILE_HEADERS = {
    "User-Agent": "Instagram 219.0.0.12.117 Android",
    "X-IG-App-ID": "936619743392459",
    "Accept": "*/*",
    "Accept-Language": "en-US",
}

_HASHTAG_RE = re.compile(r"#(\w+)")

# ── Internal helpers ──────────────────────────────────────────────────────────


def _extract_hashtags(caption: str) -> list[str]:
    """Return all hashtags found in a caption string."""
    return _HASHTAG_RE.findall(caption)


def _parse_post_node(node: dict) -> dict:
    """Normalise a single media node from the API response into a post dict."""
    shortcode: str = node.get("shortcode", "")
    url = f"https://instagram.com/p/{shortcode}/" if shortcode else ""
    is_video = bool(node.get("is_video", False))
    # Reels use /reel/ path — yt-dlp handles both /p/ and /reel/ but /reel/ is canonical
    reel_url = (f"https://instagram.com/reel/{shortcode}/" if is_video and shortcode else None)
    video_url = node.get("video_url") or None  # CDN URL (time-limited, use reel_url for yt-dlp)
    video_view_count = node.get("video_view_count") or None

    caption: str = ""
    caption_edges = node.get("edge_media_to_caption", {}).get("edges", [])
    if caption_edges:
        caption = caption_edges[0].get("node", {}).get("text", "") or ""

    return {
        "url": url,
        "reel_url": reel_url,
        "is_video": is_video,
        "video_url": video_url,
        "video_view_count": video_view_count,
        "image_url":     node.get("display_url") if not is_video else None,
        "thumbnail_url": node.get("thumbnail_src") if is_video else None,
        "caption": caption,
        "like_count": node.get("edge_liked_by", {}).get("count"),
        "comment_count": node.get("edge_media_to_comment", {}).get("count"),
        "timestamp": node.get("taken_at_timestamp"),
        "hashtags": _extract_hashtags(caption),
    }


def _empty_result(username: str, source: str, error: str) -> dict:
    """Return a fully-formed but empty result dict for error paths."""
    return {
        "username": username,
        "url": f"https://instagram.com/{username}",
        "bio": "",
        "followers": None,
        "following": None,
        "posts_count": None,
        "is_verified": False,
        "is_business": False,
        "profile_pic_url": None,
        "recent_posts": [],
        "source": source,
        "error": error,
    }


# ── Primary method: semi-public mobile API ───────────────────────────────────


async def _fetch_via_api(username: str) -> dict | None:
    """Attempt to retrieve profile data via the Instagram mobile API endpoint.

    Returns a fully-formed result dict on success, or None when the endpoint
    returns a non-200 response or the expected JSON structure is absent.
    """
    url = _API_URL.format(username=username)
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url, headers=_MOBILE_HEADERS)

        if response.status_code != 200:
            return None

        raw: dict = response.json()
    except Exception:
        return None

    try:
        user: dict = raw["data"]["user"]
    except (KeyError, TypeError):
        return None

    if not user:
        return None

    # ── Profile fields ───────────────────────────────────────────────────────
    bio: str = user.get("biography", "") or ""
    followers: int | None = (
        user.get("edge_followed_by", {}).get("count")
    )
    following: int | None = (
        user.get("edge_follow", {}).get("count")
    )
    posts_count: int | None = (
        user.get("edge_owner_to_timeline_media", {}).get("count")
    )
    is_verified: bool = bool(user.get("is_verified", False))
    is_business: bool = bool(user.get("is_business_account", False))
    profile_pic_url: str | None = user.get("profile_pic_url") or None

    # ── Recent posts (up to 12) ──────────────────────────────────────────────
    media_edges: list[dict] = (
        user.get("edge_owner_to_timeline_media", {}).get("edges", [])
    )
    recent_posts = [
        _parse_post_node(edge["node"])
        for edge in media_edges[:12]
        if isinstance(edge.get("node"), dict)
    ]

    return {
        "username": username,
        "url": f"https://instagram.com/{username}",
        "bio": bio,
        "followers": followers,
        "following": following,
        "posts_count": posts_count,
        "is_verified": is_verified,
        "is_business": is_business,
        "profile_pic_url": profile_pic_url,
        "recent_posts": recent_posts,
        "source": "instagram_api",
        "error": None,
    }


# ── Playwright fallback ───────────────────────────────────────────────────────


async def _fetch_via_playwright(username: str) -> dict | None:
    """Playwright-based fallback for when the mobile API is blocked.

    Attempts in order:
      1. Extract ``window.__additionalDataLoaded`` JSON from the page source.
      2. Extract ``window._sharedData`` JSON from the page source.
      3. Scrape basic info (profile picture, bio) from meta tags.

    Returns a result dict on any partial success, or None on total failure.
    """
    profile_url = _PROFILE_URL.format(username=username)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Version/16.6 Mobile/15E148 Safari/604.1"
                    ),
                    viewport={"width": 390, "height": 844},
                    locale="en-US",
                )
                page = await context.new_page()

                try:
                    await page.goto(profile_url, wait_until="networkidle", timeout=30_000)
                except PWTimeout:
                    # networkidle can time out on IG; still try to parse what loaded
                    pass

                html: str = await page.content()

                # ── Attempt 1: window.__additionalDataLoaded ─────────────────
                additional_data: dict | None = None
                try:
                    additional_data = await page.evaluate(
                        "() => { try { return window.__additionalDataLoaded; } catch(e) { return null; } }"
                    )
                except Exception:
                    pass

                if isinstance(additional_data, dict):
                    # Shape: { "profile": { ...user fields... } }
                    user_data: dict = {}
                    for value in additional_data.values():
                        if isinstance(value, dict) and "biography" in value:
                            user_data = value
                            break
                    if user_data:
                        result = _parse_shared_data_user(username, user_data)
                        if result:
                            result["source"] = "playwright"
                            return result

                # ── Attempt 2: window._sharedData ────────────────────────────
                shared_data: dict | None = None
                try:
                    shared_data = await page.evaluate(
                        "() => { try { return window._sharedData; } catch(e) { return null; } }"
                    )
                except Exception:
                    pass

                if isinstance(shared_data, dict):
                    try:
                        user_data = (
                            shared_data
                            .get("entry_data", {})
                            .get("ProfilePage", [{}])[0]
                            .get("graphql", {})
                            .get("user", {})
                        )
                    except (KeyError, IndexError, TypeError):
                        user_data = {}

                    if user_data:
                        result = _parse_shared_data_user(username, user_data)
                        if result:
                            result["source"] = "playwright"
                            return result

                # ── Attempt 3: meta-tag fallback (basic data only) ────────────
                bio: str = ""
                profile_pic_url: str | None = None

                # og:description often contains "{followers} Followers, {following} Following,
                # {posts} Posts — {bio}"
                og_desc_match = re.search(
                    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']',
                    html,
                    re.IGNORECASE,
                )
                if og_desc_match:
                    bio = og_desc_match.group(1).strip()

                og_image_match = re.search(
                    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']*)["\']',
                    html,
                    re.IGNORECASE,
                )
                if og_image_match:
                    profile_pic_url = og_image_match.group(1).strip() or None

                # Return whatever we found — downstream handles recent_posts=[]
                # gracefully; only return None when we got nothing at all
                if not username:
                    return None

                # og:description bio often contains "756K Followers, 12 Following, 890 Posts"
                og_followers, og_following, og_posts = _parse_followers_from_og_desc(bio)

                return {
                    "username": username,
                    "url": f"https://instagram.com/{username}",
                    "bio": bio,
                    "followers": og_followers,
                    "following": og_following,
                    "posts_count": og_posts,
                    "is_verified": False,
                    "is_business": False,
                    "profile_pic_url": profile_pic_url,
                    "recent_posts": [],
                    "source": "playwright",
                    "error": None,
                }

            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    except Exception:
        return None


def _parse_shared_data_user(username: str, user: dict) -> dict | None:
    """Parse a user object from ``window._sharedData`` or ``__additionalDataLoaded``."""
    if not isinstance(user, dict):
        return None

    bio: str = user.get("biography", "") or ""
    followers: int | None = (
        user.get("edge_followed_by", {}).get("count")
        if isinstance(user.get("edge_followed_by"), dict)
        else None
    )
    following: int | None = (
        user.get("edge_follow", {}).get("count")
        if isinstance(user.get("edge_follow"), dict)
        else None
    )
    posts_count: int | None = (
        user.get("edge_owner_to_timeline_media", {}).get("count")
        if isinstance(user.get("edge_owner_to_timeline_media"), dict)
        else None
    )

    media_edges: list[dict] = (
        user.get("edge_owner_to_timeline_media", {}).get("edges", [])
        if isinstance(user.get("edge_owner_to_timeline_media"), dict)
        else []
    )
    recent_posts = [
        _parse_post_node(edge["node"])
        for edge in media_edges[:12]
        if isinstance(edge.get("node"), dict)
    ]

    return {
        "username": username,
        "url": f"https://instagram.com/{username}",
        "bio": bio,
        "followers": followers,
        "following": following,
        "posts_count": posts_count,
        "is_verified": bool(user.get("is_verified", False)),
        "is_business": bool(user.get("is_business_account", False)),
        "profile_pic_url": user.get("profile_pic_url") or None,
        "recent_posts": recent_posts,
        "source": "playwright",
        "error": None,
    }


# ── og:description follower parser ───────────────────────────────────────────

_FOLLOWERS_RE = re.compile(
    r"([\d,\.]+[KMBkmb]?)\s+Followers",
    re.IGNORECASE,
)
_FOLLOWING_RE = re.compile(
    r"([\d,\.]+[KMBkmb]?)\s+Following",
    re.IGNORECASE,
)
_POSTS_RE = re.compile(
    r"([\d,\.]+[KMBkmb]?)\s+Posts",
    re.IGNORECASE,
)


def _parse_ig_number(s: str) -> int | None:
    """Parse '500K', '1.2M', '12,345' → int."""
    s = s.replace(",", "").strip()
    multipliers = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
    if s and s[-1].lower() in multipliers:
        try:
            return int(float(s[:-1]) * multipliers[s[-1].lower()])
        except ValueError:
            return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _parse_followers_from_og_desc(og_desc_text: str) -> tuple[int | None, int | None, int | None]:
    """Extract (followers, following, posts) from Instagram og:description.

    Instagram og:description format:
    "500K Followers, 234 Following, 890 Posts – See Instagram photos..."
    """
    followers = following = posts = None
    m = _FOLLOWERS_RE.search(og_desc_text)
    if m:
        followers = _parse_ig_number(m.group(1))
    m = _FOLLOWING_RE.search(og_desc_text)
    if m:
        following = _parse_ig_number(m.group(1))
    m = _POSTS_RE.search(og_desc_text)
    if m:
        posts = _parse_ig_number(m.group(1))
    return followers, following, posts


# ── Scrapling DynamicFetcher fallback ────────────────────────────────────────


async def _fetch_via_scrapling(username: str) -> dict | None:
    """Stealth fallback using Scrapling DynamicFetcher (90s timeout).

    Extracts og:description meta which contains follower/following/posts counts.
    Use before vanilla Playwright — stealth headers bypass Instagram's bot checks.
    """
    profile_url = _PROFILE_URL.format(username=username)
    try:
        from scrapling.fetchers import DynamicFetcher  # type: ignore
        page = await DynamicFetcher.fetch(
            profile_url,
            headless=True,
            network_idle=True,
            timeout=90_000,
            wait=1_500,
        )
        if page is None:
            return None

        html: str = str(page.html) if hasattr(page, "html") else ""
        if not html or "login" in html.lower()[:500]:
            return None

        bio: str = ""
        profile_pic_url: str | None = None
        followers: int | None = None
        following: int | None = None
        posts_count: int | None = None

        og_desc_match = re.search(
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']',
            html, re.IGNORECASE,
        )
        if og_desc_match:
            og_desc_text = og_desc_match.group(1).strip()
            followers, following, posts_count = _parse_followers_from_og_desc(og_desc_text)
            # bio = full og:description — useful for brand context
            bio = og_desc_text

        og_img = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']*)["\']',
            html, re.IGNORECASE,
        )
        if og_img:
            profile_pic_url = og_img.group(1).strip() or None

        if not bio and not profile_pic_url:
            return None

        return {
            "username": username,
            "url": f"https://instagram.com/{username}",
            "bio": bio,
            "followers": followers,
            "following": following,
            "posts_count": posts_count,
            "is_verified": False,
            "is_business": False,
            "profile_pic_url": profile_pic_url,
            "recent_posts": [],
            "source": "scrapling",
            "error": None,
        }
    except Exception:
        return None


# ── In-process cache: avoids duplicate API calls within the same audit ────────

import time as _time
_PROFILE_CACHE: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 300  # 5 minutes — enough to cover Agent 7 → Agent 8 sequential run


# ── Public entry point ────────────────────────────────────────────────────────


async def scrape_instagram_profile(username: str) -> dict:
    """Scrape an Instagram public profile — no authentication required.

    Returns dict:
    {
        "username": str,
        "url": str,                 # https://instagram.com/{username}
        "bio": str,
        "followers": int | None,
        "following": int | None,
        "posts_count": int | None,
        "is_verified": bool,
        "is_business": bool,
        "profile_pic_url": str | None,
        "recent_posts": [
            {
                "url": str,             # https://instagram.com/p/{shortcode}
                "is_video": bool,
                "image_url": str|None,  # photos/carousels — use display_url
                "thumbnail_url": str|None,  # reels — use thumbnail_src
                "video_url": str|None,  # CDN video (time-limited; prefer reel_url + yt-dlp)
                "reel_url": str|None,   # https://instagram.com/reel/{shortcode}/
                "caption": str,
                "like_count": int | None,
                "comment_count": int | None,
                "timestamp": int | None,
                "hashtags": list[str]
            }
        ],
        "source": "instagram_api" | "playwright" | "scrapling" | "failed",
        "error": str | None
    }

    Never raises — all exceptions are caught and reported in the ``error`` field.
    """
    username = username.strip().lstrip("@")

    # ── Cache hit (prevents double-scraping within same audit run) ───────────
    cached = _PROFILE_CACHE.get(username)
    if cached:
        result, expires_at = cached
        if _time.monotonic() < expires_at:
            return result
        del _PROFILE_CACHE[username]

    # ── Attempt 1: mobile API ────────────────────────────────────────────────
    try:
        result = await _fetch_via_api(username)
        if result is not None:
            _PROFILE_CACHE[username] = (result, _time.monotonic() + _CACHE_TTL)
            return result
    except Exception:
        pass

    # ── Attempt 2: Scrapling DynamicFetcher (stealth, og: meta + follower count) ─
    try:
        result = await _fetch_via_scrapling(username)
        if result is not None:
            _PROFILE_CACHE[username] = (result, _time.monotonic() + _CACHE_TTL)
            return result
    except Exception:
        pass

    # ── Attempt 3: Playwright browser ────────────────────────────────────────
    try:
        result = await _fetch_via_playwright(username)
        if result is not None:
            _PROFILE_CACHE[username] = (result, _time.monotonic() + _CACHE_TTL)
            return result
    except Exception:
        pass

    # ── Total failure ────────────────────────────────────────────────────────
    failure = _empty_result(
        username=username,
        source="failed",
        error=(
            "Both the Instagram mobile API and the Playwright browser fallback "
            "failed to retrieve profile data. The profile may be private, "
            "the username may be incorrect, or Instagram is rate-limiting requests."
        ),
    )
    # Cache failures briefly to avoid hammering Instagram on retry
    _PROFILE_CACHE[username] = (failure, _time.monotonic() + 60)
    return failure
