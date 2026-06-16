"""Brain region activation visualization — SVG heatmap from TRIBE v2 or virality scores.

Two input modes:
  1. Real TRIBE v2: (n_TRs, 1000) Schaefer parcels → 7 Yeo networks → activation [0-1]
  2. Fallback:      virality dimension scores (0-10) → estimated brain activation

Outputs an SVG string for inline embedding in HTML.
"""
from __future__ import annotations
import numpy as np

# ── Network definitions ────────────────────────────────────────────────────────
# 7 Yeo networks, each mapped to a marketing meaning and a color ramp.
NETWORKS: dict[str, dict] = {
    "visual":    {"label": "Visual Cortex",    "sub": "imagery · scroll-stop · motion",   "hi": "#ef4444", "lo": "#7f1d1d"},
    "motor":     {"label": "Motor / CTA",      "sub": "action urge · buy impulse",        "hi": "#f97316", "lo": "#7c2d12"},
    "attention": {"label": "Attention",        "sub": "hook · salience · 3-sec hold",     "hi": "#facc15", "lo": "#713f12"},
    "limbic":    {"label": "Limbic / Emotion", "sub": "desire · fear · brand feeling",    "hi": "#ec4899", "lo": "#831843"},
    "default":   {"label": "Default Mode",     "sub": "identity · trend · storytelling",  "hi": "#a855f7", "lo": "#3b0764"},
    "control":   {"label": "Prefrontal",       "sub": "trust · price eval · logic",       "hi": "#3b82f6", "lo": "#1e3a8a"},
    "reward":    {"label": "Reward Circuit",   "sub": "dopamine · FOMO · social proof",   "hi": "#22c55e", "lo": "#14532d"},
}

# ── Schaefer-1000 → Yeo-7 parcel index ranges (approximate) ──────────────────
# Standard Schaefer-1000 ordering: LH parcels 0-499, RH 500-999.
# Within each hemisphere the network order is typically:
#   Cont(0-54), Default(55-250), DorsAttn(251-295), Limbic(296-320),
#   SalVentAttn(321-380), SomMot(381-444), Vis(445-499)
# These ranges are an approximation suitable for visualization.
_PARCEL_RANGES: dict[str, list[tuple[int, int]]] = {
    "control":   [(0,   55), (500, 555)],
    "default":   [(55,  251), (555, 751)],
    "attention": [(251, 296), (751, 796)],
    "limbic":    [(296, 381), (796, 881)],  # Limbic + SalVentAttn (reward-adjacent)
    "motor":     [(381, 445), (881, 945)],
    "visual":    [(445, 500), (945, 1000)],
    "reward":    [(321, 381), (821, 881)],  # SalVentAttn only (dopamine-adjacent)
}


def tribe_preds_to_network_scores(preds: np.ndarray) -> dict[str, float]:
    """Convert TRIBE v2 (n_TRs, 1000) output to per-network activation [0–1]."""
    if preds is None or preds.ndim != 2 or preds.shape[1] < 100:
        return {k: 0.0 for k in NETWORKS}
    mean_abs = np.abs(preds).mean(axis=0)
    global_max = float(mean_abs.max())
    if global_max < 1e-8:
        return {k: 0.0 for k in NETWORKS}
    scores: dict[str, float] = {}
    for net_id, ranges in _PARCEL_RANGES.items():
        vals: list[float] = []
        for lo, hi in ranges:
            chunk = mean_abs[lo : min(hi, len(mean_abs))]
            if chunk.size:
                vals.extend(chunk.tolist())
        scores[net_id] = float(np.mean(vals)) / global_max if vals else 0.0
    return scores


def virality_dims_to_network_scores(dims: dict) -> dict[str, float]:
    """Map virality dimension scores (0-10 each) → estimated brain activation (0-1).

    Handles multiple field name conventions from different LLM prompt versions.
    Called when TRIBE v2 is unavailable.
    """
    def _get(*keys: str) -> float:
        for key in keys:
            v = dims.get(key)
            if v is None:
                continue
            if isinstance(v, dict):
                raw = v.get("score", 0) or 0
            elif isinstance(v, (int, float)):
                raw = v
            else:
                continue
            return min(1.0, max(0.0, raw / 10.0))
        return 0.0

    return {
        # Visual cortex — imagery, stopping power
        "visual":    _get("visual_stopping_power", "visual_appeal", "visual_hook_strength"),
        # Motor cortex — action urge, transformation
        "motor":     _get("transformation_clarity", "transformation_promise", "hook_strength"),
        # Attention — hook, salience
        "attention": _get("hook_strength", "curiosity_gap", "share_trigger"),
        # Limbic — emotion, desire
        "limbic":    _get("emotional_trigger", "emotion"),
        # Default mode — identity, trend, story
        "default":   _get("trend_alignment", "social_currency", "identity"),
        # Prefrontal — evaluation, utility, trust
        "control":   _get("utility", "trust", "transformation_clarity"),
        # Reward circuit — social proof, sharing
        "reward":    _get("social_currency", "share_trigger", "social_proof"),
    }


# ── Color helpers ──────────────────────────────────────────────────────────────

def _lerp_hex(lo: str, hi: str, t: float) -> str:
    """Linearly interpolate between two hex colors."""
    def _p(h: str) -> tuple[int, int, int]:
        h = h.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r0, g0, b0 = _p(lo)
    r1, g1, b1 = _p(hi)
    r = int(r0 + (r1 - r0) * t)
    g = int(g0 + (g1 - g0) * t)
    b = int(b0 + (b1 - b0) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


# ── Activation tip lookups ─────────────────────────────────────────────────────

_PEAK_TIPS: dict[str, str] = {
    "reward":    "Strong dopamine trigger — drives impulse & FOMO",
    "limbic":    "Deep emotional resonance — builds brand loyalty",
    "default":   "High identity resonance — feels personally relevant",
    "visual":    "Visually arresting — stops the scroll",
    "attention": "Strong hook — viewer stays past 3s",
    "motor":     "Action-oriented — high CTA conversion",
    "control":   "Trust-building — analytical buyers respond",
}

_OPPORTUNITY_TIPS: dict[str, str] = {
    "reward":    "Add social proof (UGC/reviews) or scarcity signals",
    "limbic":    "Include emotional transformation or human story",
    "default":   "Connect to viewer identity & aspirations",
    "visual":    "Upgrade creative — lifestyle > product-on-white",
    "attention": "Sharpen opening hook — pattern interrupt",
    "motor":     "Add CTA with urgency (limited time/stock)",
    "control":   "Add trust signals: certifications, money-back",
}


# ── Main SVG generator ─────────────────────────────────────────────────────────

def generate_activation_heatmap(
    network_scores: dict[str, float],
    is_real_tribe: bool = False,
    ad_label: str = "",
) -> str:
    """Generate an fMRI-style activation intensity heatmap SVG.

    Args:
        network_scores: dict mapping network IDs to activation [0.0–1.0].
        is_real_tribe:  True if scores came from real TRIBE v2 inference.
        ad_label:       Optional label for the ad/content being analyzed.

    Returns:
        Complete SVG string for inline embedding.
    """
    # Clamp scores
    scores = {k: min(1.0, max(0.0, network_scores.get(k, 0.0))) for k in NETWORKS}

    # Sort networks descending by score
    net_order = sorted(NETWORKS, key=lambda k: -scores[k])

    # Identify peak and opportunity networks
    peak_net = net_order[0]
    # Opportunity = lowest-scoring network (most headroom)
    opportunity_net = net_order[-1]

    # ── Build defs: linear gradients + glow filters ────────────────────────────
    defs_parts: list[str] = []
    for net_id in NETWORKS:
        s = scores[net_id]
        cfg = NETWORKS[net_id]
        hi_color = cfg["hi"]
        lo_color = cfg["lo"]
        dark_start = _lerp_hex("#0a0f1e", lo_color, 0.4)

        defs_parts.append(
            f'<linearGradient id="hg_{net_id}" x1="0" y1="0" x2="1" y2="0">'
            f'<stop offset="0%" stop-color="{dark_start}"/>'
            f'<stop offset="60%" stop-color="{lo_color}"/>'
            f'<stop offset="100%" stop-color="{hi_color}"/>'
            f'</linearGradient>'
        )

        if s > 0.70:
            defs_parts.append(
                f'<filter id="gf_{net_id}" x="-5%" y="-50%" width="110%" height="200%">'
                f'<feGaussianBlur stdDeviation="2.5" result="glow"/>'
                f'<feMerge><feMergeNode in="glow"/><feMergeNode in="SourceGraphic"/></feMerge>'
                f'</filter>'
            )

    # ── Build network rows ─────────────────────────────────────────────────────
    ROW_START_Y = 44
    ROW_HEIGHT  = 36
    BAR_X       = 172
    BAR_W       = 290
    BAR_H       = 18

    row_svgs: list[str] = []
    for idx, net_id in enumerate(net_order):
        s          = scores[net_id]
        cfg        = NETWORKS[net_id]
        hi_color   = cfg["hi"]
        is_peak    = net_id == peak_net
        pct        = int(s * 100)

        row_y      = ROW_START_Y + idx * ROW_HEIGHT
        row_center = row_y + ROW_HEIGHT // 2

        # Left section ── indicator circle + labels
        circle_r   = 5 + s * 2
        circle_op  = 0.25 + s * 0.75
        lbl_weight = "700" if is_peak else "500"
        lbl_fill   = hi_color if is_peak else "#94a3b8"

        row_svgs.append(
            f'<circle cx="14" cy="{row_center}" r="{circle_r:.2f}" '
            f'fill="{hi_color}" opacity="{circle_op:.2f}"/>'
            f'<text x="24" y="{row_center - 5}" '
            f'font-family="system-ui,sans-serif" font-size="10.5" '
            f'font-weight="{lbl_weight}" fill="{lbl_fill}">'
            f'{_esc(cfg["label"])}</text>'
            f'<text x="24" y="{row_center + 7}" '
            f'font-family="system-ui,sans-serif" font-size="8" fill="#475569">'
            f'{_esc(cfg["sub"])}</text>'
        )

        # Bar section ── track, threshold marker, heat fill, tip highlight, glow
        bar_fill_w = s * BAR_W
        threshold_x = BAR_X + BAR_W // 2  # 317

        row_svgs.append(
            # Background track
            f'<rect x="{BAR_X}" y="{row_center - 8}" width="{BAR_W}" height="{BAR_H}" '
            f'rx="5" fill="#0d1829"/>'
            # 50% threshold marker
            f'<line x1="{threshold_x}" y1="{row_center - 8}" '
            f'x2="{threshold_x}" y2="{row_center - 8 + BAR_H}" '
            f'stroke="#1e3a5f" stroke-width="1"/>'
        )

        if bar_fill_w > 0:
            # Heat bar fill
            if s > 0.70:
                row_svgs.append(
                    f'<rect x="{BAR_X}" y="{row_center - 8}" '
                    f'width="{bar_fill_w:.2f}" height="{BAR_H}" rx="5" '
                    f'fill="url(#hg_{net_id})" filter="url(#gf_{net_id})"/>'
                )
            else:
                row_svgs.append(
                    f'<rect x="{BAR_X}" y="{row_center - 8}" '
                    f'width="{bar_fill_w:.2f}" height="{BAR_H}" rx="5" '
                    f'fill="url(#hg_{net_id})"/>'
                )

            # Bright tip highlight when score > 0.5
            if s > 0.5:
                tip_x = BAR_X + bar_fill_w - 4
                row_svgs.append(
                    f'<rect x="{tip_x:.2f}" y="{row_center - 8}" '
                    f'width="4" height="{BAR_H}" rx="2" '
                    f'fill="{hi_color}" opacity="0.9"/>'
                )

        # Right section ── score percentage
        score_size   = "11.5" if is_peak else "10"
        score_weight = "800"  if is_peak else "500"
        score_fill   = hi_color if pct > 10 else "#334155"
        row_svgs.append(
            f'<text x="470" y="{row_center + 4}" '
            f'font-family="system-ui,sans-serif" font-size="{score_size}" '
            f'font-weight="{score_weight}" fill="{score_fill}">'
            f'{pct}%</text>'
        )

    # ── Footer ────────────────────────────────────────────────────────────────
    footer_y      = ROW_START_Y + len(net_order) * ROW_HEIGHT + 8
    divider_y     = footer_y
    peak_tip_y    = footer_y + 12
    opp_tip_y     = footer_y + 24
    badge_y       = footer_y + 6

    peak_cfg      = NETWORKS[peak_net]
    peak_hi       = peak_cfg["hi"]
    peak_tip_text = _PEAK_TIPS.get(peak_net, peak_cfg["sub"])
    opp_cfg       = NETWORKS[opportunity_net]
    opp_tip_text  = _OPPORTUNITY_TIPS.get(opportunity_net, opp_cfg["sub"])

    source_label  = "Meta TRIBE v2 · fMRI" if is_real_tribe else "Estimated · virality"
    source_color  = "#22c55e" if is_real_tribe else "#f59e0b"

    # Ad label — right-aligned, max 45 chars
    ad_label_svg = ""
    if ad_label:
        ad_label_svg = (
            f'<text x="546" y="20" font-family="system-ui,sans-serif" '
            f'font-size="9" fill="#475569" text-anchor="end">'
            f'{_esc(ad_label[:45])}</text>'
        )

    # Source badge — right-aligned in footer
    badge_text_w = len(source_label) * 5 + 12
    badge_x      = 546 - badge_text_w

    total_h = ROW_START_Y + len(NETWORKS) * ROW_HEIGHT + 44
    svg = (
        f'<svg viewBox="0 0 560 {total_h}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-width:580px;background:#060d1a;border-radius:12px;font-family:system-ui,sans-serif">\n'
        f'  <defs>\n'
        f'    {"".join(defs_parts)}\n'
        f'  </defs>\n'
        f'\n'
        f'  <!-- Canvas background -->\n'
        f'  <rect width="560" height="{total_h}" fill="#060d1a" rx="12"/>\n'
        f'\n'
        f'  <!-- ── Header ── -->\n'
        f'  <text x="14" y="20" font-family="system-ui,sans-serif" '
        f'font-size="13" font-weight="800" fill="#e2e8f0" letter-spacing="-0.3">'
        f'Neural Activation Heatmap</text>\n'
        f'  <text x="14" y="33" font-family="system-ui,sans-serif" '
        f'font-size="9.5" fill="#475569">'
        f'How this content fires the brain · 7 Yeo functional networks</text>\n'
        f'  {ad_label_svg}\n'
        f'  <line x1="14" y1="40" x2="546" y2="40" stroke="#0f172a" stroke-width="1"/>\n'
        f'\n'
        f'  <!-- ── Network rows ── -->\n'
        f'  {"".join(row_svgs)}\n'
        f'\n'
        f'  <!-- ── Footer ── -->\n'
        f'  <line x1="14" y1="{divider_y}" x2="546" y2="{divider_y}" '
        f'stroke="#0f172a" stroke-width="1"/>\n'
        f'  <text x="14" y="{peak_tip_y}" font-family="system-ui,sans-serif" '
        f'font-size="9" fill="{peak_hi}">'
        f'▲ Peak: {_esc(peak_cfg["label"])} — {_esc(peak_tip_text)}</text>\n'
        f'  <text x="14" y="{opp_tip_y}" font-family="system-ui,sans-serif" '
        f'font-size="9" fill="#64748b">'
        f'↑ Opportunity: {_esc(opp_cfg["label"])} — {_esc(opp_tip_text)}</text>\n'
        f'  <rect x="{badge_x}" y="{badge_y}" width="{badge_text_w}" height="14" '
        f'rx="3" fill="#0f172a"/>\n'
        f'  <text x="{badge_x + 6}" y="{badge_y + 10}" '
        f'font-family="system-ui,sans-serif" font-size="8" fill="{source_color}">'
        f'{_esc(source_label)}</text>\n'
        f'</svg>'
    )
    return svg


def generate_brain_svg(
    network_scores: dict[str, float],
    is_real_tribe: bool = False,
    ad_label: str = "",
) -> str:
    """Backward-compatible alias for generate_activation_heatmap."""
    return generate_activation_heatmap(network_scores, is_real_tribe, ad_label)


# ── Heat colormap (blue → yellow → red, standard fMRI scale) ──────────────────

_HEAT_STOPS: list[tuple[float, tuple[int, int, int]]] = [
    (0.00, (10,  28, 120)),   # deep blue  — below threshold
    (0.20, (28, 100, 200)),   # medium blue
    (0.45, (255, 230,  80)),  # yellow     — activation onset
    (0.70, (230,  80,  50)),  # orange-red — strong activation
    (1.00, (158,  15,  15)),  # deep red   — peak
]


def _heat_color(score: float) -> str:
    """Map 0-1 activation to the standard fMRI hot colormap (blue→yellow→red)."""
    s = min(1.0, max(0.0, score))
    for i in range(len(_HEAT_STOPS) - 1):
        t0, c0 = _HEAT_STOPS[i]
        t1, c1 = _HEAT_STOPS[i + 1]
        if t0 <= s <= t1:
            frac = (s - t0) / (t1 - t0)
            r = int(c0[0] + (c1[0] - c0[0]) * frac)
            g = int(c0[1] + (c1[1] - c0[1]) * frac)
            b = int(c0[2] + (c1[2] - c0[2]) * frac)
            return f"#{r:02x}{g:02x}{b:02x}"
    _, c = _HEAT_STOPS[-1]
    return f"#{c[0]:02x}{c[1]:02x}{c[2]:02x}"


def generate_anatomical_brain_svg(
    network_scores: dict[str, float],
    is_real_tribe: bool = False,
    ad_label: str = "",
) -> str:
    """Anatomical brain heat map — left hemisphere, sagittal view.

    Each lobe is colored by its corresponding Yeo-7 network activation.
    Color scale: deep blue (0% inactive) → yellow (activation onset) → deep red (100% peak).

    Rendering: SVG, safe for inline HTML embedding.
    """
    scores = {k: min(1.0, max(0.0, network_scores.get(k, 0.0))) for k in NETWORKS}

    # Unique ID suffix so multiple SVGs on the same page don't collide
    _uid = format(abs(hash(tuple(sorted(scores.items())))) % 0xFFFFFF, "x")

    # ── Anatomical region → Yeo network mapping ───────────────────────────────
    _rn: dict[str, str] = {
        "frontal":    "control",
        "motor":      "motor",
        "parietal":   "attention",
        "occipital":  "visual",
        "temporal":   "limbic",
        "reward":     "reward",
    }

    def _col(region: str) -> str:
        return _heat_color(scores.get(_rn[region], 0.0))

    def _op(region: str) -> str:
        s = scores.get(_rn[region], 0.0)
        return f"{0.52 + s * 0.44:.2f}"

    # ── Brain paths — 280×225 local coordinate space ──────────────────────────
    # Sagittal left hemisphere. Anterior (frontal) = LEFT. Posterior = RIGHT.
    # All regions are clipped to the brain silhouette via clip-path.

    BRAIN = (
        "M 15,108 "
        "C 14,82  20,52  40,32 "
        "C 60,14  90,4   122,2 "
        "C 152,-2 184,2  210,14 "
        "C 240,26 266,52 274,84 "
        "C 282,116 274,150 256,170 "
        "C 240,186 216,196 190,200 "
        "C 165,204 140,206 112,212 "
        "C 85,218  60,220  40,215 "
        "C 20,210   8,196   8,178 "
        "C  8,158  10,136  15,108 Z"
    )

    CEREB = (
        "M 228,235 "
        "C 248,225 274,225 292,238 "
        "C 311,252 312,274 298,286 "
        "C 284,298 258,302 234,295 "
        "C 208,288 196,268 204,252 "
        "C 209,239 220,240 228,235 Z"
    )

    STEM = (
        "M 186,198 "
        "C 198,192 216,192 224,200 "
        "C 230,208 228,222 218,228 "
        "C 208,234 196,234 188,226 "
        "C 180,218 180,204 186,198 Z"
    )

    # Region fills — large shapes, all clipped to brain silhouette
    # Drawn back→front so motor strip and reward appear on top
    FILLS: list[tuple[str, str]] = [
        ("temporal",  "M 6,160 L 258,160 L 254,235 L 6,235 Z"),
        ("occipital", "M 210,0  L 310,0  L 310,230 L 210,230 Z"),
        ("parietal",  "M 128,0  L 218,0  L 218,178 L 128,188 Z"),
        ("frontal",   "M 0,0   L 115,0  L 108,232 L 0,232 Z"),
        ("motor",     "M 110,0  L 138,0  L 132,232 L 104,232 Z"),
    ]

    # ── Glow filters for high-activation regions ──────────────────────────────
    glow_defs = ""
    for net_id in NETWORKS:
        if scores.get(net_id, 0) > 0.68:
            glow_defs += (
                f'<filter id="bg{_uid}_{net_id}" '
                f'x="-25%" y="-25%" width="150%" height="150%">'
                f'<feGaussianBlur stdDeviation="3.5" result="g"/>'
                f'<feMerge><feMergeNode in="g"/>'
                f'<feMergeNode in="SourceGraphic"/></feMerge>'
                f'</filter>'
            )

    def _gf(region: str) -> str:
        net = _rn.get(region, "")
        if scores.get(net, 0) > 0.68:
            return f' filter="url(#bg{_uid}_{net})"'
        return ""

    # ── Region fill SVG (drawn back→front) ────────────────────────────────────
    fill_svg = ""
    for region, path in FILLS:
        fill_svg += (
            f'<path d="{path}" fill="{_col(region)}" opacity="{_op(region)}" '
            f'clip-path="url(#bc{_uid})"{_gf(region)}/>\n'
        )

    # Reward circuit — subcortical spot inside the temporal region
    rs = scores.get("reward", 0)
    fill_svg += (
        f'<ellipse cx="128" cy="152" rx="28" ry="20" '
        f'fill="{_col("reward")}" opacity="{_op("reward")}" '
        f'clip-path="url(#bc{_uid})"{_gf("reward")}/>\n'
    )

    # ── Sulci lines (anatomical detail overlay) ───────────────────────────────
    sulci_svg = (
        f'<g fill="none" stroke="#060f1d" stroke-width="1.3" opacity="0.65">'
        # Central sulcus (divides frontal / parietal)
        f'<path d="M 114,4 C 118,38 120,75 116,112 C 113,132 108,152 102,170"/>'
        # Lateral (Sylvian) fissure
        f'<path d="M 28,148 C 64,155 104,158 142,160 C 170,162 200,160 228,156"/>'
        # Post-central sulcus
        f'<path d="M 138,4 C 141,36 141,70 137,105 C 134,124 128,142 122,160"/>'
        # Superior temporal sulcus
        f'<path d="M 54,175 C 90,181 132,183 172,181 C 202,179 226,174 244,168"/>'
        # Frontal gyri (decorative)
        f'<path d="M 40,58  C 50,52  62,52  70,60"/>'
        f'<path d="M 68,30  C 80,24  92,24 100,32"/>'
        f'<path d="M 30,88  C 40,83  52,84  58,92"/>'
        # Parietal gyri
        f'<path d="M 194,30 C 206,25 218,27 225,36"/>'
        f'<path d="M 222,70 C 233,65 244,68 250,78"/>'
        f'</g>'
    )

    # ── Labels (inside each region) ───────────────────────────────────────────
    label_data = [
        # region,    text1,         text2,                       lx,  ly
        ("frontal",  "PREFRONTAL",  f'{int(scores["control"]*100)}% active',  54,  72),
        ("parietal", "PARIETAL",    f'{int(scores["attention"]*100)}% active', 172, 60),
        ("occipital","VISUAL",      f'{int(scores["visual"]*100)}% active',   248, 90),
        ("temporal", "TEMPORAL",    f'{int(scores["limbic"]*100)}% active',   104,186),
    ]
    labels_svg = ""
    for region, l1, l2, lx, ly in label_data:
        net = _rn[region]
        s_val = scores.get(net, 0)
        tc  = "#f1f5f9" if s_val > 0.40 else "#64748b"
        tc2 = "#94a3b8" if s_val > 0.40 else "#475569"
        labels_svg += (
            f'<text x="{lx}" y="{ly}" text-anchor="middle" clip-path="url(#bc{_uid})" '
            f'font-size="8.5" font-weight="700" fill="{tc}">{l1}</text>'
            f'<text x="{lx}" y="{ly+11}" text-anchor="middle" clip-path="url(#bc{_uid})" '
            f'font-size="7" fill="{tc2}">{l2}</text>'
        )

    # Motor strip label (rotated)
    ms = scores.get("motor", 0)
    mc = "#f1f5f9" if ms > 0.40 else "#64748b"
    labels_svg += (
        f'<text x="122" y="82" text-anchor="middle" clip-path="url(#bc{_uid})" '
        f'font-size="6.5" font-weight="700" fill="{mc}" '
        f'transform="rotate(-90,122,82)">MOTOR {int(ms*100)}%</text>'
    )
    # Reward spot label
    rmc = "#f1f5f9" if rs > 0.40 else "#64748b"
    labels_svg += (
        f'<text x="128" y="155" text-anchor="middle" clip-path="url(#bc{_uid})" '
        f'font-size="6" font-weight="700" fill="{rmc}">REWARD {int(rs*100)}%</text>'
    )

    # ── Right panel: network scores bar list ──────────────────────────────────
    net_order = sorted(NETWORKS.keys(), key=lambda k: -scores.get(k, 0))
    peak_net  = net_order[0]
    opp_net   = net_order[-1]
    RX, RW    = 390, 155

    right_svg = ""
    for idx, nk in enumerate(net_order):
        cfg    = NETWORKS[nk]
        s_val  = scores.get(nk, 0)
        pct    = int(s_val * 100)
        color  = _heat_color(s_val)
        bar_w  = max(4, int(s_val * RW))
        ry     = 88 + idx * 31
        is_pk  = nk == peak_net
        right_svg += (
            f'<text x="{RX}" y="{ry}" font-size="9" '
            f'font-weight="{"700" if is_pk else "400"}" '
            f'fill="{"#e2e8f0" if is_pk else "#64748b"}">{_esc(cfg["label"])}</text>'
            f'<rect x="{RX}" y="{ry+4}" width="{RW}" height="9" rx="4" fill="#0d1829"/>'
            f'<rect x="{RX}" y="{ry+4}" width="{bar_w}" height="9" rx="4" fill="{color}"/>'
            f'<text x="{RX+RW+6}" y="{ry+12}" font-size="8" font-weight="700" fill="{color}">{pct}%</text>'
        )

    # ── Color scale legend (vertical gradient bar) ────────────────────────────
    legend_svg = ""
    LEG_X, LEG_Y, LEG_H = 372, 70, 210
    for i in range(21):
        t = 1.0 - (i / 20)
        c = _heat_color(t)
        legend_svg += (
            f'<rect x="{LEG_X}" y="{LEG_Y + i * (LEG_H/20):.1f}" '
            f'width="11" height="{LEG_H/20 + 0.5:.1f}" fill="{c}"/>'
        )
    legend_svg += (
        f'<text x="{LEG_X+13}" y="{LEG_Y+8}" font-size="7.5" fill="#9e0f0f">MAX</text>'
        f'<text x="{LEG_X+13}" y="{LEG_Y+LEG_H+2}" font-size="7.5" fill="#1c64c8">MIN</text>'
        f'<rect x="{LEG_X}" y="{LEG_Y}" width="11" height="{LEG_H}" '
        f'fill="none" stroke="#1e293b" stroke-width="0.5"/>'
    )

    # ── Source badge + ad label ────────────────────────────────────────────────
    source_label = "Meta TRIBE v2 · fMRI" if is_real_tribe else "Estimated · virality dims"
    source_color = "#22c55e" if is_real_tribe else "#f59e0b"
    peak_color   = _heat_color(scores.get(peak_net, 0))

    ad_svg = ""
    if ad_label:
        ad_svg = (
            f'<text x="348" y="20" text-anchor="end" font-size="9" fill="#475569">'
            f'{_esc(ad_label[:44])}</text>'
        )

    TOTAL_H = 385

    return (
        f'<svg viewBox="0 0 628 {TOTAL_H}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-width:660px;background:#060d1a;border-radius:14px;'
        f'font-family:system-ui,sans-serif">\n'
        f'<defs>\n'
        f'  <clipPath id="bc{_uid}"><path d="{BRAIN}"/></clipPath>\n'
        f'  {glow_defs}\n'
        f'</defs>\n'
        f'<rect width="628" height="{TOTAL_H}" fill="#060d1a" rx="14"/>\n'
        # Header
        f'<text x="18" y="22" font-size="13" font-weight="800" fill="#e2e8f0" '
        f'letter-spacing="-0.3">Brain Activation Map</text>\n'
        f'<text x="18" y="36" font-size="9" fill="#475569">'
        f'Neural response to content · left hemisphere sagittal · Yeo-7 networks</text>\n'
        f'{ad_svg}\n'
        f'<line x1="18" y1="44" x2="610" y2="44" stroke="#0f172a" stroke-width="1"/>\n'
        # Brain group (local coordinate space, shifted to x=20, y=55 in SVG)
        f'<g transform="translate(20,55)" font-family="system-ui,sans-serif">\n'
        f'  <path d="{BRAIN}" fill="#0d1829"/>\n'
        f'  {fill_svg}'
        f'  {sulci_svg}\n'
        f'  {labels_svg}\n'
        f'  <path d="{BRAIN}" fill="none" stroke="#2d4a72" stroke-width="1.5"/>\n'
        # Cerebellum
        f'  <path d="{CEREB}" fill="{_col("motor")}" opacity="{_op("motor")}"/>\n'
        f'  <path d="{CEREB}" fill="none" stroke="#1e3a5f" stroke-width="1"/>\n'
        f'  <text x="260" y="275" text-anchor="middle" font-size="7" fill="#475569">'
        f'CEREBELLUM</text>\n'
        f'  <path d="{STEM}" fill="#0b1525" stroke="#1e3a5f" stroke-width="0.8"/>\n'
        f'</g>\n'
        # Divider
        f'<line x1="365" y1="44" x2="365" y2="{TOTAL_H-12}" stroke="#0f172a" '
        f'stroke-width="1"/>\n'
        # Right panel
        f'<text x="{RX}" y="72" font-size="10" font-weight="700" fill="#cbd5e1">'
        f'Network Activation</text>\n'
        f'{legend_svg}\n'
        f'{right_svg}\n'
        # Footer
        f'<line x1="{RX}" y1="310" x2="610" y2="310" stroke="#0f172a" stroke-width="1"/>\n'
        f'<text x="{RX}" y="321" font-size="8.5" fill="{peak_color}">'
        f'▲ {_esc(NETWORKS[peak_net]["label"])} — {_esc(_PEAK_TIPS.get(peak_net, ""))}'
        f'</text>\n'
        f'<text x="{RX}" y="334" font-size="8" fill="#64748b">'
        f'↑ Opportunity: {_esc(NETWORKS[opp_net]["label"])}</text>\n'
        f'<text x="{RX}" y="345" font-size="7.5" fill="#475569">'
        f'{_esc(_OPPORTUNITY_TIPS.get(opp_net, ""))}</text>\n'
        f'<rect x="{RX}" y="360" width="148" height="14" rx="3" fill="#0f172a"/>\n'
        f'<text x="{RX+6}" y="370" font-size="8" fill="{source_color}">'
        f'{_esc(source_label)}</text>\n'
        f'</svg>'
    )


def build_from_tribe(preds: "np.ndarray", ad_label: str = "") -> str:
    """Build SVG directly from raw TRIBE v2 prediction array."""
    scores = tribe_preds_to_network_scores(preds)
    return generate_activation_heatmap(scores, is_real_tribe=True, ad_label=ad_label)


def build_from_virality(virality_data: dict, ad_label: str = "") -> str:
    """Build SVG from virality analysis data (fallback when TRIBE v2 unavailable)."""
    dims = virality_data.get("dimensions") or {}
    scores = virality_dims_to_network_scores(dims)
    return generate_activation_heatmap(scores, is_real_tribe=False, ad_label=ad_label)
