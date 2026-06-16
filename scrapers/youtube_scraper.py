"""YouTube channel scraper using yt-dlp."""
from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from scrapers.search import SearchAgent

# ── Constants ────────────────────────────────────────────────────────────────

_YTDLP_TIMEOUT_SECS = 30
_YT_DOMAIN_RE = re.compile(
    r"https?://(?:www\.)?youtube\.com/(?:@[\w.-]+|channel/[\w-]+|c/[\w.-]+|user/[\w.-]+)",
    re.IGNORECASE,
)

# ── Duration formatting ───────────────────────────────────────────────────────


def _format_duration(seconds: int | float | None) -> str:
    """Convert a duration in seconds to a human-readable ``MM:SS`` or ``H:MM:SS`` string."""
    if seconds is None:
        return ""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ── Upload-date formatting ────────────────────────────────────────────────────


def _format_upload_date(raw: str | None) -> str:
    """Normalise yt-dlp's ``upload_date`` (``YYYYMMDD``) to ``YYYY-MM-DD``."""
    if not raw:
        return ""
    raw = raw.strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


# ── yt-dlp subprocess helper ──────────────────────────────────────────────────


async def _run_ytdlp(args: list[str], timeout: int = _YTDLP_TIMEOUT_SECS) -> tuple[bytes, bytes, int]:
    """Run yt-dlp as an async subprocess and return ``(stdout, stderr, returncode)``.

    Raises ``asyncio.TimeoutError`` if the process exceeds *timeout* seconds.
    """
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "yt_dlp",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        process.communicate(),
        timeout=float(timeout),
    )
    return stdout, stderr, process.returncode or 0


# ── Empty / error result builders ────────────────────────────────────────────


def _not_found_result(brand_name: str, error: str | None = None) -> dict:
    return {
        "channel_name": "",
        "channel_url": "",
        "channel_handle": "",
        "description": "",
        "subscribers": None,
        "total_videos": None,
        "recent_videos": [],
        "avg_views": None,
        "top_video": None,
        "source": "yt_dlp",
        "status": "not_found",
        "error": error,
    }


def _error_result(brand_name: str, error: str) -> dict:
    return {
        "channel_name": "",
        "channel_url": "",
        "channel_handle": "",
        "description": "",
        "subscribers": None,
        "total_videos": None,
        "recent_videos": [],
        "avg_views": None,
        "top_video": None,
        "source": "yt_dlp",
        "status": "error",
        "error": error,
    }


# ── Homepage social link extraction ──────────────────────────────────────────

_YT_LINK_RE = re.compile(
    r"https?://(?:www\.)?youtube\.com/(?:@[\w.-]+|channel/[\w-]+|c/[\w.-]+|user/[\w.-]+)",
    re.IGNORECASE,
)


async def _extract_yt_from_homepage(brand_url: str) -> str | None:
    """Fetch brand homepage and extract the first YouTube channel link.

    Most brands embed their social media links (incl. YouTube) in the site
    header/footer. This is the most reliable signal — it's from the brand itself.
    """
    try:
        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; bot/1.0)"},
        ) as client:
            r = await client.get(brand_url)
        if r.status_code != 200:
            return None
        # Find all YouTube channel URLs in the HTML
        matches = _YT_LINK_RE.findall(r.text)
        # Prefer @handle or /c/ links over /channel/UC... (opaque IDs)
        for m in matches:
            if "/@" in m or "/c/" in m or "/user/" in m:
                return m
        return matches[0] if matches else None
    except Exception:
        return None


# ── Channel name verification ─────────────────────────────────────────────────


def _channel_name_score(brand_name: str, channel_name: str) -> int:
    """Score how well channel_name matches brand_name (higher = better).

    3 — exact match (case-insensitive)
    2 — brand name is contained in channel name with minimal extra text
    1 — channel name contains all significant brand words
    0 — no match
    """
    if not channel_name:
        return 0
    brand_lower = brand_name.lower().strip()
    channel_lower = channel_name.lower().strip()
    if brand_lower == channel_lower:
        return 3
    # Channel adds minor suffix/prefix (≤8 extra chars): "Lenskart" vs "Lenskart bd"
    if brand_lower in channel_lower and len(channel_lower) - len(brand_lower) <= 8:
        return 2
    words = [w for w in re.split(r"\W+", brand_lower) if len(w) >= 3]
    if words and all(w in channel_lower for w in words):
        return 1
    return 0


def _channel_name_matches(brand_name: str, channel_name: str) -> bool:
    return _channel_name_score(brand_name, channel_name) > 0


# ── Channel discovery ─────────────────────────────────────────────────────────


async def discover_youtube_channel(
    brand_name: str,
    search_agent: "SearchAgent | None" = None,
    brand_url: str | None = None,
) -> str | None:
    """Find the YouTube channel URL for a brand.

    Strategy (in order):
      0. Brand homepage scan — extract YouTube link from header/footer social links.
      1. DDG search with quoted brand name — verify result title contains brand.
      2. yt-dlp ytsearch3 — verify channel_name matches brand before accepting.

    Returns the channel URL string, or ``None`` when nothing is found.
    """
    # ── Attempt 0: Brand homepage social links (most reliable) ──────────────
    if brand_url:
        yt_url = await _extract_yt_from_homepage(brand_url)
        if yt_url:
            return yt_url

    # Scored candidates: list of (score, url) — collect all, return best
    candidates: list[tuple[int, str]] = []

    def _best_candidate() -> str | None:
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1] if candidates[0][0] > 0 else None

    # ── Attempt 1: DuckDuckGo search with quoted brand name ─────────────────
    if search_agent is not None:
        try:
            results = search_agent.search(
                f'"{brand_name}" official youtube channel',
                max_results=10,
            )
            for result in results:
                url: str = result.get("url", "")
                title: str = result.get("title", "")
                if _YT_DOMAIN_RE.match(url):
                    score = _channel_name_score(brand_name, title)
                    if score > 0:
                        candidates.append((score, url))
            # If we have an exact-match candidate (score=3), return immediately
            if any(s == 3 for s, _ in candidates):
                return _best_candidate()
            # Otherwise extract from snippets too
            for result in results:
                combined = f"{result.get('title', '')} {result.get('snippet', '')}"
                m = _YT_DOMAIN_RE.search(combined)
                if m:
                    score = _channel_name_score(brand_name, result.get("title", ""))
                    if score > 0:
                        candidates.append((score, m.group(0)))
        except Exception:
            pass

    # ── Attempt 2: yt-dlp search — scored by channel name match ─────────────
    try:
        query = f"ytsearch5:{brand_name} official india brand"
        stdout, _stderr, rc = await _run_ytdlp(
            ["--dump-single-json", "--no-warnings", "--flat-playlist", query],
            timeout=_YTDLP_TIMEOUT_SECS,
        )
        if rc == 0 and stdout.strip():
            data = json.loads(stdout)
            for entry in data.get("entries", []):
                # Prefer @handle URL (canonical) over /channel/UC... (opaque ID)
                uploader_url: str = entry.get("uploader_url", "") or ""
                channel_url_entry: str = entry.get("channel_url", "") or ""
                ch_url = (
                    uploader_url if (uploader_url and "/@" in uploader_url)
                    else (channel_url_entry or uploader_url)
                )
                # Use uploader (who actually uploaded) not channel (brand mentioned)
                ch_name: str = entry.get("uploader", "") or entry.get("channel", "")
                if ch_url and "youtube.com" in ch_url and (
                    "/channel/" in ch_url or "/@" in ch_url
                ):
                    score = _channel_name_score(brand_name, ch_name)
                    if score > 0:
                        candidates.append((score, ch_url))
    except Exception:
        pass

    best = _best_candidate()
    if best:
        return best

    # ── Last resort: return first DDG URL match even without name verification ─
    if search_agent is not None:
        try:
            results = search_agent.search(
                f'"{brand_name}" official youtube channel',
                max_results=5,
            )
            for result in results:
                url = result.get("url", "")
                if _YT_DOMAIN_RE.match(url):
                    return url
        except Exception:
            pass

    return None


# ── Channel data fetch ────────────────────────────────────────────────────────


_TAB_TITLE_RE = re.compile(
    r"^.+ - (?:Videos|Shorts|Live|Playlists|Community|Channels|About|Podcasts)$",
    re.IGNORECASE,
)


def _is_tab_entry(entry: dict) -> bool:
    """Return True if the entry is a channel tab (Videos/Shorts/Live), not a real video."""
    title = entry.get("title", "")
    # Channel tabs have no view_count and no duration
    has_stats = entry.get("view_count") is not None or entry.get("duration") is not None
    return bool(_TAB_TITLE_RE.match(title)) and not has_stats


async def _ytdlp_fetch(url: str, playlist_items: str = "1-12") -> dict | None:
    """Run yt-dlp --dump-single-json --flat-playlist on url, return parsed JSON or None."""
    try:
        stdout, _stderr, rc = await _run_ytdlp(
            [
                "--dump-single-json",
                "--flat-playlist",
                "--playlist-items", playlist_items,
                "--no-warnings",
                url,
            ],
            timeout=_YTDLP_TIMEOUT_SECS,
        )
    except (asyncio.TimeoutError, Exception):
        return None
    if not stdout.strip():
        return None
    try:
        data = json.loads(stdout)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _parse_channel_meta(data: dict, channel_url: str) -> dict:
    channel_name: str = (
        data.get("channel", "") or data.get("uploader", "") or data.get("title", "")
    )
    resolved_url: str = (
        data.get("channel_url", "") or data.get("uploader_url", "") or channel_url
    )
    uploader_id: str = data.get("uploader_id", "") or ""
    channel_id: str = data.get("channel_id", "") or ""
    raw_id: str = uploader_id or channel_id
    if raw_id.startswith("@"):
        channel_handle: str | None = raw_id
    elif raw_id:
        channel_handle = f"@{raw_id.lstrip('@')}"
    else:
        channel_handle = None
    return {
        "channel_name": channel_name,
        "channel_url": resolved_url,
        "channel_handle": channel_handle,
        "description": (data.get("description", "") or "")[:1000],
        "subscribers": data.get("channel_follower_count") or data.get("subscriber_count"),
        "total_videos": data.get("playlist_count"),
    }


def _parse_video_entries(entries: list[dict]) -> tuple[list[dict], list[int]]:
    recent_videos: list[dict] = []
    view_counts: list[int] = []
    for entry in entries[:12]:
        if not isinstance(entry, dict) or _is_tab_entry(entry):
            continue
        view_count: int | None = entry.get("view_count")
        if isinstance(view_count, int):
            view_counts.append(view_count)
        video_url: str = entry.get("url", "") or entry.get("webpage_url", "")
        if video_url and not video_url.startswith("http"):
            video_url = f"https://www.youtube.com/watch?v={video_url}"
        thumbnails: list[dict] = entry.get("thumbnails", [])
        thumbnail: str = thumbnails[-1].get("url", "") if thumbnails else entry.get("thumbnail", "")
        recent_videos.append({
            "title": entry.get("title", ""),
            "url": video_url,
            "view_count": view_count,
            "duration": _format_duration(entry.get("duration")),
            "thumbnail": thumbnail,
            "upload_date": _format_upload_date(entry.get("upload_date")),
        })
    return recent_videos, view_counts


async def fetch_channel_data(channel_url: str) -> dict | None:
    """Fetch channel metadata and up to 12 recent videos via yt-dlp.

    Fetches `channel_url/videos` (actual video playlist) instead of the channel
    root, which would return tab-level playlist entries (Videos/Shorts/Live) with
    no view counts or durations.
    """
    videos_url = channel_url.rstrip("/") + "/videos"

    # ── Primary: /videos playlist — real entries with view_count + duration ──
    data = await _ytdlp_fetch(videos_url)

    # ── Fallback: channel root (still useful for metadata, filter out tabs) ──
    if data is None:
        data = await _ytdlp_fetch(channel_url)
    if data is None:
        return None

    meta = _parse_channel_meta(data, channel_url)
    entries: list[dict] = data.get("entries", [])

    # If we ended up with only tab entries (got channel root not /videos),
    # try fetching /videos explicitly before giving up on video metadata.
    real_entries = [e for e in entries if isinstance(e, dict) and not _is_tab_entry(e)]
    if not real_entries and entries:
        fallback_data = await _ytdlp_fetch(videos_url)
        if fallback_data:
            real_entries = [
                e for e in fallback_data.get("entries", [])
                if isinstance(e, dict) and not _is_tab_entry(e)
            ]

    recent_videos, view_counts = _parse_video_entries(real_entries)

    avg_views: int | None = (
        int(sum(view_counts) / len(view_counts)) if view_counts else None
    )
    top_video: dict | None = (
        max(recent_videos, key=lambda v: v.get("view_count") or 0)
        if recent_videos else None
    )

    return {
        **meta,
        "recent_videos": recent_videos,
        "avg_views": avg_views,
        "top_video": top_video,
        "source": "yt_dlp",
        "status": "found",
        "error": None,
    }


# ── Public entry point ────────────────────────────────────────────────────────


async def scrape_youtube_channel(
    brand_name: str,
    search_agent: "SearchAgent | None" = None,
    brand_url: str | None = None,
) -> dict:
    """Scrape a YouTube channel for a given brand.

    Returns dict:
    {
        "channel_name": str,
        "channel_url": str,
        "channel_handle": str | None,   # @brandname, or None when not determinable
        "description": str,
        "subscribers": int | None,
        "total_videos": int | None,
        "recent_videos": [
            {
                "title": str,
                "url": str,
                "view_count": int | None,
                "duration": str,        # "3:42"
                "thumbnail": str,
                "upload_date": str      # "2024-01-15"
            }
        ],
        "avg_views": int | None,
        "top_video": dict | None,
        "source": "yt_dlp",
        "status": "found" | "not_found" | "error",
        "error": str | None
    }

    Never raises — all exceptions are caught and reported in ``error`` / ``status``.
    """
    # ── Step 1: Discover the channel URL ────────────────────────────────────
    channel_url: str | None = None
    try:
        channel_url = await discover_youtube_channel(brand_name, search_agent, brand_url)
    except Exception as exc:
        return _error_result(brand_name, f"Channel discovery failed: {exc}")

    if not channel_url:
        return _not_found_result(
            brand_name,
            error=f"No YouTube channel found for '{brand_name}'.",
        )

    # ── Step 2: Fetch channel data ───────────────────────────────────────────
    try:
        result = await fetch_channel_data(channel_url)
    except asyncio.TimeoutError:
        return _error_result(
            brand_name,
            f"yt-dlp timed out after {_YTDLP_TIMEOUT_SECS}s fetching {channel_url}",
        )
    except Exception as exc:
        return _error_result(brand_name, f"yt-dlp fetch error: {exc}")

    if result is None:
        return _not_found_result(
            brand_name,
            error=f"yt-dlp returned no data for channel URL: {channel_url}",
        )

    return result
