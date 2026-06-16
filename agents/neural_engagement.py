"""Neural engagement analysis using Meta TRIBE v2.

TRIBE v2 predicts fMRI cortical surface activations when a human watches a video.
Output: (n_timesteps, ~20k_vertices) on the fsaverage5 cortical mesh.
High mean absolute activation = content is strongly engaging attention, emotion,
and memory circuits across cortex.

Workflow:
  1. Find video content from a product/social URL (YouTube, Instagram, TikTok,
     or any direct video link)
  2. Download via yt-dlp (handles 1000+ platforms) or direct HTTP
  3. Run TribeModel.get_events_dataframe(video_path=...) → word/audio events
  4. Run TribeModel.predict(events_df) → (n_TRs, ~20k_vertices) activation matrix
  5. Derive Neural Engagement Score (0-100) from mean absolute cortical activation

Score calibration (z-score units, fsaverage5 cortical mesh):
  mean_abs < 0.08  → Low     (score < 20)
  0.08 – 0.20      → Medium  (score 20-50)
  0.20 – 0.40      → High    (score 50-100)

Weights: HuggingFace `facebook/tribev2` (auto-downloaded on first use, ~several GB).
License: CC-BY-NC-4.0 (non-commercial research use only).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Env var: local directory with config.yaml + best.ckpt, or HF repo id
_TRIBE_CHECKPOINT_DIR = os.getenv("TRIBE_CHECKPOINT_DIR", "facebook/tribev2")

# Video platform URL patterns that yt-dlp can handle directly
_VIDEO_PLATFORM_RE = re.compile(
    r"(?:youtube\.com/(?:watch|shorts|embed)|youtu\.be/"
    r"|instagram\.com/(?:reel|p|tv|stories)"
    r"|tiktok\.com/@[^/]+/video"
    r"|vimeo\.com/\d"
    r"|twitter\.com/.+/status"
    r"|x\.com/.+/status"
    r"|facebook\.com/.+/videos"
    r"|dailymotion\.com/video)",
    re.IGNORECASE,
)

# Direct video file URL pattern
_DIRECT_VIDEO_RE = re.compile(r"\.(?:mp4|mkv|webm|mov|avi|m4v)(?:[?#]|$)", re.IGNORECASE)

# Patterns to extract embedded video URLs from page descriptions / HTML
_EMBED_VIDEO_RE = re.compile(
    r"(?:src|href)=[\"']([^\"']*(?:youtube\.com/embed/|youtu\.be/|vimeo\.com/video/)[^\"']*)[\"']",
    re.IGNORECASE,
)

# Module-level lazy singleton — model load takes ~10-30s, should happen once
_tribe_model = None


def _is_video_platform_url(url: str) -> bool:
    return bool(_VIDEO_PLATFORM_RE.search(url))


def _is_direct_video_url(url: str) -> bool:
    return bool(_DIRECT_VIDEO_RE.search(url))


def _find_video_url_in_text(text: str) -> str | None:
    """Extract the first embedded video URL from page HTML / description."""
    m = _EMBED_VIDEO_RE.search(text or "")
    return m.group(1) if m else None


def _get_tribe_model():
    global _tribe_model
    if _tribe_model is not None:
        return _tribe_model

    # Add tribeV2 repo to sys.path if not installed as a package
    import sys
    tribe_repo = Path(__file__).parent.parent.parent / "tribeV2"
    if tribe_repo.exists() and str(tribe_repo) not in sys.path:
        sys.path.insert(0, str(tribe_repo))

    try:
        from tribev2 import TribeModel  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "tribev2 not installed. Clone the tribeV2 repo and add its path, "
            "or set TRIBE_CHECKPOINT_DIR to a local checkpoint directory."
        ) from exc

    import torch as _torch
    # neuralset extractors only accept 'cpu' or 'cuda' (not 'mps').
    # Use CUDA when available; fall back to CPU. The main TRIBE v2 encoder
    # can use MPS but the feature extractors cannot.
    _device = "cuda" if _torch.cuda.is_available() else "cpu"

    # Force all feature extractors to the correct device.
    # The checkpoint config.yaml was created for CUDA training; without this
    # override all extractors keep device=cuda and crash on CPU machines.
    # The video extractor uses a nested HuggingFaceImage which needs its own override.
    # num_workers=0: TRIBE v2 is always called from a run_in_executor thread; spawning
    # 20 DataLoader workers from a thread causes multiprocessing spawn failures on macOS.
    _device_update = {
        "data.text_feature.device": _device,
        "data.audio_feature.device": _device,
        "data.video_feature.image.device": _device,
        "data.image_feature.image.device": _device,
        "data.num_workers": 0,
        # Quality/speed balance: full tri-modal, V-JEPA at 16 frames + 336px resize.
        # Targets ~7-8 min on CPU vs 25 min default (64 frames, native res, 2Hz).
        "data.features_to_use": ["audio", "video", "text"],
        "data.video_feature.num_frames":    16,   # 64→16: 4x fewer frames/clip
        "data.video_feature.max_imsize":    336,  # resize before encoding (memory + speed)
        "data.video_feature.clip_duration":  2,   # 4s→2s clips
        "data.video_feature.frequency":      1,   # 2→1 Hz: 1 clip/TR, halves time points
    }

    logger.info("Loading TRIBE v2 from %s (device=%s) …", _TRIBE_CHECKPOINT_DIR, _device)
    _tribe_model = TribeModel.from_pretrained(
        checkpoint_dir=_TRIBE_CHECKPOINT_DIR,
        cache_folder=str(Path("./cache/tribe_features")),
        device=_device,
        config_update=_device_update,
    )
    return _tribe_model


def _download_media_sync(url: str, dest_dir: str) -> str | None:
    """Download video from url into dest_dir. Returns local file path or None."""
    try:
        import yt_dlp  # noqa: PLC0415

        ydl_opts = {
            "outtmpl": f"{dest_dir}/%(id)s.%(ext)s",
            "format": "best[ext=mp4]/best[height<=720]/best",
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if Path(filename).exists():
                return filename
            # yt-dlp sometimes changes the extension after download
            for f in Path(dest_dir).iterdir():
                if f.is_file() and f.stat().st_size > 1024:
                    return str(f)
    except Exception as exc:
        logger.warning("yt-dlp failed for %s: %s", url, exc)

    # Direct HTTP fallback for plain video URLs
    if _is_direct_video_url(url):
        try:
            import httpx  # noqa: PLC0415

            ext = re.search(r"\.(mp4|mkv|webm|mov|avi)", url, re.IGNORECASE)
            ext = ext.group(0) if ext else ".mp4"
            dest = Path(dest_dir) / f"video{ext}"
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with open(dest, "wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=128 * 1024):
                            f.write(chunk)
            if dest.stat().st_size > 1024:
                return str(dest)
        except Exception as exc:
            logger.warning("Direct HTTP download failed for %s: %s", url, exc)
    return None


def _persist_tribe_videos(
    media_path: str,
    preds: "np.ndarray",
    segments: list,
    video_url: str,
) -> "tuple[str | None, str | None]":
    """Copy reel video to persistent cache and render a brain simulation MP4.

    Returns (reel_video_path, sim_video_path). Either may be None on failure.
    Outputs go to ./cache/tribe_videos/<url_hash>_{reel,brain}.mp4.
    """
    import hashlib
    import tempfile as _tmp
    import concurrent.futures
    import matplotlib.image as mpimg
    import matplotlib.pyplot as plt

    url_hash = hashlib.md5(video_url.encode()).hexdigest()[:12]
    out_dir = Path("./cache/tribe_videos")
    out_dir.mkdir(parents=True, exist_ok=True)

    reel_dst = out_dir / f"{url_hash}_reel.mp4"
    sim_dst  = out_dir / f"{url_hash}_brain.mp4"

    # Skip if already rendered (idempotent)
    if reel_dst.exists() and sim_dst.exists():
        return str(reel_dst), str(sim_dst)

    # ── Copy reel to persistent location ──────────────────────────────────────
    reel_video_path: str | None = None
    try:
        import shutil as _sh
        _sh.copy2(media_path, reel_dst)
        reel_video_path = str(reel_dst)
        logger.info("[tribe_video] Reel saved: %s", reel_dst)
    except Exception as exc:
        logger.warning("[tribe_video] Could not copy reel: %s", exc)

    # ── Generate brain simulation video ───────────────────────────────────────
    sim_video_path: str | None = None
    try:
        import sys as _sys
        tribe_repo = Path(__file__).parent.parent.parent / "tribeV2"
        if tribe_repo.exists() and str(tribe_repo) not in _sys.path:
            _sys.path.insert(0, str(tribe_repo))
        from tribev2.plotting import PlotBrain
        from tribev2.plotting.utils import get_cmap, get_scalar_mappable, robust_normalize, tight_crop
        from tribev2.plotting.cortical_pv import VIEW_DICT as _PV_VIEWS
        import pyvista as pv

        BRAIN_DPI  = int(os.getenv("TRIBE_SIM_DPI",  "300"))
        BRAIN_MESH = os.getenv("TRIBE_SIM_MESH", "fsaverage3")
        BRAIN_VIEW = os.getenv("TRIBE_SIM_VIEW", "left")
        HEMO_LAG   = 5.0
        COMP_W, COMP_H = 960, 480
        FLOW_DPI   = 100
        N_WORKERS  = 2

        plotter   = PlotBrain(mesh=BRAIN_MESH, dpi=BRAIN_DPI)
        n_trs     = preds.shape[0]
        hemi      = "left" if "left" in BRAIN_VIEW else "right" if "right" in BRAIN_VIEW else "both"
        mesh_data = plotter._mesh[hemi]
        vertices  = mesh_data["coords"]
        faces     = mesh_data["faces"]
        bg_map    = mesh_data["bg_map"]

        bg_norm = (bg_map - bg_map.min()) / (bg_map.max() - bg_map.min() + 1e-8)
        bg_rgb  = 1.0 - np.column_stack(
            [plotter.bg_darkness + bg_norm * (1 - plotter.bg_darkness)] * 3
        )
        cmap_obj = get_cmap("fire", alpha_cmap=(0.0, 0.2))
        pv_faces = np.column_stack([np.full(len(faces), 3), faces])
        normed   = robust_normalize(preds, percentile=99)

        # Pre-compute all color arrays
        all_colors = []
        for i in range(n_trs):
            sm       = get_scalar_mappable(normed[i], cmap_obj, vmin=0.6)
            stat_map = plotter.get_stat_map(normed[i])[hemi]
            rgba     = sm.to_rgba(stat_map)
            all_colors.append(rgba[:, 3:4] * rgba[:, :3] + (1 - rgba[:, 3:4]) * bg_rgb)

        def _render_one(idx: int) -> "tuple[int, np.ndarray]":
            pl = pv.Plotter(window_size=[BRAIN_DPI, BRAIN_DPI], off_screen=True)
            surf = pv.PolyData(vertices, pv_faces)
            surf.point_data["colors"] = all_colors[idx]
            pl.add_mesh(surf, scalars="colors", rgb=True,
                        smooth_shading=False, ambient=plotter.ambient)
            pl.set_background("white")
            vec, up = _PV_VIEWS[BRAIN_VIEW]
            pl.view_vector(vec, viewup=up)
            with _tmp.NamedTemporaryFile(suffix=".png", delete=False) as t:
                tp = t.name
            img = pl.screenshot(tp, return_img=True)
            pl.close()
            os.unlink(tp)
            return idx, tight_crop(img, w_pad=plotter.w_pad, h_pad=plotter.h_pad)

        brain_imgs = [None] * n_trs
        with concurrent.futures.ThreadPoolExecutor(max_workers=N_WORKERS) as pool:
            for idx, img in pool.map(_render_one, range(n_trs)):
                brain_imgs[idx] = img
        logger.info("[tribe_video] Rendered %d brain frames", n_trs)

        # Extract video frames for each TR at t_stim = t_brain - HEMO_LAG
        tmp_frames_dir = Path(_tmp.mkdtemp(prefix="tribe_vframes_"))
        video_frames: list = [None] * n_trs
        for i, seg in enumerate(segments):
            t_stim = max(0.0, float(getattr(seg, "start", i)) - HEMO_LAG)
            vf_out = tmp_frames_dir / f"vf_{i:05d}.png"
            res = subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{t_stim:.3f}", "-i", str(media_path),
                 "-vframes", "1", "-q:v", "2", str(vf_out)],
                capture_output=True, timeout=30,
            )
            if res.returncode == 0 and vf_out.exists():
                vframe = mpimg.imread(str(vf_out))
                if vframe.ndim == 3 and vframe.shape[2] == 4:
                    vframe = vframe[:, :, :3]
                video_frames[i] = (float(getattr(segments[i], "start", i)), t_stim, vframe)
            else:
                video_frames[i] = (float(getattr(segments[i], "start", i)), t_stim, None)

        # Build composite PNGs
        tmp_comp_dir = Path(_tmp.mkdtemp(prefix="tribe_comp_"))
        for i, (t_brain, t_stim, vframe) in enumerate(video_frames):
            fig_c, (ax_l, ax_r) = plt.subplots(
                1, 2, figsize=(COMP_W / FLOW_DPI, COMP_H / FLOW_DPI),
                facecolor="black", gridspec_kw={"wspace": 0.02},
            )
            if vframe is not None:
                ax_l.imshow(vframe)
            else:
                ax_l.set_facecolor("#111")
            ax_l.set_title(f"Stimulus  t={t_stim:.1f}s", color="white", fontsize=8, pad=2)
            ax_l.axis("off")
            if brain_imgs[i] is not None:
                ax_r.imshow(brain_imgs[i], aspect="equal")
            ax_r.set_title(f"Brain response  t={t_brain:.1f}s", color="white", fontsize=8, pad=2)
            ax_r.axis("off")
            fig_c.text(0.5, 0.01,
                f"TR {i}  ·  brain={t_brain:.1f}s  ←  stimulus={t_stim:.1f}s  (5s lag)  ·  TRIBE v2 {BRAIN_MESH}",
                color="#888", fontsize=7, ha="center", va="bottom",
            )
            fig_c.savefig(tmp_comp_dir / f"comp_{i:05d}.png",
                          dpi=FLOW_DPI, bbox_inches="tight", facecolor="black")
            plt.close(fig_c)

        # Encode to MP4
        vf_chain = "pad=ceil(iw/2)*2:ceil(ih/2)*2"
        enc_res = subprocess.run(
            ["ffmpeg", "-y", "-framerate", "1",
             "-i", str(tmp_comp_dir / "comp_%05d.png"),
             "-vf", vf_chain, "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
             str(sim_dst)],
            capture_output=True, timeout=300,
        )
        if enc_res.returncode == 0 and sim_dst.exists():
            sim_video_path = str(sim_dst)
            logger.info("[tribe_video] Brain sim video saved: %s (%d TRs)", sim_dst, n_trs)
        else:
            logger.warning("[tribe_video] ffmpeg encode failed: %s",
                           enc_res.stderr.decode()[:300])

        # Cleanup temp frame dirs
        import shutil as _sh2
        _sh2.rmtree(tmp_frames_dir, ignore_errors=True)
        _sh2.rmtree(tmp_comp_dir, ignore_errors=True)

    except Exception as exc:
        logger.warning("[tribe_video] Brain sim video generation failed: %s", exc, exc_info=True)

    return reel_video_path, sim_video_path


def _compute_score(preds: np.ndarray, n_segments: int) -> dict:
    """Derive neural engagement metrics from TRIBE v2 predictions.

    preds: (n_segments, 1000) — predicted fMRI activations in z-score units
    """
    if preds.shape[0] == 0:
        return _error_result("Model returned no predictions")

    abs_preds = np.abs(preds)  # (n, 1000)

    # Overall engagement: mean absolute activation across all TRs and parcels
    mean_abs = float(abs_preds.mean())

    # Consistency: how stable is engagement over time (low std = more consistent)
    per_tr = abs_preds.mean(axis=1)  # (n,)
    consistency = float(1.0 - min(1.0, np.std(per_tr) / (mean_abs + 1e-8)))

    # Early hook: first 25% of video vs rest (>1 = stronger opening)
    split = max(1, n_segments // 4)
    early_mean = float(abs_preds[:split].mean())
    hook_ratio = round(early_mean / (mean_abs + 1e-8), 2)

    # Normalize mean_abs to 0-100 (calibrated from Algonauts 2025 data range)
    # z-score range: low engagement ~0.05, high engagement ~0.40
    score = int(min(100, max(0, round(mean_abs / 0.40 * 100))))

    tier = "High" if score >= 70 else ("Medium" if score >= 40 else "Low")
    consistency_score = int(round(consistency * 100))

    interpretation = _interpret(score, consistency_score, hook_ratio)

    return {
        "neural_engagement_score": score,
        "tier": tier,
        "n_trs_analyzed": n_segments,
        "mean_absolute_activation": round(mean_abs, 4),
        "consistency_score": consistency_score,
        "early_hook_ratio": hook_ratio,
        "early_hook_strong": hook_ratio >= 1.15,
        "interpretation": interpretation,
        "powered_by": "Meta TRIBE v2 — fMRI encoding model",
        "license": "CC-BY-NC-4.0 (non-commercial research use only)",
        "error": None,
    }


def _interpret(score: int, consistency: int, hook_ratio: float) -> str:
    tier = "High" if score >= 70 else ("Medium" if score >= 40 else "Low")
    parts = []
    if tier == "High":
        parts.append("Strong neural engagement — content activates attention and emotion circuits.")
    elif tier == "Medium":
        parts.append("Moderate neural engagement — content registers but lacks peak stimulus.")
    else:
        parts.append("Weak neural engagement — content is not strongly activating the brain.")

    if hook_ratio >= 1.20:
        parts.append("Opening captures attention strongly (hook ratio {:.1f}x).".format(hook_ratio))
    elif hook_ratio < 0.85:
        parts.append("Opening is weak — engagement builds later (hook ratio {:.1f}x).".format(hook_ratio))

    if consistency >= 75:
        parts.append("Engagement is consistent throughout.")
    elif consistency < 45:
        parts.append("Engagement fluctuates — uneven pacing.")
    return " ".join(parts)


def _error_result(error: str) -> dict:
    return {
        "neural_engagement_score": None,
        "tier": None,
        "n_trs_analyzed": 0,
        "mean_absolute_activation": None,
        "consistency_score": None,
        "early_hook_ratio": None,
        "early_hook_strong": None,
        "interpretation": None,
        "powered_by": "Meta TRIBE v2 — fMRI encoding model",
        "license": "CC-BY-NC-4.0 (non-commercial research use only)",
        "error": error,
    }


def _trim_to_hook(video_path: str, tmpdir: str, max_sec: int) -> str:
    """Trim video to first max_sec seconds using ffmpeg.

    Analysing only the hook window (first 15 s) cuts TRIBE v2 CPU time by ~4×
    while capturing the segment that drives 80%+ of engagement signal for
    short-form D2C ad content. Returns original path on any failure.
    """
    out = os.path.join(tmpdir, "hook.mp4")
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", video_path, "-t", str(max_sec),
             "-c", "copy", "-y", out],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1024:
            logger.info("Trimmed video to %ds hook window: %s", max_sec, out)
            return out
    except Exception as exc:
        logger.debug("ffmpeg trim failed (%s) — using full video", exc)
    return video_path


class NeuralEngagementAnalyzer:
    """Async wrapper around TRIBE v2 for video-to-neural-engagement prediction.

    Usage::

        analyzer = NeuralEngagementAnalyzer()
        result = await analyzer.analyze(
            product_url="https://youtu.be/abc123",
            scraped={"description": "..."},
            brand_name="Rare Rabbit",
            search_agent=search,
        )
    """

    def _resolve_video_url(
        self, product_url: str | None, scraped: dict | None
    ) -> str | None:
        """Find the best video URL to analyze from the product URL / scraped page."""
        # 1. Product URL is itself a video platform link
        if product_url and _is_video_platform_url(product_url):
            return product_url

        # 2. Product URL is a direct video file
        if product_url and _is_direct_video_url(product_url):
            return product_url

        # 3. Embedded video URL in scraped page description/HTML
        if scraped:
            for field in ("description", "page_body", "raw_html"):
                val = scraped.get(field, "")
                if val:
                    found = _find_video_url_in_text(val)
                    if found:
                        return found

        return None

    async def _find_brand_video_url(
        self, brand_name: str, search_agent
    ) -> str | None:
        """Search DDG for the brand's official YouTube / TikTok / Instagram video."""
        queries = [
            f"{brand_name} official youtube channel",
            f"{brand_name} brand ad youtube",
            f"{brand_name} site:youtube.com",
            f"{brand_name} tiktok brand video site:tiktok.com",
            f"{brand_name} instagram reel ad site:instagram.com",
        ]
        for q in queries:
            try:
                results = search_agent.search(q, max_results=5)
                for r in results:
                    for field in ("url", "href", "link"):
                        url = r.get(field, "")
                        if url and _is_video_platform_url(url):
                            logger.info("Found brand video via DDG: %s", url)
                            return url
            except Exception as exc:
                logger.debug("DDG search failed for %r: %s", q, exc)
        return None

    async def analyze(
        self,
        product_url: str | None = None,
        scraped: dict | None = None,
        brand_name: str | None = None,
        search_agent=None,
    ) -> dict:
        """Run TRIBE v2 neural engagement analysis on video content.

        Priority order for video source:
          1. Product URL if it's a video platform link
          2. Embedded video in scraped page HTML
          3. DDG search for brand's YouTube / TikTok / Instagram video
          4. yt-dlp fallback on raw product URL

        Returns a dict with neural_engagement_score (0-100), tier, and
        interpretation. error is None on success.
        """
        video_url = self._resolve_video_url(product_url, scraped)

        # Brand video search via DDG (before falling back to raw product URL)
        if video_url is None and brand_name and search_agent is not None:
            video_url = await self._find_brand_video_url(brand_name, search_agent)

        # Last resort: try yt-dlp directly on the product URL
        if video_url is None and product_url and product_url.startswith(("http://", "https://")):
            video_url = product_url
        if video_url is None:
            return _error_result("No video content found for this product URL")

        try:
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._run_sync, video_url),
                timeout=1800.0,  # 30 min — first run downloads Llama; subsequent runs cache
            )
            return result
        except asyncio.TimeoutError:
            return _error_result("TRIBE v2 inference timed out (>30 min)")
        except Exception as exc:
            logger.error("Neural engagement analysis failed: %s", exc)
            return _error_result(str(exc))

    def _run_sync(self, video_url: str) -> dict:
        """Blocking: download media → TRIBE v2 inference → score."""
        score, preds, reel_video_path, sim_video_path = self._run_sync_full(video_url)
        if preds is not None:
            score["_raw_preds"] = preds
        if reel_video_path:
            score["_reel_video_path"] = reel_video_path
        if sim_video_path:
            score["_sim_video_path"] = sim_video_path
        return score

    def _run_sync_full(self, video_url: str) -> "tuple[dict, np.ndarray | None, str | None, str | None]":
        """Run TRIBE v2 and return (score_dict, preds, reel_video_path, sim_video_path).

        reel_video_path: local path to the trimmed reel MP4 (persisted in cache/tribe_videos/)
        sim_video_path:  local path to the brain simulation MP4 (side-by-side reel + brain)
        Both paths are None on failure or if generation fails.
        """
        tmpdir = tempfile.mkdtemp(prefix="tribe_ne_")
        try:
            logger.info("Downloading media for neural engagement: %s", video_url)
            media_path = _download_media_sync(video_url, tmpdir)
            if media_path is None:
                return (
                    _error_result(
                        f"Could not download video from {video_url} — "
                        "check yt-dlp is installed (pip install yt-dlp)"
                    ),
                    None, None, None,
                )

            # Free local media analysis (FFmpeg + OpenCV + Pillow) — runs while
            # the file is still on disk, before TRIBE v2 occupies the CPU.
            media_analysis: dict = {}
            try:
                from agents.media_processor import analyze_reel_media
                media_analysis = analyze_reel_media(media_path)
                logger.info(
                    "Media analysis: duration=%.1fs pacing=%s palette=%s",
                    media_analysis.get("duration_s", 0),
                    media_analysis.get("pacing", {}).get("pacing_label", "?"),
                    media_analysis.get("color_palette_names", []),
                )
            except Exception as _me:
                logger.debug("media_processor skipped: %s", _me)

            # Trim to hook window — analyse only the first N seconds.
            # Default: 9 s (opening hook drives 80%+ of engagement signal for short-form D2C content).
            # Set TRIBE_HOOK_SECONDS=0 to disable trimming and process the full video.
            hook_secs = int(os.getenv("TRIBE_HOOK_SECONDS", "9"))
            if hook_secs > 0:
                media_path = _trim_to_hook(media_path, tmpdir, hook_secs)

            logger.info("Running TRIBE v2 on %s …", media_path)
            model = _get_tribe_model()
            events_df = self._get_events(model, media_path)
            if events_df is None or events_df.empty or len(events_df) == 0:
                return _error_result("TRIBE v2: no events extracted from video"), None, None, None

            preds, segments = model.predict(events_df, verbose=False)
            if preds is None or preds.shape[0] == 0:
                return _error_result("TRIBE v2: model returned empty predictions"), None, None, None

            logger.info(
                "TRIBE v2 predictions: %d TRs × %d parcels, mean_abs=%.4f",
                preds.shape[0], preds.shape[1], np.abs(preds).mean()
            )
            score = _compute_score(preds, len(segments))
            # Attach free local media signals to the score dict
            if media_analysis and not media_analysis.get("error"):
                score["pacing"] = media_analysis.get("pacing", {})
                score["hook_frames_b64"] = media_analysis.get("hook_frames_b64", [])
                score["color_palette_hex"] = media_analysis.get("color_palette_hex", [])
                score["color_palette_names"] = media_analysis.get("color_palette_names", [])
                score["duration_s"] = media_analysis.get("duration_s")

            # Persist reel video + generate brain simulation video
            reel_video_path, sim_video_path = _persist_tribe_videos(
                media_path, preds, segments, video_url
            )

            return score, preds, reel_video_path, sim_video_path

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _get_events(self, model, media_path: str):
        """Extract events from video.

        On CPU-only machines, Llama-3.2-3B text embeddings take 2+ hours per
        video. Unless TRIBE_FULL_PIPELINE=1 is set, we use audio_only mode
        which skips Llama and uses audio + visual features only (runs in < 5 min).
        Set TRIBE_FULL_PIPELINE=1 to enable language features on a GPU machine.
        """
        import torch
        import pandas as pd
        from tribev2.demo_utils import get_audio_and_text_events

        has_gpu = torch.cuda.is_available()
        force_full = os.getenv("TRIBE_FULL_PIPELINE", "").lower() in ("1", "true", "yes")

        if not has_gpu and not force_full:
            # CPU-only: skip Llama text embeddings (too slow — ~2h per video)
            logger.info(
                "No CUDA GPU detected — running TRIBE v2 in audio+visual mode "
                "(skipping Llama-3.2-3B text embeddings). "
                "Set TRIBE_FULL_PIPELINE=1 to enable full pipeline on GPU."
            )
            event = {
                "type": "Video",
                "filepath": str(media_path),
                "start": 0,
                "timeline": "default",
                "subject": "default",
            }
            return get_audio_and_text_events(pd.DataFrame([event]), audio_only=True)

        # Full pipeline (GPU available or explicitly requested)
        try:
            return model.get_events_dataframe(video_path=media_path)
        except Exception as exc:
            err = str(exc)
            if "gated" in err.lower() or "401" in err or "403" in err or "awaiting" in err.lower():
                logger.warning(
                    "meta-llama/Llama-3.2-3B not accessible (%s). "
                    "Falling back to audio+visual features only. "
                    "Accept the Meta license at https://huggingface.co/meta-llama/Llama-3.2-3B "
                    "and run 'hf auth login' to enable full predictions.",
                    exc,
                )
                event = {
                    "type": "Video",
                    "filepath": str(media_path),
                    "start": 0,
                    "timeline": "default",
                    "subject": "default",
                }
                return get_audio_and_text_events(pd.DataFrame([event]), audio_only=True)
            raise
