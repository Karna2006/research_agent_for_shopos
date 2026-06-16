"""Visual attention / saliency heatmap analysis using DeepGaze IIE.

DeepGaze IIE is a neural saliency model trained on human eye-tracking data.
Given a product image it predicts WHERE humans look first — useful for diagnosing
whether a hero shot draws attention to the right region.

Model weights (~300 MB) are downloaded once on first use and cached by PyTorch.
All inference runs on CPU — no GPU required.
"""
from __future__ import annotations

import asyncio
import base64
import io

import matplotlib
matplotlib.use("Agg")  # non-interactive backend — must come before pyplot import
import matplotlib.pyplot as plt
import numpy as np

try:
    import torch
    from PIL import Image
    from deepgaze_pytorch import DeepGazeIIE
    _DEEPGAZE_AVAILABLE = True
except ImportError:
    _DEEPGAZE_AVAILABLE = False


# Module-level lazy singleton — loaded once, reused across calls.
_model: "DeepGazeIIE | None" = None


def _get_model() -> "DeepGazeIIE":
    global _model
    if _model is None:
        model = DeepGazeIIE(pretrained=True)
        model.eval()
        _model = model
    return _model


def _run_saliency(img: "Image.Image") -> "np.ndarray":
    """Blocking CPU inference — always call via run_in_executor."""
    model = _get_model()
    img_512 = img.resize((512, 512), Image.LANCZOS)
    img_arr = np.array(img_512)  # (512, 512, 3)
    img_tensor = torch.FloatTensor(img_arr).permute(2, 0, 1).unsqueeze(0) / 255.0  # (1,3,512,512)
    # centerbias must be 3D: (batch, H, W) — Finalizer.view() expects this shape
    centerbias = torch.zeros(1, 512, 512)
    with torch.no_grad():
        log_density = model(img_tensor, centerbias)  # (1, 1, 512, 512) log-density
    saliency = torch.exp(log_density).squeeze().numpy()  # (512, 512)
    max_val = saliency.max()
    return saliency / max_val if max_val > 0 else saliency


def _overlay_heatmap(img: "Image.Image", saliency: "np.ndarray") -> str:
    """Render heatmap overlay as a base64-encoded PNG string."""
    fig, ax = plt.subplots(1, 1, figsize=(5, 5))
    ax.imshow(img)
    ax.imshow(saliency, alpha=0.55, cmap="jet", vmin=0, vmax=1)
    ax.axis("off")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0, dpi=100)
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.getvalue()).decode()


def _extract_metrics(saliency: "np.ndarray") -> dict:
    """Derive human-readable attention metrics from the saliency map."""
    h, w = saliency.shape

    # Primary focus: where is the global maximum?
    flat_idx = int(np.argmax(saliency))
    row, col = divmod(flat_idx, w)
    v_zone = "upper" if row < h / 3 else ("middle" if row < 2 * h / 3 else "lower")
    h_zone = "left"  if col < w / 3 else ("center" if col < 2 * w / 3 else "right")
    focus = f"{v_zone}-{h_zone}"
    primary_area = f"{v_zone} {h_zone} region"

    # Concentration: what fraction of the image holds 90 % of saliency mass?
    flat = saliency.ravel()
    sorted_desc = np.sort(flat)[::-1]
    cumsum = np.cumsum(sorted_desc)
    total = cumsum[-1]
    if total > 0:
        idx_90 = int(np.searchsorted(cumsum, 0.9 * total))
        concentration_pct = idx_90 / flat.size  # fraction of pixels holding 90% mass
    else:
        concentration_pct = 1.0
    distribution = "concentrated" if concentration_pct < 0.12 else "spread"

    # Attention hotspot count: distinct peaks above the 85th percentile
    threshold = float(np.percentile(saliency, 85))
    hotspot_pixels = int(np.sum(saliency > threshold))

    return {
        "focus": focus,
        "hotspot_pixels": hotspot_pixels,
        "primary_area": primary_area,
        "distribution": distribution,
        "concentration_pct": round(concentration_pct * 100, 1),
    }


def _interpret(focus: str, distribution: str) -> str:
    """One-line human interpretation of the attention pattern."""
    v, h = focus.split("-", 1) if "-" in focus else (focus, "center")
    if v == "upper" and h == "center":
        return "Upper-center attention — text or face is drawing eyes first (hook is working)"
    if distribution == "spread":
        return "Distributed attention — no clear focal point, weakens engagement"
    if v == "lower":
        return "Lower attention only — product not drawing eyes; consider a lifestyle shot"
    if v == "upper":
        return f"Upper-{h} attention — headline/logo area dominates over product"
    if v == "middle" and h == "center":
        return "Center attention — product is the focal point (ideal for product shots)"
    return f"{focus.replace('-', ' ').title()} focus — {distribution} attention pattern"


class VisualAttentionAnalyzer:
    """Wraps DeepGaze IIE for async-friendly product image analysis."""

    async def analyze_image_url(self, image_url: str) -> dict:
        """Download image, run saliency, return heatmap + attention metrics.

        Returns a dict with ``heatmap_base64`` (embed in HTML as data URI),
        ``attention_focus``, ``primary_attention_area``, ``attention_distribution``,
        ``interpretation``, and ``error`` (None on success).
        """
        if not _DEEPGAZE_AVAILABLE:
            return {
                "heatmap_base64": None,
                "error": "deepgaze_pytorch not installed",
            }
        if not image_url or not image_url.startswith(("http://", "https://")):
            return {"heatmap_base64": None, "error": "invalid image URL"}

        try:
            import httpx

            # Use a real browser UA + Referer so Shopify/CDN doesn't block the download
            _HEADERS = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.google.com/",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "sec-fetch-dest": "image",
                "sec-fetch-mode": "no-cors",
                "sec-fetch-site": "cross-site",
            }

            async with httpx.AsyncClient(
                timeout=20,
                follow_redirects=True,
                headers=_HEADERS,
            ) as client:
                resp = await client.get(image_url)
            if resp.status_code != 200 or len(resp.content) < 512:
                return {
                    "heatmap_base64": None,
                    "error": f"image download failed (HTTP {resp.status_code})",
                }

            from PIL import Image as _PILImage

            img = _PILImage.open(io.BytesIO(resp.content)).convert("RGB")
            img_512 = img.resize((512, 512), _PILImage.LANCZOS)

            # CPU-bound inference → thread executor so event loop isn't blocked
            loop = asyncio.get_event_loop()
            saliency = await loop.run_in_executor(None, _run_saliency, img_512)

            heatmap_b64 = _overlay_heatmap(img_512, saliency)
            metrics = _extract_metrics(saliency)
            interpretation = _interpret(metrics["focus"], metrics["distribution"])

            return {
                "heatmap_base64": heatmap_b64,
                "attention_focus": metrics["focus"],
                "primary_attention_area": metrics["primary_area"],
                "attention_distribution": metrics["distribution"],
                "concentration_pct": metrics["concentration_pct"],
                "interpretation": interpretation,
                "powered_by": "DeepGaze IIE — human visual attention model",
                "error": None,
            }

        except Exception as exc:
            return {"heatmap_base64": None, "error": str(exc)}
