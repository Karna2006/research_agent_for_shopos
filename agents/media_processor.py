"""Free local media analysis — FFmpeg + OpenCV + Pillow.

All processing runs on CPU with no API calls or token costs.

Public API
----------
extract_pacing_stats(video_path)   → cut density / pacing label
extract_hook_frames(video_path)    → list[bytes] of JPEG frames (first 3 s)
extract_color_palette(video_path)  → list of hex color strings
analyze_reel_media(video_path)     → combined dict (all three above)
"""
from __future__ import annotations

import base64
import colorsys
import io
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

_FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
_FFPROBE = shutil.which("ffprobe") or "ffprobe"

try:
    import cv2 as _cv2
    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False


# ── FFprobe ──────────────────────────────────────────────────────────────────

def get_video_duration(video_path: str) -> float:
    """Return duration in seconds via ffprobe. Returns 0.0 on failure."""
    try:
        r = subprocess.run(
            [_FFPROBE, "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             video_path],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


# ── Hook frames via FFmpeg ────────────────────────────────────────────────────

def extract_hook_frames(
    video_path: str,
    duration_s: float = 3.0,
    fps: float = 1.0,
) -> list[bytes]:
    """Extract JPEG frames from the first `duration_s` seconds at `fps` fps.

    The hook window (first 3 s) is the most critical segment for Reels/TikTok
    retention — if the viewer doesn't stop scrolling here, the content fails.
    """
    frames: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="shopos_hook_") as tmpdir:
        out_pattern = os.path.join(tmpdir, "frame_%03d.jpg")
        try:
            subprocess.run(
                [_FFMPEG, "-y", "-i", video_path,
                 "-t", str(duration_s),
                 "-vf", f"fps={fps}",
                 "-q:v", "4",
                 out_pattern],
                capture_output=True, timeout=30,
            )
        except Exception:
            return []
        for f in sorted(Path(tmpdir).glob("frame_*.jpg")):
            frames.append(f.read_bytes())
    return frames


# ── Cut density via OpenCV ────────────────────────────────────────────────────

def extract_pacing_stats(video_path: str) -> dict:
    """Detect scene cuts using histogram comparison (Bhattacharyya distance).

    Returns
    -------
    dict with:
      cut_count       – total detected scene changes
      duration_s      – video length in seconds
      cuts_per_second – edit rate (key virality metric)
      pacing_label    – "Ultra-Fast" / "Fast" / "Medium" / "Slow"
      cv2_available   – bool (False → fallback message in pacing_label)

    Benchmarks for D2C Reels/TikTok:
      ≥0.8 cuts/s  → viral-format edit pacing
      0.5–0.8      → fast, high retention
      0.25–0.5     → medium (educational / GRWM)
      <0.25        → slow (talking-head / vlog)
    """
    if not _HAS_CV2:
        return {
            "cut_count": None,
            "duration_s": get_video_duration(video_path),
            "cuts_per_second": None,
            "pacing_label": "Unavailable (install opencv-python-headless)",
            "cv2_available": False,
        }

    import cv2

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return {
            "cut_count": None,
            "duration_s": 0.0,
            "cuts_per_second": None,
            "pacing_label": "Cannot open video",
            "cv2_available": True,
        }

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_s = total_frames / fps if fps > 0 else 0.0

    THRESHOLD = 0.30  # Bhattacharyya distance for scene cut
    cut_count = 0
    prev_hist = None
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        # Sample every other frame — 2× speed, negligible accuracy loss
        if frame_idx % 2 != 0:
            frame_idx += 1
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hist = cv2.calcHist([gray], [0], None, [64], [0, 256])
        hist = cv2.normalize(hist, hist).flatten()

        if prev_hist is not None:
            diff = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA)
            if diff > THRESHOLD:
                cut_count += 1

        prev_hist = hist
        frame_idx += 1

    cap.release()

    cuts_per_second = cut_count / duration_s if duration_s > 0 else 0.0

    if cuts_per_second >= 0.8:
        label = "Ultra-Fast (viral-format editing)"
    elif cuts_per_second >= 0.5:
        label = "Fast (high-retention pacing)"
    elif cuts_per_second >= 0.25:
        label = "Medium (educational / narrative)"
    else:
        label = "Slow (talking-head / cinematic)"

    return {
        "cut_count": cut_count,
        "duration_s": round(duration_s, 1),
        "cuts_per_second": round(cuts_per_second, 2),
        "pacing_label": label,
        "cv2_available": True,
    }


# ── Color palette via Pillow ──────────────────────────────────────────────────

def extract_color_palette(
    frames: list[bytes],
    n_colors: int = 5,
) -> list[str]:
    """Return top N dominant hex colors from a list of JPEG frame bytes.

    Samples all frames, reduces each to a 32×32 thumbnail, quantizes RGB to
    32-step buckets, then returns the most common bucket centroids as hex.
    """
    if not frames:
        return []
    try:
        from PIL import Image

        all_pixels: list[tuple[int, int, int]] = []
        for fb in frames[:6]:
            img = Image.open(io.BytesIO(fb)).convert("RGB").resize((32, 32), Image.LANCZOS)
            all_pixels.extend(list(img.getdata()))  # type: ignore[arg-type]

        quantized = [
            (r // 32 * 32, g // 32 * 32, b // 32 * 32)
            for r, g, b in all_pixels
        ]
        top = Counter(quantized).most_common(n_colors)
        return [f"#{r:02x}{g:02x}{b:02x}" for (r, g, b), _ in top]
    except Exception:
        return []


def _palette_to_names(palette: list[str]) -> list[str]:
    """Convert hex palette to human-readable color names."""
    names = []
    for hex_col in palette:
        try:
            r = int(hex_col[1:3], 16) / 255
            g = int(hex_col[3:5], 16) / 255
            b = int(hex_col[5:7], 16) / 255
            h, s, v = colorsys.rgb_to_hsv(r, g, b)
            if v < 0.20:
                names.append("black")
            elif v > 0.88 and s < 0.12:
                names.append("white")
            elif s < 0.15:
                names.append("grey")
            else:
                h_deg = h * 360
                names.append(
                    "red"    if h_deg < 15 or h_deg >= 345 else
                    "orange" if h_deg < 35  else
                    "yellow" if h_deg < 65  else
                    "green"  if h_deg < 150 else
                    "cyan"   if h_deg < 200 else
                    "blue"   if h_deg < 260 else
                    "purple" if h_deg < 300 else
                    "pink"
                )
        except Exception:
            names.append("unknown")
    return names


# ── Combined entry point ──────────────────────────────────────────────────────

def analyze_reel_media(video_path: str) -> dict:
    """Run all free local analyses on a downloaded Reel/video file.

    Designed to be called from a thread executor (CPU-bound).

    Returns
    -------
    {
      duration_s         : float
      pacing             : { cut_count, duration_s, cuts_per_second, pacing_label }
      hook_frames_b64    : list[str]   ← base64-encoded JPEGs of first 3 frames
      color_palette_hex  : list[str]   ← top 5 dominant hex colors
      color_palette_names: list[str]   ← human-readable color names
      error              : str | None
    }
    """
    try:
        duration = get_video_duration(video_path)
        pacing = extract_pacing_stats(video_path)
        hook_frames = extract_hook_frames(video_path, duration_s=3.0, fps=1.0)
        palette_hex = extract_color_palette(hook_frames)
        palette_names = _palette_to_names(palette_hex)
        frames_b64 = [base64.b64encode(fb).decode() for fb in hook_frames[:3]]

        return {
            "duration_s": round(duration, 1),
            "pacing": pacing,
            "hook_frames_b64": frames_b64,
            "color_palette_hex": palette_hex,
            "color_palette_names": palette_names,
            "error": None,
        }
    except Exception as exc:
        return {
            "duration_s": 0.0,
            "pacing": {},
            "hook_frames_b64": [],
            "color_palette_hex": [],
            "color_palette_names": [],
            "error": str(exc),
        }
