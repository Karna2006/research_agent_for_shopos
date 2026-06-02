"""Side-by-side competitive comparison report — pure HTML, no Jinja2 template."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlparse

_IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> str:
    return datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST")

# ── Dimension definitions (radar axes, table rows) ────────────────────────────

_DIMS = [
    ("brand",    "Brand Basics"),
    ("content",  "Content Quality"),
    ("ads",      "Ad Performance"),
    ("geo",      "GEO Visibility"),
    ("store",    "Store CRO"),
    ("research", "Research Fit"),
]
_AXES_KEYS   = [d[0] for d in _DIMS]
_AXES_LABELS = [d[1] for d in _DIMS]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None:
            return default
    return cur


def _clamp(v, lo: float = 0.0, hi: float = 10.0) -> float:
    try:
        return round(min(max(float(v or 0), lo), hi), 1)
    except (TypeError, ValueError):
        return 5.0


def _brand_name_from_url(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    netloc = re.sub(r"^www\.", "", netloc)
    name_part = netloc.split(".")[0]
    return " ".join(w.capitalize() for w in re.split(r"[-_]", name_part))


def _score_color(v: float) -> str:
    if v >= 7: return "#22c55e"
    if v >= 5: return "#f59e0b"
    return "#ef4444"


def _esc(s: object) -> str:
    """HTML-escape a value for safe embedding."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Score extraction ──────────────────────────────────────────────────────────

def extract_dim_scores(results: dict) -> dict:
    """Extract six 0-10 dimension scores from a full agent results dict."""

    content = _get(results, "content_catalog", "analysis", "pdp_quality_score")
    if content is None:
        content = _get(results, "content_catalog", "analysis", "homepage_score", default=5.0)

    ads = _get(results, "performance_ads", "analysis", "hook_strength_score", default=5.0)

    geo_raw = _get(results, "geo_visibility", "analysis", "geo_score")
    geo = round(float(geo_raw) / 10, 1) if geo_raw is not None else 3.8

    store_raw = (
        _get(results, "store_cro", "pagespeed", "mobile_score") or
        _get(results, "store_cro", "analysis", "pagespeed_mobile")
    )
    store = round(float(store_raw) / 10, 1) if store_raw is not None else 5.0

    bb = _get(results, "brand_basics", "analysis") or {}
    signals = [
        1.0 if bb.get("founding_year") else 0.0,
        1.0 if bb.get("brand_positioning") else 0.0,
        1.0 if bb.get("tone_of_voice") else 0.0,
        1.0 if bb.get("target_audience") else 0.0,
        min(len(bb.get("social_channels") or {}), 4) / 4.0,
        min(len(bb.get("key_strengths") or []), 5) / 5.0,
    ]
    brand = 5.0 + (sum(signals) / len(signals)) * 5.0

    res = _get(results, "research", "analysis") or {}
    wins  = len(res.get("where_brand_wins")  or [])
    loses = len(res.get("where_brand_loses") or [])
    total = wins + loses
    research = (wins / total * 10.0) if total > 0 else 5.0

    return {
        "brand":    _clamp(brand),
        "content":  _clamp(content),
        "ads":      _clamp(ads),
        "geo":      _clamp(geo),
        "store":    _clamp(store),
        "research": _clamp(research),
    }


def overall_score(scores: dict) -> float:
    vals = [v for v in scores.values() if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 1) if vals else 0.0


# ── Radar chart (pure SVG, 30% opacity fills) ─────────────────────────────────

def _radar_svg(sa: dict, sb: dict, la: str, lb: str) -> str:
    cx, cy, R = 220, 235, 158
    n = len(_AXES_KEYS)

    def pt(val: float, i: int) -> tuple[float, float]:
        angle = math.pi * 2 * i / n - math.pi / 2
        v = _clamp(val) / 10 * R
        return cx + v * math.cos(angle), cy + v * math.sin(angle)

    def poly(scores: dict) -> str:
        return " ".join(
            f"{pt(scores.get(k, 0), i)[0]:.1f},{pt(scores.get(k, 0), i)[1]:.1f}"
            for i, k in enumerate(_AXES_KEYS)
        )

    grid = ""
    for frac, opacity in [(0.25, "0.2"), (0.5, "0.25"), (0.75, "0.3"), (1.0, "0.45")]:
        ring = " ".join(
            f"{cx + R * frac * math.cos(math.pi * 2 * i / n - math.pi / 2):.1f},"
            f"{cy + R * frac * math.sin(math.pi * 2 * i / n - math.pi / 2):.1f}"
            for i in range(n)
        )
        grid += f'<polygon points="{ring}" fill="none" stroke="#3a3a3a" stroke-width="1" opacity="{opacity}"/>'

    grid_labels = ""
    for frac, lbl in [(0.25, "2.5"), (0.5, "5"), (0.75, "7.5"), (1.0, "10")]:
        gx, gy = cx + 3, cy - R * frac
        grid_labels += (
            f'<text x="{gx:.0f}" y="{gy:.0f}" font-size="9" fill="#404040" '
            f'font-family="system-ui,sans-serif" dominant-baseline="middle">{lbl}</text>'
        )

    axes = ""
    for i in range(n):
        angle = math.pi * 2 * i / n - math.pi / 2
        ax, ay = cx + R * math.cos(angle), cy + R * math.sin(angle)
        axes += f'<line x1="{cx:.0f}" y1="{cy:.0f}" x2="{ax:.1f}" y2="{ay:.1f}" stroke="#2e2e2e" stroke-width="1"/>'

    # B below A so A renders on top; 30% opacity fills
    poly_b = f'<polygon points="{poly(sb)}" fill="rgba(245,158,11,.30)" stroke="#f59e0b" stroke-width="2.5" stroke-linejoin="round"/>'
    poly_a = f'<polygon points="{poly(sa)}" fill="rgba(59,130,246,.30)" stroke="#3b82f6" stroke-width="2.5" stroke-linejoin="round"/>'

    dots = ""
    for i, k in enumerate(_AXES_KEYS):
        ax, ay = pt(sa.get(k, 0), i)
        bx, by = pt(sb.get(k, 0), i)
        dots += (
            f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="4.5" fill="#f59e0b" opacity=".9"/>'
            f'<circle cx="{ax:.1f}" cy="{ay:.1f}" r="4.5" fill="#3b82f6" opacity=".9"/>'
        )

    label_els = ""
    for i, (key, lbl) in enumerate(zip(_AXES_KEYS, _AXES_LABELS)):
        angle = math.pi * 2 * i / n - math.pi / 2
        lx = cx + (R + 32) * math.cos(angle)
        ly = cy + (R + 32) * math.sin(angle)
        cos_a = math.cos(angle)
        anchor = "middle" if abs(cos_a) < 0.25 else ("start" if cos_a > 0 else "end")
        va = sa.get(key, 0)
        vb = sb.get(key, 0)
        label_els += (
            f'<text x="{lx:.1f}" y="{ly - 5:.1f}" text-anchor="{anchor}" '
            f'font-size="12" font-weight="600" fill="#d1d5db" font-family="system-ui,sans-serif">{lbl}</text>'
            f'<text x="{lx:.1f}" y="{ly + 9:.1f}" text-anchor="{anchor}" font-size="10" '
            f'font-family="system-ui,sans-serif">'
            f'<tspan fill="#60a5fa" font-weight="700">{va:.1f}</tspan>'
            f'<tspan fill="#444"> vs </tspan>'
            f'<tspan fill="#fbbf24" font-weight="700">{vb:.1f}</tspan>'
            f'</text>'
        )

    la_short = la[:22]
    lb_short = lb[:22]
    legend = (
        f'<rect x="8" y="8" width="15" height="15" rx="3" fill="rgba(59,130,246,.35)" stroke="#3b82f6" stroke-width="1.5"/>'
        f'<text x="27" y="19" font-size="12" fill="#93c5fd" font-weight="600" font-family="system-ui,sans-serif">{la_short}</text>'
        f'<rect x="8" y="28" width="15" height="15" rx="3" fill="rgba(245,158,11,.35)" stroke="#f59e0b" stroke-width="1.5"/>'
        f'<text x="27" y="39" font-size="12" fill="#fcd34d" font-weight="600" font-family="system-ui,sans-serif">{lb_short}</text>'
    )

    w, h = 440, 470
    return (
        f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'style="max-width:100%;display:block;margin:0 auto">'
        f'{grid}{grid_labels}{axes}{poly_b}{poly_a}{dots}{label_els}{legend}'
        f'</svg>'
    )


# ── Dim detail extraction for expandable rows ─────────────────────────────────

def _extract_dim_detail(key: str, results: dict) -> list[tuple[str, str]]:
    """Return (label, value) pairs shown in the expanded score row."""
    pairs: list[tuple[str, str]] = []
    if key == "brand":
        bb = _get(results, "brand_basics", "analysis") or {}
        if bb.get("founding_year"):
            pairs.append(("Founded", str(bb["founding_year"])))
        if bb.get("brand_positioning"):
            pairs.append(("Positioning", str(bb["brand_positioning"])[:75]))
        channels = list((bb.get("social_channels") or {}).keys())
        if channels:
            pairs.append(("Social", ", ".join(channels[:5])))
        strengths = bb.get("key_strengths") or []
        if strengths:
            pairs.append(("Top strength", str(strengths[0])[:75]))
    elif key == "content":
        cc = _get(results, "content_catalog", "analysis") or {}
        if cc.get("pdp_quality_score") is not None:
            pairs.append(("PDP score", f"{cc['pdp_quality_score']}/10"))
        weaknesses = cc.get("pdp_weaknesses") or []
        if weaknesses:
            pairs.append(("Top issue", str(weaknesses[0])[:80]))
        rewrites = cc.get("pdp_rewrites") or {}
        if isinstance(rewrites, dict) and rewrites.get("headline"):
            pairs.append(("Suggested headline", str(rewrites["headline"])[:75]))
    elif key == "ads":
        pa = _get(results, "performance_ads", "analysis") or {}
        if pa.get("hook_strength_score") is not None:
            pairs.append(("Hook strength", f"{pa['hook_strength_score']}/10"))
        formats = pa.get("ad_formats_present") or []
        if formats:
            pairs.append(("Ad formats", ", ".join(str(f) for f in formats[:4])))
        if pa.get("landing_page_match_score") is not None:
            pairs.append(("LP match", f"{pa['landing_page_match_score']}/10"))
    elif key == "geo":
        geo = _get(results, "geo_visibility", "analysis") or {}
        if geo.get("geo_score") is not None:
            pairs.append(("GEO score", f"{geo['geo_score']}/100"))
        pairs.append(("ChatGPT cited", "Yes" if geo.get("chatgpt_mentioned") else "No"))
        pairs.append(("Wikipedia", "Yes" if geo.get("wikipedia_present") else "No"))
        schemas = geo.get("schema_types") or []
        if schemas:
            pairs.append(("Schema types", ", ".join(str(s) for s in schemas[:4])))
    elif key == "store":
        ps = _get(results, "store_cro", "pagespeed") or {}
        cro = _get(results, "store_cro", "analysis") or {}
        if ps.get("mobile_score") is not None:
            pairs.append(("Mobile PSI", str(ps["mobile_score"])))
        if ps.get("desktop_score") is not None:
            pairs.append(("Desktop PSI", str(ps["desktop_score"])))
        if cro.get("cro_score") is not None:
            pairs.append(("CRO score", f"{cro['cro_score']}/10"))
        lcp = ps.get("lcp") or ps.get("largest_contentful_paint")
        if lcp:
            pairs.append(("LCP", str(lcp)))
    elif key == "research":
        res = _get(results, "research", "analysis") or {}
        wins  = (res.get("where_brand_wins")  or [])[:2]
        loses = (res.get("where_brand_loses") or [])[:2]
        if wins:
            pairs.append(("Wins vs competitors", "; ".join(str(w)[:50] for w in wins)))
        if loses:
            pairs.append(("Gaps vs competitors", "; ".join(str(l)[:50] for l in loses)))
    return pairs or [("Detail", "No data available")]


def _detail_row_html(key: str, results_a: dict, results_b: dict, la: str, lb: str) -> str:
    da = _extract_dim_detail(key, results_a)
    db_ = _extract_dim_detail(key, results_b)

    def _kv(pairs: list) -> str:
        return "".join(
            f'<div style="margin:.22rem 0;font-size:.79rem">'
            f'<span style="color:#444;font-size:.72rem">{_esc(k)}: </span>'
            f'<span style="color:#9ca3af">{_esc(v)}</span>'
            f'</div>'
            for k, v in pairs
        )

    return (
        f'<tr id="detail-{key}" style="display:none;background:#0c0c0c">'
        f'<td colspan="4" style="padding:.55rem 1rem 1rem 1.5rem">'
        f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:1.5rem;padding-top:.3rem">'
        f'<div>'
        f'<div style="font-size:.66rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.08em;color:#60a5fa;margin-bottom:.3rem">{_esc(la)}</div>'
        f'{_kv(da)}'
        f'</div>'
        f'<div>'
        f'<div style="font-size:.66rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.08em;color:#fcd34d;margin-bottom:.3rem">{_esc(lb)}</div>'
        f'{_kv(db_)}'
        f'</div>'
        f'</div>'
        f'</td>'
        f'</tr>'
    )


# ── Scoring table with expandable rows ────────────────────────────────────────

def _winner_cell(va: float, vb: float, la: str, lb: str) -> str:
    if abs(va - vb) < 0.15:
        return '<span style="color:#444;font-size:.78rem">—</span>'
    if va > vb:
        return (f'<span style="color:#60a5fa;font-weight:700;font-size:.79rem">'
                f'{la[:14]} ✓</span>')
    return (f'<span style="color:#fcd34d;font-weight:700;font-size:.79rem">'
            f'{lb[:14]} ✓</span>')


def _score_table_html(
    sa: dict, sb: dict, oa: float, ob: float, la: str, lb: str,
    results_a: Optional[dict] = None,
    results_b: Optional[dict] = None,
) -> str:
    results_a = results_a or {}
    results_b = results_b or {}
    cell_pad = "padding:.65rem .9rem"
    th = (
        f'<tr style="background:#141414">'
        f'<th style="{cell_pad};text-align:left;color:#6b7280;font-size:.71rem;font-weight:700;'
        f'text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid #1e1e1e">'
        f'Dimension <span style="color:#333;font-size:.65rem;font-weight:400">(click to expand)</span></th>'
        f'<th style="{cell_pad};text-align:center;color:#60a5fa;font-size:.8rem;font-weight:700;'
        f'border-bottom:1px solid #1e1e1e">{la[:18]}</th>'
        f'<th style="{cell_pad};text-align:center;color:#fcd34d;font-size:.8rem;font-weight:700;'
        f'border-bottom:1px solid #1e1e1e">{lb[:18]}</th>'
        f'<th style="{cell_pad};text-align:center;color:#6b7280;font-size:.71rem;font-weight:700;'
        f'text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid #1e1e1e">Winner</th>'
        f'</tr>'
    )

    def dim_row(key: str, dim_label: str) -> str:
        va, vb = sa.get(key, 0), sb.get(key, 0)
        detail = _detail_row_html(key, results_a, results_b, la, lb)
        return (
            f'<tr style="border-bottom:1px solid #181818;cursor:pointer" '
            f'onclick="_toggleRow(\'{key}\')">'
            f'<td style="{cell_pad};color:#9ca3af;font-weight:600">'
            f'{dim_label}'
            f'<span id="arr-{key}" style="color:#333;font-size:.72rem;margin-left:.4rem;'
            f'transition:color .15s">▸</span>'
            f'</td>'
            f'<td style="{cell_pad};color:{_score_color(va)};font-weight:700;text-align:center">'
            f'{va:.1f}<span style="font-size:.73rem;color:#444;font-weight:400">/10</span></td>'
            f'<td style="{cell_pad};color:{_score_color(vb)};font-weight:700;text-align:center">'
            f'{vb:.1f}<span style="font-size:.73rem;color:#444;font-weight:400">/10</span></td>'
            f'<td style="{cell_pad};text-align:center">{_winner_cell(va, vb, la, lb)}</td>'
            f'</tr>'
            f'{detail}'
        )

    rows = "".join(dim_row(k, lbl) for k, lbl in _DIMS)
    overall_row = (
        f'<tr style="border-top:2px solid #2a2a2a">'
        f'<td style="{cell_pad};color:#e8e8e8;font-weight:800;font-size:.95rem">OVERALL</td>'
        f'<td style="{cell_pad};color:{_score_color(oa)};font-weight:800;font-size:1.1rem;text-align:center">'
        f'{oa:.1f}<span style="font-size:.73rem;color:#444;font-weight:400">/10</span></td>'
        f'<td style="{cell_pad};color:{_score_color(ob)};font-weight:800;font-size:1.1rem;text-align:center">'
        f'{ob:.1f}<span style="font-size:.73rem;color:#444;font-weight:400">/10</span></td>'
        f'<td style="{cell_pad};text-align:center">{_winner_cell(oa, ob, la, lb)}</td>'
        f'</tr>'
    )

    return (
        f'<div style="overflow-x:auto">'
        f'<table style="width:100%;border-collapse:collapse;font-size:.88rem">'
        f'<thead>{th}</thead>'
        f'<tbody>{rows}{overall_row}</tbody>'
        f'</table>'
        f'</div>'
    )


# ── Key findings ──────────────────────────────────────────────────────────────

def _dim_verdicts_html(verdicts: list, la: str, lb: str) -> str:
    if not verdicts:
        return ""
    rows = ""
    for v in verdicts:
        dim = _esc(v.get("dimension", ""))
        winner = _esc(v.get("winner", ""))
        why = _esc(v.get("why_winner_wins", ""))
        fix = _esc(v.get("loser_fix", ""))
        gap = v.get("gap")
        gap_str = f"{gap:.1f} pts" if isinstance(gap, (int, float)) else ""
        is_a = la[:20] in winner or winner.startswith("Brand A")
        winner_color = "#60a5fa" if is_a else "#fcd34d"
        gap_html = '<span style="color:#555;font-size:.75rem">+' + gap_str + "</span>" if gap_str else ""
        fix_html = '<div style="font-size:.78rem;color:#f59e0b;border-left:2px solid #f59e0b44;padding-left:.55rem;margin-top:.3rem">Fix: ' + fix + "</div>" if fix else ""
        rows += (
            f'<div style="background:#111;border:1px solid #1e1e1e;border-radius:8px;'
            f'padding:.85rem 1rem;margin-bottom:.55rem">'
            f'<div style="display:flex;align-items:baseline;justify-content:space-between;'
            f'gap:.5rem;flex-wrap:wrap;margin-bottom:.35rem">'
            f'<span style="font-weight:700;color:#e8e8e8;font-size:.88rem">{dim}</span>'
            f'<div style="display:flex;gap:.5rem;align-items:center">'
            f'<span style="color:{winner_color};font-weight:700;font-size:.8rem">✓ {winner[:22]}</span>'
            f'{gap_html}'
            f'</div>'
            f'</div>'
            f'<div style="font-size:.82rem;color:#9ca3af;line-height:1.55;margin-bottom:.3rem">{why}</div>'
            f'{fix_html}'
            f'</div>'
        )
    return (
        f'<div style="font-size:.66rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.09em;color:#6b7280;margin-bottom:.65rem">Dimension Verdicts</div>'
        + rows
    )


def _steal_this_html(steal_this: list) -> str:
    if not steal_this:
        return ""
    cards = ""
    for item in steal_this[:3]:
        what = _esc(item.get("what", ""))
        from_brand = _esc(item.get("from_brand", ""))
        why = _esc(item.get("why", ""))
        how = _esc(item.get("how", ""))
        cards += (
            f'<div style="background:#0d1117;border:1px solid #1e3a5f;border-radius:8px;'
            f'padding:.9rem 1rem">'
            f'<div style="font-size:.66rem;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.08em;color:#60a5fa;margin-bottom:.3rem">From {from_brand[:22]}</div>'
            f'<div style="font-weight:700;color:#e8e8e8;font-size:.88rem;margin-bottom:.3rem">{what}</div>'
            f'<div style="font-size:.8rem;color:#9ca3af;margin-bottom:.35rem">{why}</div>'
            f'<div style="font-size:.78rem;color:#4ade80;border-left:2px solid #22c55e44;'
            f'padding-left:.55rem">→ {how}</div>'
            f'</div>'
        )
    return (
        f'<div style="font-size:.66rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.09em;color:#6b7280;margin:.9rem 0 .65rem">Steal This</div>'
        f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:.75rem;margin-bottom:.9rem">'
        f'{cards}'
        f'</div>'
    )


def _journey_html(journey: dict, la: str, lb: str) -> str:
    if not journey:
        return ""
    stages = [
        ("awareness", "Awareness"),
        ("consideration", "Consideration"),
        ("conversion", "Conversion"),
        ("retention", "Retention"),
    ]
    rows = ""
    for key, label in stages:
        stage = journey.get(key) or {}
        winner = _esc(stage.get("winner", "—"))
        evidence = _esc(stage.get("evidence", ""))
        verdict = _esc(stage.get("verdict", ""))
        is_a = la[:20] in winner or winner.startswith("Brand A")
        w_color = "#60a5fa" if is_a else ("#fcd34d" if winner != "—" else "#555")
        rows += (
            f'<tr style="border-bottom:1px solid #181818">'
            f'<td style="padding:.6rem .85rem;color:#9ca3af;font-weight:600;'
            f'font-size:.83rem;white-space:nowrap">{label}</td>'
            f'<td style="padding:.6rem .85rem;color:{w_color};font-weight:700;font-size:.82rem">{winner}</td>'
            f'<td style="padding:.6rem .85rem;color:#6b7280;font-size:.8rem">{evidence}</td>'
            f'<td style="padding:.6rem .85rem;color:#d1d5db;font-size:.8rem">{verdict}</td>'
            f'</tr>'
        )
    return (
        f'<div style="font-size:.66rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.09em;color:#6b7280;margin:.9rem 0 .65rem">Customer Journey Battleground</div>'
        f'<div style="overflow-x:auto;border-radius:8px;border:1px solid #1e1e1e;margin-bottom:.9rem">'
        f'<table style="width:100%;border-collapse:collapse;font-size:.85rem">'
        f'<thead><tr style="background:#141414">'
        f'<th style="padding:.5rem .85rem;text-align:left;color:#6b7280;font-size:.7rem;'
        f'font-weight:700;text-transform:uppercase;border-bottom:1px solid #1e1e1e">Stage</th>'
        f'<th style="padding:.5rem .85rem;text-align:left;color:#6b7280;font-size:.7rem;'
        f'font-weight:700;text-transform:uppercase;border-bottom:1px solid #1e1e1e">Winner</th>'
        f'<th style="padding:.5rem .85rem;text-align:left;color:#6b7280;font-size:.7rem;'
        f'font-weight:700;text-transform:uppercase;border-bottom:1px solid #1e1e1e">Evidence</th>'
        f'<th style="padding:.5rem .85rem;text-align:left;color:#6b7280;font-size:.7rem;'
        f'font-weight:700;text-transform:uppercase;border-bottom:1px solid #1e1e1e">Verdict</th>'
        f'</tr></thead>'
        f'<tbody>{rows}</tbody>'
        f'</table>'
        f'</div>'
    )


def _findings_html(findings: dict, la: str, lb: str) -> str:
    # New rich schema
    dim_verdicts = findings.get("dimension_verdicts") or []
    steal_this   = findings.get("steal_this") or []
    journey      = findings.get("customer_journey_battleground") or {}
    verdict      = findings.get("head_to_head_verdict", "")
    underdog     = findings.get("the_underdog_opportunity", "")
    blindspot    = findings.get("shared_blindspot", "")

    if dim_verdicts or steal_this or journey:
        # Render rich new schema
        out = _dim_verdicts_html(dim_verdicts, la, lb)
        out += _steal_this_html(steal_this)
        out += _journey_html(journey, la, lb)

        if verdict:
            out += (
                f'<div style="background:#141414;border:1px solid #1e1e1e;border-radius:10px;'
                f'padding:1rem 1.25rem;margin-bottom:.75rem">'
                f'<div style="font-size:.66rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.09em;color:#9ca3af;margin-bottom:.4rem">⚔ Head-to-Head Verdict</div>'
                f'<div style="font-size:.88rem;color:#d1d5db;line-height:1.7">{_esc(verdict)}</div>'
                f'</div>'
            )
        if underdog:
            out += (
                f'<div style="background:#160b00;border:1px solid #3d2600;border-radius:10px;'
                f'padding:1rem 1.25rem;margin-bottom:.75rem">'
                f'<div style="font-size:.66rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.09em;color:#fb923c;margin-bottom:.4rem">🎯 Underdog Opportunity</div>'
                f'<div style="font-size:.88rem;color:#fdba74;line-height:1.7">{_esc(underdog)}</div>'
                f'</div>'
            )
        if blindspot:
            out += (
                f'<div style="background:#0d0b16;border:1px solid #2d1b5e;border-radius:10px;'
                f'padding:1rem 1.25rem">'
                f'<div style="font-size:.66rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.09em;color:#a78bfa;margin-bottom:.4rem">⚠ Shared Blindspot</div>'
                f'<div style="font-size:.88rem;color:#c4b5fd;line-height:1.7">{_esc(blindspot)}</div>'
                f'</div>'
            )
        return f'<div class="card">{out}</div>'

    # Fallback: old schema
    def bullets(items: list) -> str:
        if not items:
            return '<li style="color:#555;padding:.28rem 0">No data available</li>'
        return "".join(
            f'<li style="padding:.35rem 0;border-bottom:1px solid #1c1c1c;'
            f'color:#d1d5db;font-size:.87rem">{_esc(item)}</li>'
            for item in items[:3]
        )

    a_wins = findings.get("a_wins") or []
    b_wins = findings.get("b_wins") or []
    threat = findings.get("a_threat_from_b", "")
    opp    = findings.get("a_opportunity_vs_b", "")

    cols = (
        f'<div style="background:#0d1520;border:1px solid #1e3a5f;border-radius:10px;'
        f'padding:1.1rem 1.25rem">'
        f'<div style="font-size:.67rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.09em;color:#60a5fa;margin-bottom:.65rem">Where {la[:20]} leads</div>'
        f'<ul style="list-style:none;padding:0">{bullets(a_wins)}</ul>'
        f'</div>'
        + f'<div style="background:#160f00;border:1px solid #3d2600;border-radius:10px;'
        f'padding:1.1rem 1.25rem">'
        f'<div style="font-size:.67rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.09em;color:#fcd34d;margin-bottom:.65rem">Where {lb[:20]} leads</div>'
        f'<ul style="list-style:none;padding:0">{bullets(b_wins)}</ul>'
        f'</div>'
    )
    threat_block = ""
    if threat:
        threat_block = (
            f'<div style="background:#160808;border:1px solid #3a1111;border-radius:10px;'
            f'padding:1rem 1.25rem;margin-bottom:.75rem">'
            f'<div style="font-size:.67rem;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.09em;color:#f87171;margin-bottom:.45rem">'
            f'⚠ Biggest threat to {la[:20]}</div>'
            f'<div style="font-size:.88rem;color:#fca5a5;line-height:1.6">{_esc(threat)}</div>'
            f'</div>'
        )
    opp_block = ""
    if opp:
        opp_block = (
            f'<div style="background:#081608;border:1px solid #1a3a1a;border-radius:10px;'
            f'padding:1rem 1.25rem">'
            f'<div style="font-size:.67rem;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.09em;color:#4ade80;margin-bottom:.45rem">'
            f'✦ Biggest opportunity for {la[:20]}</div>'
            f'<div style="font-size:.88rem;color:#86efac;line-height:1.6">{_esc(opp)}</div>'
            f'</div>'
        )
    return (
        f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1rem">'
        f'{cols}'
        f'</div>'
        f'{threat_block}'
        f'{opp_block}'
    )


# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = """\
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0a0a;--card:#141414;--border:#1e1e1e;
  --text:#e8e8e8;--muted:#6b7280;--blue:#3b82f6;--amber:#f59e0b;
  --green:#22c55e;--red:#ef4444;--r:10px;
}
html{font-size:15px;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased}
body{font-family:system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
     max-width:960px;margin:0 auto;padding:0 1.5rem 5rem;line-height:1.6}
a{color:inherit;text-decoration:none}
.sec-hdr{display:flex;align-items:baseline;gap:.75rem;padding-left:1rem;
  border-left:4px solid var(--blue);margin:2.5rem 0 1.35rem}
.sec-num{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--blue)}
.sec-title{font-size:1.25rem;font-weight:700;letter-spacing:-.2px}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:1.25rem}
.swot-grid{display:grid;grid-template-columns:1fr 1fr;gap:1.25rem;margin-bottom:1rem}
.swot-card{background:#111;border:1px solid #1e1e1e;border-radius:10px;padding:1.25rem}
footer{text-align:center;padding:2rem 0 1rem;color:var(--muted);font-size:.78rem;
  border-top:1px solid var(--border);margin-top:3rem;line-height:1.8}
@media(max-width:600px){.swot-grid{grid-template-columns:1fr}}
@media print{
  :root{--bg:#fff;--card:#f9f9f9;--border:#ddd;--text:#111;--muted:#555}
  *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  #cmp-toolbar,button,.no-print{display:none!important}
}
"""


# ── Toolbar ───────────────────────────────────────────────────────────────────

def _compare_toolbar_html(
    la: str, lb: str,
    audit_id_a: Optional[int],
    audit_id_b: Optional[int],
    share_token: Optional[str],
) -> str:
    btn = (
        "display:inline-flex;align-items:center;gap:.3rem;padding:.32rem .78rem;"
        "border-radius:6px;border:1px solid #2a2a2a;background:#181818;"
        "color:#e8e8e8;font-size:.76rem;font-weight:600;cursor:pointer;"
        "text-decoration:none;white-space:nowrap"
    )

    if audit_id_a:
        link_a = (
            f'<a href="/report/{audit_id_a}" target="_blank" '
            f'style="{btn};color:#60a5fa;border-color:rgba(59,130,246,.3)">'
            f'↗ {la[:16]} Report</a>'
        )
    else:
        link_a = (
            f'<button disabled title="Individual report not available for this brand" '
            f'style="{btn};opacity:.35;cursor:not-allowed;color:#60a5fa">'
            f'↗ {la[:16]} Report</button>'
        )

    if audit_id_b:
        link_b = (
            f'<a href="/report/{audit_id_b}" target="_blank" '
            f'style="{btn};color:#fcd34d;border-color:rgba(245,158,11,.3)">'
            f'↗ {lb[:16]} Report</a>'
        )
    else:
        link_b = (
            f'<button disabled title="Individual report not available for this brand" '
            f'style="{btn};opacity:.35;cursor:not-allowed;color:#fcd34d">'
            f'↗ {lb[:16]} Report</button>'
        )

    share_path = json.dumps(f"/share/compare/{share_token}" if share_token else "")

    return (
        f'<div id="cmp-toolbar" style="position:sticky;top:0;z-index:200;'
        f'background:rgba(10,10,10,.95);backdrop-filter:blur(8px);'
        f'-webkit-backdrop-filter:blur(8px);border-bottom:1px solid #1e1e1e;'
        f'padding:.45rem 1.5rem;display:flex;align-items:center;gap:.4rem;flex-wrap:wrap">'
        f'{link_a}'
        f'{link_b}'
        f'<button id="cmp-share-btn" onclick="_shareCmp()" style="{btn}">📋 Share Comparison</button>'
        f'</div>'
        f'<script>'
        f'function _shareCmp(){{'
        f'var p={share_path};'
        f'var u=p?window.location.origin+p:window.location.href;'
        f'var b=document.getElementById("cmp-share-btn");var o=b.innerHTML;'
        f'if(navigator.clipboard){{'
        f'navigator.clipboard.writeText(u).then(function(){{'
        f'b.innerHTML="✓ Copied!";b.style.color="#22c55e";'
        f'setTimeout(function(){{b.innerHTML=o;b.style.color="";}},2200);'
        f'}}).catch(function(){{prompt("Copy:",u);}});'
        f'}}else{{prompt("Copy:",u);}}'
        f'}}'
        f'</script>'
    )


# ── Client-side JS for SWOT + Strategy ───────────────────────────────────────

def _dynamic_js(compare_id: Optional[int], la: str, lb: str) -> str:
    """Embedded JS for row toggle, SWOT auto-load, and Strategy on-demand."""
    cid = compare_id or "null"
    la_js = json.dumps(la)
    lb_js = json.dumps(lb)

    # JS is built as a raw string to avoid f-string / JS template literal conflicts
    js = (
        "function _toggleRow(key){"
        "var row=document.getElementById('detail-'+key);"
        "var arr=document.getElementById('arr-'+key);"
        "var open=row.style.display!=='none';"
        "row.style.display=open?'none':'';"
        "if(arr){arr.textContent=open?'▸':'▾';arr.style.color=open?'#333':'#60a5fa';}"
        "}"

        "function _loadSwot(){"
        "var sec=document.getElementById('swot-section');"
        "var hdr=document.getElementById('swot-hdr');"
        "if(!sec||!__cmpId)return;"
        "sec.innerHTML='<div style=\"text-align:center;padding:2rem;color:#555;font-size:.87rem\">Generating SWOT…</div>';"
        "fetch('/compare/'+__cmpId+'/swot',{method:'POST'})"
        ".then(function(r){return r.json();})"
        ".then(function(d){"
        "if(hdr)hdr.style.display='';"
        "sec.innerHTML=_renderSwot(d);"
        "})"
        ".catch(function(){"
        "sec.innerHTML='<div style=\"padding:1rem;color:#555;font-size:.82rem\">SWOT unavailable — try refreshing.</div>';"
        "});"
        "}"

        "function _renderSwot(data){"
        "var ba=data.brand_a_swot||{};"
        "var bb=data.brand_b_swot||{};"
        "function _itemHtml(item,c){"
        "if(typeof item==='string'){return '<div style=\"font-size:.81rem;color:#9ca3af;padding:.16rem 0;border-left:2px solid '+c+'44;padding-left:.55rem;margin:.15rem 0\">'+item+'</div>';}"
        "var p=item.point||'';var e=item.evidence||'';"
        "return '<div style=\"font-size:.81rem;color:#9ca3af;padding:.22rem 0;border-left:2px solid '+c+'44;padding-left:.55rem;margin:.18rem 0\">'"
        "+'<span style=\"color:#d1d5db\">'+p+'</span>'"
        "+(e?'<br><span style=\"font-size:.75rem;color:#555;font-style:italic\">'+e+'</span>':'')"
        "+'</div>';"
        "}"
        "function _card(swot,name,nc){"
        "var secs=[['💪 Strengths',swot.strengths||[],'#22c55e'],"
        "['⚠ Weaknesses',swot.weaknesses||[],'#ef4444'],"
        "['✦ Opportunities',swot.opportunities||[],'#3b82f6'],"
        "['⚡ Threats',swot.threats||[],'#f59e0b']];"
        "return '<div class=\"swot-card\">'"
        "+'<div style=\"font-size:.7rem;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.09em;color:'+nc+';margin-bottom:1rem\">'+name+'</div>'"
        "+secs.map(function(s){"
        "return '<div style=\"margin-bottom:.8rem\">'"
        "+'<div style=\"font-size:.74rem;font-weight:700;color:'+s[2]+';margin-bottom:.3rem\">'+s[0]+'</div>'"
        "+s[1].map(function(i){return _itemHtml(i,s[2]);}).join('')"
        "+'</div>';"
        "}).join('')"
        "+'</div>';"
        "}"
        "var h='<div class=\"swot-grid\">'+_card(ba,__la,'#60a5fa')+_card(bb,__lb,'#fcd34d')+'</div>';"
        "if(data.overall_winner||data.match_summary){"
        "var margin=data.winning_margin?(' — '+data.winning_margin):'';var ms=data.match_summary||'';"
        "h+='<div style=\"background:#141414;border:1px solid #2a2a2a;border-radius:10px;padding:.85rem 1.25rem;margin-bottom:.75rem\">'"
        "+(data.overall_winner?'<div style=\"font-size:.66rem;font-weight:700;text-transform:uppercase;letter-spacing:.09em;color:#22c55e;margin-bottom:.3rem\">Overall Winner'+margin+'</div><div style=\"font-size:.92rem;font-weight:700;color:#e8e8e8;margin-bottom:.4rem\">'+data.overall_winner+'</div>':'')"
        "+(ms?'<div style=\"font-size:.82rem;color:#9ca3af;line-height:1.6\">'+ms+'</div>':'')"
        "+'</div>';"
        "}"
        "if(data.head_to_head_verdict){"
        "h+='<div style=\"background:#141414;border:1px solid #1e1e1e;border-radius:10px;"
        "padding:1.1rem 1.25rem;margin-bottom:.75rem\">'"
        "+'<div style=\"font-size:.66rem;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.09em;color:#9ca3af;margin-bottom:.45rem\">⚔ Head-to-Head Verdict</div>'"
        "+'<div style=\"font-size:.87rem;color:#d1d5db;line-height:1.7\">'+data.head_to_head_verdict+'</div>'"
        "+'</div>';"
        "}"
        "return h;"
        "}"

        "var _strCacheA=null,_strCacheB=null;"
        "function _loadStrategy(brand){"
        "var content=document.getElementById('strategy-content');"
        "var hdr=document.getElementById('strategy-hdr');"
        "var btnA=document.getElementById('btn-str-a');"
        "var btnB=document.getElementById('btn-str-b');"
        "var cached=brand==='a'?_strCacheA:_strCacheB;"
        "if(cached){if(hdr)hdr.style.display='';content.innerHTML=_renderStrategy(cached);return;}"
        "var goal='outperform '+(brand==='a'?__lb:__la);"
        "content.innerHTML='<div style=\"text-align:center;padding:1.5rem;color:#555;"
        "font-size:.87rem\">Building 90-day battle plan…</div>';"
        "fetch('/strategy',{method:'POST',"
        "headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({brand:brand,compare_id:__cmpId,goal:goal})})"
        ".then(function(r){return r.json();})"
        ".then(function(d){"
        "if(brand==='a')_strCacheA=d;else _strCacheB=d;"
        "if(hdr)hdr.style.display='';"
        "content.innerHTML=_renderStrategy(d);"
        "})"
        ".catch(function(e){"
        "content.innerHTML='<div style=\"color:#555;font-size:.82rem;padding:.5rem\">"
        "Strategy unavailable — try refreshing.</div>';"
        "});"
        "}"

        "function _renderStrategy(data){"
        "var h='';"
        "function _ec(e){return e==='Low'?'#22c55e':e==='Medium'?'#f59e0b':'#ef4444';}"
        "if(data.situation_in_one_line){"
        "h+='<div style=\"font-size:.87rem;color:#d1d5db;line-height:1.7;margin-bottom:1.1rem;"
        "padding:.85rem 1.1rem;background:#0d0d0d;border-radius:8px;border:1px solid #1a1a1a\">"
        "'+data.situation_in_one_line+'</div>';"
        "}"
        "var gap=data.the_gap_that_matters_most||{};"
        "if(gap.dimension){"
        "h+='<div style=\"background:#0d1520;border:1px solid #1e3a5f;border-radius:8px;"
        "padding:.85rem 1.05rem;margin-bottom:.85rem\">'"
        "+'<div style=\"font-size:.66rem;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.09em;color:#60a5fa;margin-bottom:.35rem\">🎯 The Gap That Matters Most</div>'"
        "+'<div style=\"font-weight:700;color:#e8e8e8;font-size:.9rem;margin-bottom:.25rem\">'+gap.dimension+'</div>'"
        "+(gap.current_gap?'<div style=\"font-size:.8rem;color:#9ca3af;margin-bottom:.25rem\">'+gap.current_gap+'</div>':'')"
        "+(gap.why_it_matters?'<div style=\"font-size:.8rem;color:#60a5fa;border-left:2px solid #3b82f644;padding-left:.55rem\">'+gap.why_it_matters+'</div>':'')"
        "+'</div>';"
        "}"
        "function _actionCards(items,color,label){"
        "if(!items||!items.length)return '';"
        "return '<div style=\"background:#111;border:1px solid #1e1e1e;border-radius:8px;padding:.8rem\">'"
        "+'<div style=\"font-size:.66rem;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.09em;color:'+color+';margin-bottom:.5rem\">'+label+'</div>'"
        "+items.map(function(item){"
        "if(typeof item==='string'){return '<div style=\"font-size:.78rem;color:#9ca3af;padding:.15rem 0\">• '+item+'</div>';}"
        "var a=item.action||'';var cgi=item.closes_gap_in||'';var ef=item.effort||'';"
        "var ei=item.expected_impact||'';var ww=item.why_this_works||'';"
        "return '<div style=\"border-bottom:1px solid #1a1a1a;padding:.45rem 0;margin-bottom:.3rem\">'"
        "+'<div style=\"display:flex;gap:.4rem;align-items:baseline;flex-wrap:wrap;margin-bottom:.2rem\">'"
        "+'<span style=\"font-size:.83rem;font-weight:600;color:#e8e8e8\">'+a+'</span>'"
        "+(ef?'<span style=\"font-size:.7rem;padding:.08rem .35rem;border-radius:4px;background:'+_ec(ef)+'22;color:'+_ec(ef)+'\">'+ef+'</span>':'')"
        "+(cgi?'<span style=\"font-size:.7rem;color:#555\">'+cgi+'</span>':'')"
        "+'</div>'"
        "+(ei?'<div style=\"font-size:.77rem;color:#22c55e\">→ '+ei+'</div>':'')"
        "+(ww?'<div style=\"font-size:.75rem;color:#555;font-style:italic\">'+ww+'</div>':'')"
        "+'</div>';"
        "}).join('')"
        "+'</div>';"
        "}"
        "h+='<div style=\"display:grid;grid-template-columns:1fr 1fr 1fr;gap:.7rem;margin:.75rem 0\">';"
        "h+=_actionCards(data['30_day_quick_wins'],'#22c55e','30 Days');"
        "h+=_actionCards(data['60_day_plays'],'#3b82f6','60 Days');"
        "h+=_actionCards(data['90_day_moat'],'#8b5cf6','90 Days');"
        "h+='</div>';"
        "var dwt=data.dont_waste_time_on||[];"
        "if(dwt.length){"
        "h+='<div style=\"background:#160808;border:1px solid #2a1010;border-radius:8px;"
        "padding:.85rem 1.05rem;margin-top:.5rem\">'"
        "+'<div style=\"font-size:.66rem;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.09em;color:#f87171;margin-bottom:.35rem\">✗ Don\\'t waste time on</div>'"
        "+dwt.map(function(d){return '<div style=\"font-size:.82rem;color:#fca5a5;padding:.18rem 0\">• '+d+'</div>';}).join('')"
        "+'</div>';"
        "}"
        "var ifc=data.if_competitor_does_this_respond_with||{};"
        "if(ifc.trigger&&ifc.response){"
        "h+='<div style=\"background:#081608;border:1px solid #1a3a1a;border-radius:8px;"
        "padding:.85rem 1.05rem;margin-top:.55rem\">'"
        "+'<div style=\"font-size:.66rem;font-weight:700;text-transform:uppercase;"
        "letter-spacing:.09em;color:#4ade80;margin-bottom:.35rem\">⚡ If Competitor Does This…</div>'"
        "+'<div style=\"font-size:.8rem;color:#9ca3af;margin-bottom:.3rem\"><span style=\"color:#fcd34d\">If:</span> '+ifc.trigger+'</div>'"
        "+'<div style=\"font-size:.8rem;color:#86efac\"><span style=\"color:#4ade80\">Respond:</span> '+ifc.response+'</div>'"
        "+'</div>';"
        "}"
        "return h;"
        "}"

        "document.addEventListener('DOMContentLoaded',function(){if(__cmpId)_loadSwot();});"
    )

    return (
        f"<script>"
        f"var __cmpId={cid};"
        f"var __la={la_js};"
        f"var __lb={lb_js};"
        f"{js}"
        f"</script>"
    )


# ── Main report generator ─────────────────────────────────────────────────────

def generate_compare_report(
    audit_a: dict,
    audit_b: dict,
    findings: dict,
    url_a: str,
    url_b: str,
    *,
    cache_hit_a: bool = False,
    cache_hit_b: bool = False,
    compare_id: Optional[int] = None,
    audit_id_a: Optional[int] = None,
    audit_id_b: Optional[int] = None,
    share_token: Optional[str] = None,
    brand_a_failed: bool = False,
    brand_b_failed: bool = False,
) -> str:
    """Render a full competitive comparison as a self-contained HTML string."""

    results_a = audit_a.get("results") or audit_a
    results_b = audit_b.get("results") or audit_b

    la = audit_a.get("brand_name") or _brand_name_from_url(url_a)
    lb = audit_b.get("brand_name") or _brand_name_from_url(url_b)

    sa = extract_dim_scores(results_a)
    sb = extract_dim_scores(results_b)
    oa = overall_score(sa)
    ob = overall_score(sb)

    generated_at = _now_ist()

    margin = abs(oa - ob)
    if margin < 0.15:
        verdict_html = "Too close to call — <strong>within 0.15 points</strong>"
        verdict_color_a = "#e8e8e8"
        verdict_color_b = "#e8e8e8"
    elif oa > ob:
        verdict_html = (f'<strong style="color:#60a5fa">{la}</strong> '
                        f'leads by <strong>{margin:.1f} pts</strong>')
        verdict_color_a = "#60a5fa"
        verdict_color_b = "#fcd34d"
    else:
        verdict_html = (f'<strong style="color:#fcd34d">{lb}</strong> '
                        f'leads by <strong>{margin:.1f} pts</strong>')
        verdict_color_a = "#60a5fa"
        verdict_color_b = "#fcd34d"

    cache_badges = ""
    badge_style = (
        "display:inline-flex;align-items:center;gap:.25rem;padding:.18rem .55rem;"
        "border-radius:5px;font-size:.7rem;font-weight:600;margin-right:.4rem"
    )
    if cache_hit_a:
        cache_badges += (
            f'<span style="{badge_style};background:rgba(59,130,246,.1);'
            f'color:#60a5fa;border:1px solid rgba(59,130,246,.25)">⚡ {la}: from cache</span>'
        )
    if cache_hit_b:
        cache_badges += (
            f'<span style="{badge_style};background:rgba(245,158,11,.1);'
            f'color:#fcd34d;border:1px solid rgba(245,158,11,.25)">⚡ {lb}: from cache</span>'
        )

    toolbar = _compare_toolbar_html(la, lb, audit_id_a, audit_id_b, share_token)
    radar = _radar_svg(sa, sb, la, lb)
    table = _score_table_html(sa, sb, oa, ob, la, lb, results_a, results_b)
    findings_section = _findings_html(findings, la, lb)
    dyn_js = _dynamic_js(compare_id, la, lb)

    btn_style = (
        "padding:.38rem .95rem;border-radius:7px;border:1px solid;font-size:.82rem;"
        "font-weight:600;cursor:pointer;transition:opacity .15s"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>{la} vs {lb} — Competitive Comparison</title>
<style>{_CSS}</style>
</head>
<body>
{toolbar}

<!-- ── REPORT HEADER ── -->
<header style="padding:2.5rem 0 1.75rem;border-bottom:1px solid var(--border);margin-bottom:2rem">
  <div style="display:flex;align-items:flex-start;justify-content:space-between;
    gap:2rem;flex-wrap:wrap">
    <div>
      <div style="font-size:.68rem;font-weight:700;text-transform:uppercase;
        letter-spacing:.1em;color:var(--muted);margin-bottom:.4rem">Competitive Comparison</div>
      <h1 style="font-size:1.85rem;font-weight:800;letter-spacing:-.4px;line-height:1.25">
        <span style="color:#60a5fa">{la}</span>
        <span style="color:var(--muted);font-weight:400;font-size:1.1rem;padding:0 .5rem">vs</span>
        <span style="color:#fcd34d">{lb}</span>
      </h1>
      <div style="font-size:.8rem;color:var(--muted);margin-top:.5rem;line-height:1.75">
        {generated_at}<br>
        <a href="{url_a}" target="_blank" style="color:#60a5fa;opacity:.8">{url_a}</a>
        <span style="color:#333;padding:0 .4rem">·</span>
        <a href="{url_b}" target="_blank" style="color:#fcd34d;opacity:.8">{url_b}</a>
      </div>
      {(f'<div style="margin-top:.65rem">{cache_badges}</div>') if cache_badges else ''}
      {''.join([
        f'<div style="margin-top:.55rem;display:inline-flex;align-items:center;gap:.4rem;'
        f'padding:.3rem .7rem;background:#160808;border:1px solid #3a1111;border-radius:7px;'
        f'font-size:.75rem;color:#fca5a5">⚠ {la} data unavailable — audit failed '
        f'(site may be blocking access). Scores shown are estimated defaults.</div>'
        if brand_a_failed else '',
        f'<div style="margin-top:.55rem;display:inline-flex;align-items:center;gap:.4rem;'
        f'padding:.3rem .7rem;background:#160808;border:1px solid #3a1111;border-radius:7px;'
        f'font-size:.75rem;color:#fca5a5">⚠ {lb} data unavailable — audit failed '
        f'(site may be blocking access). Scores shown are estimated defaults.</div>'
        if brand_b_failed else '',
      ])}
    </div>

    <!-- Verdict card -->
    <div style="background:var(--card);border:1px solid var(--border);border-radius:12px;
      padding:1.1rem 1.5rem;text-align:center;flex-shrink:0;min-width:170px">
      <div style="font-size:.65rem;font-weight:700;text-transform:uppercase;
        letter-spacing:.1em;color:var(--muted);margin-bottom:.35rem">Overall Verdict</div>
      <div style="font-size:.88rem;line-height:1.5;margin-bottom:.8rem">{verdict_html}</div>
      <div style="display:flex;gap:1rem;justify-content:center;align-items:flex-end">
        <div style="text-align:center">
          <div style="font-size:1.6rem;font-weight:800;color:{verdict_color_a};line-height:1">{oa:.1f}</div>
          <div style="font-size:.65rem;color:var(--muted);margin-top:.1rem">{la[:10]}</div>
        </div>
        <div style="color:#333;padding-bottom:.4rem">vs</div>
        <div style="text-align:center">
          <div style="font-size:1.6rem;font-weight:800;color:{verdict_color_b};line-height:1">{ob:.1f}</div>
          <div style="font-size:.65rem;color:var(--muted);margin-top:.1rem">{lb[:10]}</div>
        </div>
      </div>
    </div>
  </div>
</header>

<!-- ── SECTION 1: RADAR CHART ── -->
<div class="sec-hdr">
  <span class="sec-num">01</span>
  <span class="sec-title">Competitive Radar</span>
</div>
<div class="card" style="display:flex;justify-content:center;padding:1.75rem 1.25rem">
  {radar}
</div>

<!-- ── SECTION 2: SCORE TABLE ── -->
<div class="sec-hdr">
  <span class="sec-num">02</span>
  <span class="sec-title">Score Breakdown</span>
</div>
<div class="card" style="padding:0;overflow:hidden">
  {table}
</div>

<!-- ── SECTION 3: KEY FINDINGS ── -->
<div class="sec-hdr">
  <span class="sec-num">03</span>
  <span class="sec-title">Key Findings</span>
</div>
{findings_section}

<!-- ── SECTION 4: SWOT ANALYSIS (loaded via JS) ── -->
<div class="sec-hdr" id="swot-hdr" style="display:none">
  <span class="sec-num">04</span>
  <span class="sec-title">SWOT Analysis</span>
</div>
<div id="swot-section"></div>

<!-- ── SECTION 5: STRATEGY ── -->
<div class="sec-hdr" id="strategy-hdr" style="display:none">
  <span class="sec-num">05</span>
  <span class="sec-title">90-Day Strategy Battle Plan</span>
</div>
<div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:1.25rem">
  <div style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;margin-bottom:.5rem">
    <div style="font-size:.84rem;color:var(--muted)">Get 90-day strategy for:</div>
    <button id="btn-str-a" onclick="_loadStrategy('a')"
      style="{btn_style};color:#60a5fa;border-color:rgba(59,130,246,.4);background:rgba(59,130,246,.06)">
      {la[:20]} →
    </button>
    <button id="btn-str-b" onclick="_loadStrategy('b')"
      style="{btn_style};color:#fcd34d;border-color:rgba(245,158,11,.4);background:rgba(245,158,11,.06)">
      {lb[:20]} →
    </button>
  </div>
  <div id="strategy-content"></div>
</div>

<footer>
  Generated by Research Agent &nbsp;·&nbsp; {generated_at}<br>
  Scores derived from live brand audit data &nbsp;·&nbsp; Not financial advice
</footer>

{dyn_js}
</body>
</html>"""
