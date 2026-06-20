"""Agent 8: Social Media Content Audit — multi-platform deep content analysis.

Workflow:
  1. Handle discovery  — Instagram, YouTube, Twitter (via DDG + scrapers)
  2. Content scraping  — real posts, captions, thumbnails per platform
  3. Image collection  — up to 8 representative images downloaded
  4. Multimodal AI     — Llama 4 Scout batch visual analysis + LLM text analysis
  5. Reels TRIBE v2   — download up to 3 Reels, run Meta TRIBE v2 fMRI predictions,
                         generate per-Reel brain activation heatmaps + brand aggregate
  6. Scoring           — 5 sub-dimensions → overall social health score
  7. Report            — wins, gaps, recommendations with evidence

Edge over single-prompt LLMs: real-time scraped data, actual post images,
quantitative metrics, TRIBE v2 fMRI neural engagement, multi-platform cross-analysis.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import TYPE_CHECKING

import httpx
import numpy as np

from scrapers.instagram_scraper import scrape_instagram_profile
from scrapers.youtube_scraper import scrape_youtube_channel
from scrapers.instagram_handle_finder import discover_handle
from scrapers.ig_handle_cache import get_cached_handle, store_handle

if TYPE_CHECKING:
    from llm.client import GroqClient
    from scrapers.search import SearchAgent as SearchAgentT

_MAX_REELS_TRIBE = 3  # Max Reels to run through TRIBE v2 per audit

# ── Image download headers ─────────────────────────────────────────────────────
_IMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.instagram.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── LLM prompts ────────────────────────────────────────────────────────────────
_VISUAL_BATCH_PROMPT = """\
You are a brand content strategist with expertise in visual marketing analysis.
Analyze these social media post images from a brand and output ONLY valid JSON:
{
  "style_consistency": 0,
  "aesthetic_quality": 0,
  "content_type_mix": {
    "product": 0.0,
    "lifestyle": 0.0,
    "promotional": 0.0,
    "educational": 0.0,
    "ugc": 0.0
  },
  "dominant_mood": "string",
  "color_palette_notes": "string",
  "visual_storytelling": "string",
  "production_quality": "string",
  "brand_consistency_notes": "string"
}
style_consistency: 0-10 (how uniform the visual style is across posts).
aesthetic_quality: 0-10 (production value and visual appeal).
content_type_mix: fractions summing to 1.0.
dominant_mood: single descriptor (e.g. "aspirational", "playful", "clinical", "warm").
color_palette_notes: 1 sentence about dominant colors.
visual_storytelling: 1-2 sentences about how the brand tells its story visually.
production_quality: "high" | "medium" | "low".
brand_consistency_notes: 1 sentence.
"""

_TEXT_ANALYSIS_PROMPT = """\
You are a brand content strategist. Analyze these social media captions/posts
from a brand and output ONLY valid JSON:
{
  "brand_voice": "string",
  "tone": "string",
  "hashtag_strategy": "string",
  "cta_usage": "string",
  "content_pillars": ["string"],
  "posting_patterns": "string",
  "engagement_quality": "string",
  "key_themes": ["string"],
  "voice_score": 0,
  "strategy_score": 0
}
brand_voice: 1 sentence describing the overall communication style.
tone: comma-separated descriptors (e.g. "professional, aspirational, occasionally humorous").
hashtag_strategy: brief assessment of hashtag use.
cta_usage: "strong" | "moderate" | "weak" | "none".
content_pillars: list of 2-4 recurring content themes.
posting_patterns: observations about timing/frequency consistency.
engagement_quality: assessment of comment/reply quality.
key_themes: list of 3-5 dominant topics in the content.
voice_score: 0-10 consistency and quality of brand voice.
strategy_score: 0-10 evidence of deliberate content strategy.
"""

_SYNTHESIS_PROMPT = """\
You are a senior social media strategist. Given the data below about a brand's
social media presence, provide a structured assessment.
Output ONLY valid JSON:
{
  "overall_assessment": "string",
  "top_3_strengths": ["string"],
  "top_3_gaps": ["string"],
  "top_3_recommendations": ["string"],
  "competitive_edge": "string",
  "urgency_areas": ["string"]
}
Keep each list item to 1 concise sentence with specific evidence from the data.
overall_assessment: 2-3 sentence executive summary.
competitive_edge: 1 sentence on what this brand does better than typical competitors.
urgency_areas: 1-2 immediate improvements needed.
"""


async def _download_image_b64(url: str, client: httpx.AsyncClient) -> str | None:
    """Download image and return base64-encoded string for Groq vision API."""
    try:
        r = await client.get(url, headers=_IMG_HEADERS, timeout=15, follow_redirects=True)
        if r.status_code == 200 and len(r.content) > 512:
            # Resize hint: Groq accepts up to 4MB per image
            return base64.b64encode(r.content).decode()
    except Exception:
        pass
    return None


async def _collect_images(posts: list[dict], max_images: int = 8) -> list[dict]:
    """Download up to max_images post thumbnails and return with base64 data."""
    urls = [p.get("thumbnail") or p.get("url") for p in posts if p.get("thumbnail")]
    urls = [u for u in urls if u][:max_images]
    if not urls:
        return []

    results = []
    async with httpx.AsyncClient() as client:
        tasks = [_download_image_b64(u, client) for u in urls]
        b64s = await asyncio.gather(*tasks)

    for url, b64, post in zip(urls, b64s, posts):
        if b64:
            results.append({
                "url": url,
                "b64": b64,
                "caption": post.get("caption", ""),
                "hashtags": post.get("hashtags", []),
                "like_count": post.get("like_count"),
                "post_url": post.get("url", ""),
            })
    return results


async def _run_visual_analysis(images: list[dict], llm: "GroqClient") -> dict:
    """Batch visual analysis using Llama 4 Scout vision."""
    if not images:
        return {
            "style_consistency": 0, "aesthetic_quality": 0,
            "content_type_mix": {"product": 1.0, "lifestyle": 0, "promotional": 0, "educational": 0, "ugc": 0},
            "dominant_mood": "unknown", "color_palette_notes": "No images available",
            "visual_storytelling": "", "production_quality": "unknown",
            "brand_consistency_notes": "No images to analyze",
            "_no_images": True,
        }

    # Build multimodal message content
    content_parts: list[dict] = []
    for i, img in enumerate(images[:6]):  # Groq limit: up to 5 images per call
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img['b64']}"},
        })
        if img.get("caption"):
            content_parts.append({
                "type": "text",
                "text": f"Post {i+1} caption: {img['caption'][:120]}",
            })

    content_parts.append({"type": "text", "text": _VISUAL_BATCH_PROMPT})

    try:
        images_b64 = [img["b64"] for img in images[:6] if img.get("b64")]
        raw_text = await llm.analyze_image_batch(
            system_prompt="You are a brand visual analyst. Return ONLY valid JSON.",
            images_b64=images_b64,
            text_prompt=_VISUAL_BATCH_PROMPT,
            max_tokens=800,
        )
        import re as _re, json as _json
        clean = _re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=_re.MULTILINE).strip()
        parsed = _json.loads(clean or "{}")
        if parsed and "_parse_error" not in parsed:
            return parsed
    except Exception as _exc:
        print(f"[social-audit] vision batch failed: {_exc}", flush=True)

    # Fallback: text-only analysis
    captions_text = "\n".join(f"- {img['caption'][:100]}" for img in images if img.get("caption"))
    try:
        raw = await llm.analyze_structured(
            system_prompt="You are a brand visual analyst. Return ONLY valid JSON.",
            user_content=f"Based on these post captions, estimate visual style:\n{captions_text}\n\n{_VISUAL_BATCH_PROMPT}",
            max_tokens=600,
        )
        if "_parse_error" not in raw:
            return raw
    except Exception:
        pass

    return {
        "style_consistency": 5, "aesthetic_quality": 5,
        "content_type_mix": {"product": 0.5, "lifestyle": 0.3, "promotional": 0.2, "educational": 0, "ugc": 0},
        "dominant_mood": "neutral", "color_palette_notes": "Analysis unavailable",
        "visual_storytelling": "", "production_quality": "medium",
        "brand_consistency_notes": "Visual analysis failed",
    }


async def _run_text_analysis(
    ig_posts: list[dict],
    yt_videos: list[dict],
    llm: "GroqClient",
) -> dict:
    """Analyze captions, hashtags, and video titles for brand voice."""
    lines = []
    for p in ig_posts[:10]:
        if p.get("caption"):
            lines.append(f"[IG] {p['caption'][:140]}")
    for v in yt_videos[:8]:
        if v.get("title"):
            lines.append(f"[YT] {v['title']}")

    if not lines:
        return {
            "brand_voice": "Insufficient data", "tone": "unknown",
            "hashtag_strategy": "unknown", "cta_usage": "unknown",
            "content_pillars": [], "posting_patterns": "No data",
            "engagement_quality": "unknown", "key_themes": [],
            "voice_score": 0, "strategy_score": 0,
        }

    text_block = "\n".join(lines)
    all_hashtags: list[str] = []
    for p in ig_posts:
        all_hashtags.extend(p.get("hashtags", []))
    top_tags = [f"#{t}" for t, _ in __import__("collections").Counter(all_hashtags).most_common(10)]
    text_block += f"\n\nTop hashtags: {', '.join(top_tags)}"

    try:
        raw = await llm.analyze_structured(
            system_prompt="Brand content strategist. Return ONLY valid JSON.",
            user_content=f"Social media content to analyze:\n{text_block}\n\n{_TEXT_ANALYSIS_PROMPT}",
            max_tokens=700,
        )
        if "_parse_error" not in raw:
            return raw
    except Exception:
        pass

    return {
        "brand_voice": "Analysis unavailable", "tone": "unknown",
        "hashtag_strategy": "unknown", "cta_usage": "unknown",
        "content_pillars": [], "posting_patterns": "N/A",
        "engagement_quality": "unknown", "key_themes": [],
        "voice_score": 5, "strategy_score": 5,
    }


async def _synthesize(
    platforms: dict,
    visual: dict,
    text: dict,
    scores: dict,
    llm: "GroqClient",
) -> dict:
    """Generate overall assessment, wins, gaps, and recommendations."""
    ig = platforms.get("instagram", {})
    yt = platforms.get("youtube", {})
    summary = (
        f"Brand: {ig.get('username', 'unknown')}\n"
        f"Instagram: {ig.get('followers') or 0:,} followers, {ig.get('posts_count') or 0} posts\n"
        f"YouTube: {yt.get('subscribers') or 0:,} subscribers, {yt.get('total_videos') or 0} videos\n"
        f"Visual style consistency: {visual.get('style_consistency', 0)}/10\n"
        f"Aesthetic quality: {visual.get('aesthetic_quality', 0)}/10\n"
        f"Content type mix: {json.dumps(visual.get('content_type_mix', {}))}\n"
        f"Brand voice: {text.get('brand_voice', '')}\n"
        f"Hashtag strategy: {text.get('hashtag_strategy', '')}\n"
        f"Top content themes: {text.get('key_themes', [])}\n"
        f"Platform presence score: {scores.get('platform_presence', 0)}/10\n"
        f"Content quality score: {scores.get('content_quality', 0)}/10\n"
        f"Engagement score: {scores.get('engagement', 0)}/10\n"
    )
    try:
        raw = await llm.analyze_structured(
            system_prompt="Senior social media strategist. Return ONLY valid JSON.",
            user_content=f"{summary}\n\n{_SYNTHESIS_PROMPT}",
            max_tokens=700,
        )
        if "_parse_error" not in raw:
            return raw
    except Exception:
        pass
    return {
        "overall_assessment": "Analysis unavailable.",
        "top_3_strengths": [],
        "top_3_gaps": [],
        "top_3_recommendations": [],
        "competitive_edge": "",
        "urgency_areas": [],
    }


def _compute_scores(
    ig: dict,
    yt: dict,
    visual: dict,
    text: dict,
) -> dict:
    """Compute 5 sub-scores and an overall score."""
    # Platform presence (0-10): covers Instagram + YouTube
    pp = 0.0
    if ig.get("followers"):
        f = ig["followers"]
        if f > 500_000:    pp += 4.5
        elif f > 100_000:  pp += 3.5
        elif f > 10_000:   pp += 2.5
        elif f > 1_000:    pp += 1.5
        else:              pp += 0.5
    if yt.get("subscribers"):
        s = yt["subscribers"]
        if s > 100_000:  pp += 3.5
        elif s > 10_000: pp += 2.5
        elif s > 1_000:  pp += 1.5
        else:            pp += 0.5
    pp = min(10.0, pp)

    # Content quality (0-10)
    cq = (
        (visual.get("style_consistency", 5) * 0.4) +
        (visual.get("aesthetic_quality", 5) * 0.4) +
        ({"high": 10, "medium": 6, "low": 3}.get(visual.get("production_quality", "medium"), 6) * 0.2)
    )

    # Engagement (0-10): inferred from IG posts
    ig_posts = ig.get("recent_posts", [])
    ig_followers = ig.get("followers") or 1
    if ig_posts:
        likes = [p.get("like_count") or 0 for p in ig_posts if p.get("like_count")]
        avg_likes = sum(likes) / len(likes) if likes else 0
        er = avg_likes / ig_followers * 100
        if er > 5:       eng = 9.0
        elif er > 3:     eng = 7.5
        elif er > 1:     eng = 6.0
        elif er > 0.5:   eng = 4.5
        else:            eng = 2.5 if avg_likes > 0 else 0.0
    else:
        eng = 0.0

    # Content strategy (0-10)
    cs = (text.get("voice_score", 5) * 0.5) + (text.get("strategy_score", 5) * 0.5)

    # Brand consistency (0-10)
    bc = visual.get("style_consistency", 5)

    overall = round(pp * 0.25 + cq * 0.25 + eng * 0.20 + cs * 0.15 + bc * 0.15, 1)
    return {
        "platform_presence":   round(pp, 1),
        "content_quality":     round(cq, 1),
        "engagement":          round(eng, 1),
        "content_strategy":    round(cs, 1),
        "brand_consistency":   round(bc, 1),
        "overall":             overall,
    }


# ── Reels TRIBE v2 neural engagement pipeline ─────────────────────────────────

async def _process_reels_tribe(posts: list[dict]) -> dict:
    """Download up to _MAX_REELS_TRIBE Reels and run Meta TRIBE v2 on each.

    Returns:
      {
        "reels_tribe": [ {reel_url, caption, thumbnail, like_count,
                          video_view_count, neural_engagement, network_scores,
                          brain_map_svg, error?} ],
        "brand_brain_map": { network_scores, brain_map_svg, reels_analyzed } | None,
        "tribe_available": bool,
        "tribe_error": str | None,
      }
    """
    from agents.neural_engagement import NeuralEngagementAnalyzer  # lazy import — heavy
    from agents.brain_map import (
        tribe_preds_to_network_scores,
        generate_activation_heatmap,
        generate_anatomical_brain_svg,
    )

    empty = {"reels_tribe": [], "brand_brain_map": None, "tribe_available": False, "tribe_error": None}

    # Filter to video posts that have a downloadable URL
    reels = [
        p for p in posts
        if p.get("is_video") and (p.get("reel_url") or p.get("url"))
    ][:_MAX_REELS_TRIBE]

    if not reels:
        empty["tribe_error"] = "No Reels found in recent posts"
        return empty

    analyzer = NeuralEngagementAnalyzer()
    reel_results: list[dict] = []
    all_preds: list[np.ndarray] = []

    for reel in reels:
        video_url = reel.get("reel_url") or reel.get("url")
        caption_short = (reel.get("caption") or "")[:35]
        print(f"  [tribe_reels] Processing: {video_url}", flush=True)

        try:
            loop = asyncio.get_event_loop()
            score_dict, preds, reel_video_path, sim_video_path = await asyncio.wait_for(
                loop.run_in_executor(None, analyzer._run_sync_full, video_url),
                timeout=600.0,  # 10 min per Reel (model load + inference)
            )

            network_scores: dict = {}
            brain_map_svg: str | None = None
            anatomical_svg: str | None = None
            if preds is not None and preds.shape[0] > 0:
                network_scores = tribe_preds_to_network_scores(preds)
                brain_map_svg = generate_activation_heatmap(
                    network_scores,
                    is_real_tribe=True,
                    ad_label=caption_short or "Instagram Reel",
                )
                anatomical_svg = generate_anatomical_brain_svg(
                    network_scores,
                    is_real_tribe=True,
                    ad_label=caption_short or "Instagram Reel",
                )
                all_preds.append(preds)
                print(
                    f"  [tribe_reels] Done — score={score_dict.get('neural_engagement_score')}, "
                    f"TRs={preds.shape[0]}, sim_video={'yes' if sim_video_path else 'no'}",
                    flush=True,
                )

            reel_results.append({
                "reel_url": video_url,
                "caption": (reel.get("caption") or "")[:200],
                "thumbnail": reel.get("thumbnail", ""),
                "like_count": reel.get("like_count"),
                "video_view_count": reel.get("video_view_count"),
                "neural_engagement": score_dict,
                "network_scores":   network_scores,
                "brain_map_svg":    brain_map_svg,
                "anatomical_svg":   anatomical_svg,
                # Video paths for the brain simulation player
                "reel_video_path":  reel_video_path,
                "sim_video_path":   sim_video_path,
                # Free local media analysis (FFmpeg + OpenCV + Pillow)
                "pacing":              score_dict.get("pacing"),
                "hook_frames_b64":     score_dict.get("hook_frames_b64", []),
                "color_palette_hex":   score_dict.get("color_palette_hex", []),
                "color_palette_names": score_dict.get("color_palette_names", []),
                "duration_s":          score_dict.get("duration_s"),
            })

        except asyncio.TimeoutError:
            print(f"  [tribe_reels] Timeout for {video_url}", flush=True)
            reel_results.append({
                "reel_url": video_url,
                "caption": (reel.get("caption") or "")[:200],
                "thumbnail": reel.get("thumbnail", ""),
                "error": "TRIBE v2 timed out for this Reel (>10 min)",
            })
        except Exception as exc:
            print(f"  [tribe_reels] Error for {video_url}: {exc}", flush=True)
            reel_results.append({
                "reel_url": video_url,
                "caption": (reel.get("caption") or "")[:200],
                "thumbnail": reel.get("thumbnail", ""),
                "error": str(exc),
            })

    # Brand-aggregate brain map: concatenate all Reels' preds → single heatmap
    brand_brain_map = None
    if all_preds:
        combined = np.concatenate(all_preds, axis=0)
        avg_network_scores = tribe_preds_to_network_scores(combined)
        _agg_label = f"Brand Aggregate · {len(all_preds)} Reel{'s' if len(all_preds) > 1 else ''}"
        brand_svg = generate_activation_heatmap(
            avg_network_scores,
            is_real_tribe=True,
            ad_label=_agg_label,
        )
        brand_anatomical_svg = generate_anatomical_brain_svg(
            avg_network_scores,
            is_real_tribe=True,
            ad_label=_agg_label,
        )
        brand_brain_map = {
            "network_scores":  avg_network_scores,
            "brain_map_svg":   brand_svg,
            "anatomical_svg":  brand_anatomical_svg,
            "reels_analyzed":  len(all_preds),
        }
        print(f"  [tribe_reels] Brand aggregate brain map built from {len(all_preds)} Reels", flush=True)

    return {
        "reels_tribe": reel_results,
        "brand_brain_map": brand_brain_map,
        "tribe_available": bool(all_preds),
        "tribe_error": None,
    }


# ── Main agent class ───────────────────────────────────────────────────────────

class SocialMediaAuditAgent:
    """Agent 8: Deep multi-platform social media content audit."""

    def __init__(self, llm: "GroqClient", search: "SearchAgentT"):
        self.llm = llm
        self.search = search

    async def run(self, url: str, brand_name: str, deep_visual: bool = False) -> dict:
        """Orchestrator-compatible entry point — delegates to audit()."""
        return await self.audit(brand_name, website_url=url, deep_visual=deep_visual)

    async def audit(self, brand_name: str, website_url: str = "", deep_visual: bool = False) -> dict:
        """
        Run full social media audit for a brand.

        Returns structured dict suitable for report rendering.
        """
        print(f"  [social_audit] Starting audit for {brand_name}", flush=True)

        # ── Step 1: Discover Instagram handle (cache → multi-strategy) ───────
        _cached = get_cached_handle(website_url)
        if _cached:
            ig_handle, ig_handle_confidence = _cached
            print(f"  [social_audit] IG handle cache hit: @{ig_handle} ({ig_handle_confidence})", flush=True)
        else:
            ig_handle, ig_handle_confidence = await discover_handle(
                brand_name=brand_name,
                website_url=website_url,
                search_agent=self.search,
            )
            store_handle(website_url, ig_handle, ig_handle_confidence)
        print(f"  [social_audit] Instagram handle: {ig_handle} ({ig_handle_confidence})", flush=True)

        # ── Step 2: Scrape Instagram ───────────────────────────────────────────
        ig_data: dict = {}
        if ig_handle:
            try:
                ig_data = await scrape_instagram_profile(ig_handle)
                posts_found = len(ig_data.get("recent_posts", []))
                print(f"  [social_audit] IG: {ig_data.get('followers') or 0:,} followers, {posts_found} posts", flush=True)
            except Exception as e:
                ig_data = {"username": ig_handle, "error": str(e), "source": "failed", "recent_posts": []}

        # ── Step 3: Scrape YouTube ─────────────────────────────────────────────
        try:
            yt_data = await scrape_youtube_channel(brand_name, self.search, brand_url=website_url)
            if yt_data.get("status") == "found":
                print(f"  [social_audit] YT: {yt_data.get('subscribers') or 0:,} subscribers", flush=True)
            else:
                print(f"  [social_audit] YT: not found ({yt_data.get('error', '')})", flush=True)
        except Exception as e:
            yt_data = {"status": "error", "error": str(e), "recent_videos": []}

        # ── Step 4: Collect images for visual analysis ─────────────────────────
        all_posts = ig_data.get("recent_posts", [])
        images = await _collect_images(all_posts, max_images=8)
        print(f"  [social_audit] Downloaded {len(images)} post images", flush=True)

        # ── Step 5: Multimodal visual analysis (Llama 4 Scout) ─────────────────
        visual_analysis = await _run_visual_analysis(images, self.llm)
        print(f"  [social_audit] Visual analysis: consistency={visual_analysis.get('style_consistency')}", flush=True)

        # ── Step 6: Reels TRIBE v2 neural engagement ─────────────────────────────
        # TRIBE v2 is always deferred to a background task after the main audit
        # completes (see _run_tribe_background in main.py). deep_visual=True just
        # marks the result so the background runner knows to start processing.
        # This keeps the main audit at ~3 min regardless of Reel count.
        reels_for_tribe = [p for p in all_posts if p.get("is_video") and (p.get("reel_url") or p.get("url"))]

        # Fallback: use YouTube videos when Instagram Reels are unavailable.
        # youtube_scraper already fetched these; _process_reels_tribe accepts any
        # dict with is_video=True + a url — neural_engagement handles YouTube URLs natively.
        if not reels_for_tribe and yt_data.get("status") == "found":
            reels_for_tribe = [
                {
                    "is_video": True,
                    "reel_url": v.get("url"),
                    "url": v.get("url"),
                    "caption": v.get("title", ""),
                    "thumbnail": v.get("thumbnail", ""),
                    "like_count": None,
                    "video_view_count": v.get("view_count"),
                    "source": "youtube",
                }
                for v in yt_data.get("recent_videos", [])[:_MAX_REELS_TRIBE]
                if v.get("url")
            ]
            if reels_for_tribe:
                print(f"  [social_audit] No IG Reels — using {len(reels_for_tribe)} YouTube videos for TRIBE v2", flush=True)

        tribe_data = {
            "reels_tribe": [],
            "brand_brain_map": None,
            "tribe_available": False,
            "tribe_status": "pending" if (deep_visual and reels_for_tribe) else "none",
            "tribe_posts_snapshot": reels_for_tribe[:3] if deep_visual else [],
        }

        # ── Step 7: Text / brand voice analysis ───────────────────────────────
        text_analysis = await _run_text_analysis(
            ig_data.get("recent_posts", []),
            yt_data.get("recent_videos", []),
            self.llm,
        )
        print(f"  [social_audit] Text analysis: voice={text_analysis.get('brand_voice', '')[:40]}", flush=True)

        # ── Step 8: Scores ─────────────────────────────────────────────────────
        scores = _compute_scores(ig_data, yt_data, visual_analysis, text_analysis)
        print(f"  [social_audit] Overall social score: {scores['overall']}/10", flush=True)

        # ── Step 9: Synthesis ─────────────────────────────────────────────────
        synthesis = await _synthesize(
            {"instagram": ig_data, "youtube": yt_data},
            visual_analysis, text_analysis, scores, self.llm,
        )

        # ── Compile image gallery (strip base64, keep URLs + captions) ─────────
        image_gallery = [
            {
                "thumbnail": img["url"],
                "caption": img.get("caption", "")[:120],
                "like_count": img.get("like_count"),
                "post_url": img.get("post_url", ""),
                "hashtags": img.get("hashtags", [])[:5],
            }
            for img in images
        ]

        # Build data_gap_reason based on Instagram post availability
        ig_posts = ig_data.get("recent_posts", [])
        data_gap: str | None = None
        if not ig_posts:
            ig_source = ig_data.get("source", "unknown")
            ig_error = ig_data.get("error", "")
            if "rate" in str(ig_error).lower() or "429" in str(ig_error):
                data_gap = "Instagram API rate limited — post data unavailable. Engagement scores are estimated from follower count only."
            elif "403" in str(ig_error) or "blocked" in str(ig_error).lower():
                data_gap = "Instagram blocked the scrape request — post data unavailable. Try again in 1-2 hours."
            elif ig_source == "playwright":
                data_gap = "Instagram profile loaded via browser but posts couldn't be extracted (IG requires login to show posts to bots). Profile stats (followers, bio) are from public meta tags."
            else:
                data_gap = "No Instagram posts retrieved — TRIBE neural engagement analysis and engagement rate calculations are unavailable."

        return {
            "agent": "social_media_audit",
            "brand_name": brand_name,
            "platforms": {
                "instagram": {
                    "handle": ig_data.get("username"),
                    "url": ig_data.get("url"),
                    "followers": ig_data.get("followers"),
                    "following": ig_data.get("following"),
                    "posts_count": ig_data.get("posts_count"),
                    "bio": ig_data.get("bio"),
                    "is_verified": ig_data.get("is_verified", False),
                    "is_business": ig_data.get("is_business", False),
                    "profile_pic": ig_data.get("profile_pic_url"),
                    "recent_posts_count": len(ig_data.get("recent_posts", [])),
                    "avg_likes": _avg_likes(ig_data.get("recent_posts", [])),
                    "engagement_rate": _engagement_rate(ig_data),
                    "top_hashtags": _top_hashtags(ig_data.get("recent_posts", [])),
                    "status": "found" if ig_data.get("followers") else "not_found",
                    "source": ig_data.get("source", "failed"),
                    "source_url": ig_data.get("url"),
                },
                "youtube": {
                    "channel_name": yt_data.get("channel_name"),
                    "handle": yt_data.get("channel_handle"),
                    "url": yt_data.get("channel_url"),
                    "subscribers": yt_data.get("subscribers"),
                    "total_videos": yt_data.get("total_videos"),
                    "avg_views": yt_data.get("avg_views"),
                    "top_video": yt_data.get("top_video"),
                    "recent_videos": yt_data.get("recent_videos", [])[:5],
                    "status": yt_data.get("status", "not_found"),
                    "source_url": yt_data.get("channel_url"),
                },
            },
            "visual_analysis": visual_analysis,
            "text_analysis": text_analysis,
            "image_gallery": image_gallery,
            "reels_tribe": tribe_data.get("reels_tribe", []),
            "brand_brain_map": tribe_data.get("brand_brain_map"),
            "tribe_available": tribe_data.get("tribe_available", False),
            "tribe_status": tribe_data.get("tribe_status", "none"),
            "tribe_posts_snapshot": tribe_data.get("tribe_posts_snapshot", []),
            "scores": scores,
            "top_3_strengths": synthesis.get("top_3_strengths", []),
            "top_3_gaps": synthesis.get("top_3_gaps", []),
            "top_3_recommendations": synthesis.get("top_3_recommendations", []),
            "overall_assessment": synthesis.get("overall_assessment", ""),
            "competitive_edge": synthesis.get("competitive_edge", ""),
            "urgency_areas": synthesis.get("urgency_areas", []),
            "data_sources": [
                s for s in [
                    ig_data.get("url") if ig_data.get("followers") else None,
                    yt_data.get("channel_url") if yt_data.get("status") == "found" else None,
                ] if s
            ],
            "data_gap_reason": data_gap,
        }



# ── Helpers ────────────────────────────────────────────────────────────────────

def _avg_likes(posts: list[dict]) -> int | None:
    likes = [p.get("like_count") for p in posts if p.get("like_count")]
    return round(sum(likes) / len(likes)) if likes else None


def _engagement_rate(ig: dict) -> float | None:
    avg = _avg_likes(ig.get("recent_posts", []))
    followers = ig.get("followers")
    if avg and followers:
        return round(avg / followers * 100, 2)
    return None


def _top_hashtags(posts: list[dict], n: int = 10) -> list[str]:
    from collections import Counter
    all_tags: list[str] = []
    for p in posts:
        all_tags.extend(p.get("hashtags", []))
    return [f"#{t}" for t, _ in Counter(all_tags).most_common(n)]
