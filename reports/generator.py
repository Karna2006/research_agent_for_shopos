"""Compiles agent outputs into standalone HTML reports (no external deps)."""
from __future__ import annotations

import copy
import json
import logging
import re as _re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote as _url_quote

_log = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> str:
    """Current time formatted as IST."""
    return datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST")


def sanitize_text(text: str) -> str:
    """Replace predominantly non-ASCII text (regional scripts) with a placeholder."""
    if not text:
        return text
    non_ascii = sum(1 for c in text if ord(c) > 127)
    if non_ascii > len(text) * 0.3:
        return f"[Regional language content — {len(text)} chars]"
    return text

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = Path(__file__).parent / "templates"
REPORTS_DIR  = Path(__file__).parent / "output"

# ── Benchmark reference data (50+ Indian D2C brands, updated quarterly) ────────

BENCHMARKS: dict[str, dict] = {
    "pdp_quality":      {"category_avg": 5.8, "top_10_pct": 8.1, "label": "Indian D2C PDP Quality", "maxval": 10},
    "homepage":         {"category_avg": 6.1, "top_10_pct": 8.4, "label": "Homepage Score",          "maxval": 10},
    "geo_score":        {"category_avg": 38,  "top_10_pct": 72,  "label": "GEO Visibility Score",    "maxval": 100},
    "mobile_pagespeed": {"category_avg": 52,  "top_10_pct": 78,  "label": "Mobile PageSpeed",        "maxval": 100},
    "hook_strength":    {"category_avg": 5.2, "top_10_pct": 8.0, "label": "Ad Hook Strength",        "maxval": 10},
    "virality_score":   {"category_avg": 42,  "top_10_pct": 74,  "label": "Virality Score",          "maxval": 100},
}

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)

# ── Custom Jinja2 filters ──────────────────────────────────────────────────────

def _score_color(score) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "score-na"
    if s >= 7:
        return "score-green"
    if s >= 5:
        return "score-yellow"
    return "score-red"


def _score_hex(score, maxval: int = 10) -> str:
    """Return hex colour for a 0-maxval score."""
    try:
        s = float(score or 0)
        pct = s / maxval * 100
    except (TypeError, ValueError):
        return "#6b7280"
    if pct >= 70:
        return "#22c55e"
    if pct >= 50:
        return "#f59e0b"
    return "#ef4444"


def _ps_hex(score) -> str:
    """Return hex colour for a PageSpeed 0-100 score."""
    try:
        s = float(score or 0)
    except (TypeError, ValueError):
        return "#6b7280"
    if s >= 80:
        return "#22c55e"
    if s >= 50:
        return "#f59e0b"
    return "#ef4444"


def _pct_color(score) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "score-na"
    if s >= 70:
        return "score-green"
    if s >= 50:
        return "score-yellow"
    return "score-red"


def _grade_color(grade: str) -> str:
    g = (grade or "").upper()[:1]
    return {"S": "#f59e0b", "A": "#22c55e", "B": "#3b82f6",
            "C": "#eab308", "D": "#ef4444"}.get(g, "#6b7280")


def _safe_list(val) -> list:
    if isinstance(val, list):
        return val
    if isinstance(val, str) and val:
        return [val]
    return []


def _analysis(section: dict) -> dict:
    if not isinstance(section, dict):
        return {}
    return section.get("analysis") or {}


def _pill_class(score, maxval: int = 10) -> str:
    """Return pill CSS class based on score."""
    try:
        pct = float(score or 0) / maxval * 100
    except (TypeError, ValueError):
        return "pill-muted"
    if pct >= 70:
        return "pill-green"
    if pct >= 50:
        return "pill-amber"
    return "pill-red"


_env.filters["score_color"] = _score_color
_env.filters["score_hex"]   = _score_hex
_env.filters["ps_hex"]      = _ps_hex
_env.filters["pct_color"]   = _pct_color
_env.filters["grade_color"] = _grade_color
_env.filters["safe_list"]   = _safe_list
_env.filters["analysis"]    = _analysis
_env.filters["pill_class"]  = _pill_class
_env.globals["zip"]         = zip
_env.globals["min"]         = min
_env.globals["max"]         = max
_env.globals["round"]       = round
_env.globals["abs"]         = abs
_env.globals["int"]         = int


# ── Benchmark helpers (called from templates via | safe) ───────────────────────

def _bm_chip(bm_key: str, val) -> str:
    """Return a benchmark context HTML snippet for a score value."""
    bm = BENCHMARKS.get(bm_key)
    if not bm:
        return ""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return ""

    avg     = bm["category_avg"]
    top     = bm["top_10_pct"]
    maxval  = bm["maxval"]
    bottom25 = avg * 0.7  # inferred bottom-quartile threshold

    unit     = "/100" if maxval == 100 else "/10"
    avg_fmt  = f"{avg:.0f}" if avg >= 10 else f"{avg:.1f}"
    top_fmt  = f"{top:.0f}" if top >= 10 else f"{top:.1f}"

    if v >= top:
        badge, badge_color = "★ Top 10%", "#f59e0b"
    elif v >= avg:
        badge, badge_color = "↑ Above avg", "#22c55e"
    elif v >= bottom25:
        badge, badge_color = "↓ Below avg", "#f59e0b"
    else:
        badge, badge_color = "⚠ Needs urgent attention", "#ef4444"

    return (
        f'<div style="font-size:.67rem;color:#6b7280;margin-top:.28rem;line-height:1.5">'
        f'Avg&nbsp;{avg_fmt}{unit}&nbsp;·&nbsp;Top&nbsp;10%&nbsp;{top_fmt}{unit}'
        f'&nbsp;<span style="color:{badge_color};font-weight:700">{badge}</span>'
        f'</div>'
    )


_env.globals["bm_chip"]   = _bm_chip
_env.filters["tojson"]    = lambda v: json.dumps(v, ensure_ascii=False)


def _sparkline_svg(values: list[float], color: str = "#3b82f6", width: int = 80, height: int = 24) -> str:
    """Return an inline SVG polyline sparkline for a list of values (≥2 required)."""
    clean = [v for v in (values or []) if v is not None]
    if len(clean) < 2:
        return ""
    max_v = max(clean) or 1
    min_v = min(clean)
    span  = (max_v - min_v) or 1
    n     = len(clean)
    pts   = " ".join(
        f"{int(i * width / (n - 1))},{int(height - (v - min_v) / span * (height - 2) + 1)}"
        for i, v in enumerate(clean)
    )
    return (
        f'<svg width="{width}" height="{height}" style="vertical-align:middle;overflow:visible">'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
        f'</svg>'
    )


_env.globals["sparkline_svg"] = _sparkline_svg


def _worst_score_trigger(results: dict) -> str:
    """Return HTML for the worst benchmark-relative score, used in 'Why this?'."""
    checks = [
        ("mobile_pagespeed", _get(results, "store_cro",       "pagespeed", "mobile_score")),
        ("geo_score",        _get(results, "geo_visibility",  "analysis",  "geo_score")),
        ("pdp_quality",      _get(results, "content_catalog", "analysis",  "pdp_quality_score")),
        ("hook_strength",    _get(results, "performance_ads", "analysis",  "hook_strength_score")),
        ("homepage",         _get(results, "content_catalog", "analysis",  "homepage_score")),
    ]
    worst_ratio = 999.0
    worst_html = ""
    for bm_key, val in checks:
        bm = BENCHMARKS.get(bm_key)
        if not bm or val is None:
            continue
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        ratio = v / bm["category_avg"] if bm["category_avg"] > 0 else 999.0
        if ratio < worst_ratio:
            worst_ratio = ratio
            maxval   = bm["maxval"]
            unit     = "/100" if maxval == 100 else "/10"
            avg_fmt  = f"{bm['category_avg']:.0f}" if bm["category_avg"] >= 10 else f"{bm['category_avg']:.1f}"
            top_fmt  = f"{bm['top_10_pct']:.0f}"  if bm["top_10_pct"]  >= 10 else f"{bm['top_10_pct']:.1f}"
            worst_html = (
                f"Triggered by <strong>{bm['label']}</strong>: "
                f"<strong style=\"color:#ef4444\">{int(v)}{unit}</strong> &nbsp;·&nbsp; "
                f"Category avg: {avg_fmt}{unit} &nbsp;·&nbsp; Top 10%: {top_fmt}{unit}"
            )
    return worst_html


def _render_one_thing_banner(sentence: str, trigger: str = "") -> str:
    """Return the ⚡ highest-impact fix banner as an HTML string."""
    if not sentence:
        return ""

    why_block = ""
    if trigger:
        why_block = (
            f'<details style="margin-top:.7rem">'
            f'<summary style="font-size:.76rem;color:#7ab3e0;cursor:pointer;'
            f'list-style:none;display:inline-flex;align-items:center;gap:.3rem;'
            f'user-select:none">&#9654; Why this? &#8595;</summary>'
            f'<div style="margin-top:.45rem;font-size:.8rem;color:#92b4cc;line-height:1.55;'
            f'padding:.5rem .75rem;background:rgba(0,0,0,.25);border-radius:6px">'
            f'{trigger}</div>'
            f'</details>'
        )

    return (
        f'<div style="margin:1.5rem 0 2rem;background:#1e3a5f;'
        f'border-left:4px solid #2196f3;border-radius:10px;'
        f'padding:1.25rem 1.5rem">'
        f'<div style="font-size:.69rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.11em;color:#7ab3e0;margin-bottom:.55rem">'
        f'&#9889; This week\'s highest-impact fix</div>'
        f'<div style="font-size:1.13rem;font-weight:700;color:#ffffff;line-height:1.45">'
        f'{sentence}'
        f'</div>'
        f'{why_block}'
        f'</div>'
    )


# ── Score validation ───────────────────────────────────────────────────────────

def validate_scores(audit_data: dict) -> dict:
    """Clamp all scores to valid ranges and log out-of-range values.

    1-10 fields default to 5 when missing; 0-100 fields default to 42.
    Returns a deep copy — original is never mutated.
    """
    data    = copy.deepcopy(audit_data)
    results = data.get("results") or data

    # (section_key, analysis_sub_key, field_name, lo, hi, default)
    _fields: list[tuple[str, str, str, float, float, float]] = [
        # 1-10 section scores
        ("content_catalog", "analysis", "pdp_quality_score",      1, 10, 5),
        ("content_catalog", "analysis", "headline_clarity",        1, 10, 5),
        ("content_catalog", "analysis", "cta_clarity",             1, 10, 5),
        ("content_catalog", "analysis", "homepage_score",          1, 10, 5),
        ("content_catalog", "analysis", "hero_message_clarity",    1, 10, 5),
        ("performance_ads", "analysis", "hook_strength_score",     1, 10, 5),
        ("performance_ads", "analysis", "landing_page_match_score",1, 10, 5),
        ("performance_ads", "analysis", "cta_consistency",         1, 10, 5),
        ("store_cro",       "analysis", "cro_score",               1, 10, 5),
        ("research",        "analysis", "research_score",          1, 10, 5),
        # 0-100 composite scores
        ("geo_visibility",  "analysis", "geo_score",               0, 100, 42),
        ("store_cro",       "analysis", "pagespeed_mobile",        0, 100, 42),
        ("store_cro",       "analysis", "pagespeed_desktop",       0, 100, 42),
    ]

    for section, sub, field, lo, hi, default in _fields:
        sec = results.get(section)
        if not isinstance(sec, dict):
            continue
        ana = sec.get(sub)
        if not isinstance(ana, dict):
            continue
        raw = ana.get(field)
        if raw is None:
            continue
        try:
            v       = float(raw)
            clamped = max(lo, min(hi, v))
            if v != clamped:
                _log.warning("Score out of range: %s.%s=%s → clamped to %s",
                             section, field, v, clamped)
            ana[field] = clamped
        except (TypeError, ValueError):
            _log.warning("Non-numeric score: %s.%s=%r → defaulting to %s",
                         section, field, raw, default)
            ana[field] = default

    # Virality dimensions (0-10 each) + overall (0-100)
    virality = results.get("virality") or {}
    if isinstance(virality, dict):
        dims = virality.get("dimensions") or {}
        if isinstance(dims, dict):
            for dim_key, dim_val in dims.items():
                if isinstance(dim_val, dict) and "score" in dim_val:
                    raw = dim_val["score"]
                    try:
                        v = float(raw)
                        clamped = max(0.0, min(10.0, v))
                        if v != clamped:
                            _log.warning("Virality dim out of range: %s=%s → %s",
                                         dim_key, v, clamped)
                        dim_val["score"] = clamped
                    except (TypeError, ValueError):
                        dim_val["score"] = 5.0
        ovr = virality.get("overall_virality_score")
        if ovr is not None:
            try:
                v = float(ovr)
                virality["overall_virality_score"] = max(0.0, min(100.0, v))
            except (TypeError, ValueError):
                virality["overall_virality_score"] = 42.0

    return data


# ── Score history helpers ──────────────────────────────────────────────────────

def extract_native_scores(results: dict) -> dict:
    """Return native-unit scores for writing to ScoreHistory."""
    bb_a  = _get(results, "brand_basics",    "analysis") or {}
    cc_a  = _get(results, "content_catalog", "analysis") or {}
    pa_a  = _get(results, "performance_ads", "analysis") or {}
    geo_a = _get(results, "geo_visibility",  "analysis") or {}
    cro_a = _get(results, "store_cro",       "analysis") or {}
    res_a = _get(results, "research",        "analysis") or {}

    ps_wrap      = (results.get("store_cro") or {}).get("pagespeed") or {}
    mobile_score = ps_wrap.get("mobile_score") or cro_a.get("pagespeed_mobile")

    bb_fields  = ["brand_name", "brand_description", "logo_url", "contact_email", "social_links"]
    bb_present = sum(1 for f in bb_fields if bb_a.get(f))
    bb_score   = 5.0 + (bb_present / len(bb_fields)) * 5.0

    def _f(val):
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    res_raw = res_a.get("research_score") or res_a.get("overall_research_score")
    if res_raw is None:
        sub = [_f(res_a.get(k)) for k in ("market_position_score", "competitive_score", "trend_alignment_score")]
        sub = [v for v in sub if v is not None]
        res_raw = sum(sub) / len(sub) if sub else None

    return {
        "brand_basics_score": round(bb_score, 2),
        "content_score":      _f(cc_a.get("pdp_quality_score")),
        "ads_score":          _f(pa_a.get("hook_strength_score")),
        "geo_score":          _f(geo_a.get("geo_score")),
        "store_score":        _f(mobile_score),
        "research_score":     _f(res_raw),
    }


def _get_score_series(url: str, limit: int = 8) -> dict[str, list[float]]:
    """Return the last `limit` non-null values for each score field, oldest first."""
    try:
        from db.database import engine
        from db.models import ScoreHistory
        from sqlmodel import Session, select
    except Exception:
        return {}
    try:
        with Session(engine) as s:
            rows = s.exec(
                select(ScoreHistory)
                .where(ScoreHistory.brand_url == url)
                .order_by(ScoreHistory.timestamp.desc())
                .limit(limit)
            ).all()
    except Exception:
        return {}
    if len(rows) < 2:
        return {}

    rows = list(reversed(rows))  # chronological order
    fields = [
        ("content",  "content_score"),
        ("ads",      "ads_score"),
        ("geo",      "geo_score"),
        ("store",    "store_score"),
        ("research", "research_score"),
        ("overall",  "overall_score"),
    ]
    series: dict[str, list[float]] = {}
    for key, attr in fields:
        vals = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
        if len(vals) >= 2:
            series[key] = vals
    return series


def _get_score_trends(url: str) -> dict:
    """Return delta dict comparing the last two ScoreHistory rows for this URL."""
    try:
        from db.database import engine
        from db.models import ScoreHistory
        from sqlmodel import Session, select
    except Exception:
        return {}

    try:
        with Session(engine) as s:
            rows = s.exec(
                select(ScoreHistory)
                .where(ScoreHistory.brand_url == url)
                .order_by(ScoreHistory.timestamp.desc())
                .limit(2)
            ).all()
    except Exception:
        return {}

    if len(rows) < 2:
        return {}

    curr, prev = rows[0], rows[1]
    delta_days = max(0, (curr.timestamp - prev.timestamp).days)

    trends: dict = {}
    fields = [
        ("brand_basics", "brand_basics_score", 10),
        ("content",      "content_score",      10),
        ("ads",          "ads_score",           10),
        ("geo",          "geo_score",           100),
        ("store",        "store_score",         100),
        ("research",     "research_score",      10),
        ("overall",      "overall_score",       100),
    ]
    for key, attr, unit in fields:
        c = getattr(curr, attr)
        p = getattr(prev, attr)
        if c is not None and p is not None:
            trends[key] = {"delta": round(c - p, 2), "days_ago": delta_days, "unit": unit}

    return trends


def _render_trend_chip(delta: float, days_ago: int, unit: int) -> str:
    """Return a small inline HTML trend chip: green ↑, red ↓, grey →."""
    threshold = 0.2 if unit == 10 else 2
    if abs(delta) < threshold:
        arrow, color = "→", "#6b7280"
    elif delta > 0:
        arrow, color = "↑", "#22c55e"
    else:
        arrow, color = "↓", "#ef4444"

    sign       = "+" if delta > 0 else ""
    days_label = f"{days_ago}d ago" if days_ago > 0 else "just now"
    return (
        f'<span style="display:inline-flex;align-items:center;gap:.2rem;'
        f'font-size:.68rem;font-weight:700;color:{color};'
        f'background:{color}20;border-radius:4px;padding:.1rem .38rem;'
        f'margin-left:.45rem;vertical-align:middle" '
        f'title="vs {days_label}">'
        f'{arrow}&nbsp;{sign}{delta:g}'
        f'</span>'
    )


def _render_changes_banner(changes: dict) -> str:
    """Return 'Since Last Audit' change detection banner HTML, or empty string."""
    if not changes:
        return ""
    summary      = changes.get("summary", "")
    improvements = changes.get("improvements") or []
    regressions  = changes.get("regressions")  or []
    days_ago     = changes.get("days_ago", 0)

    if not summary and not improvements and not regressions:
        return ""

    days_label = f"{days_ago} day{'s' if days_ago != 1 else ''} ago" if days_ago else "last audit"

    items_html = ""
    for item in improvements[:3]:
        items_html += (
            f'<div style="display:flex;gap:.4rem;margin-bottom:.3rem">'
            f'<span style="color:#22c55e;flex-shrink:0">↑</span>'
            f'<span style="font-size:.82rem;color:#a8c8a0">{item}</span></div>'
        )
    for item in regressions[:3]:
        items_html += (
            f'<div style="display:flex;gap:.4rem;margin-bottom:.3rem">'
            f'<span style="color:#ef4444;flex-shrink:0">↓</span>'
            f'<span style="font-size:.82rem;color:#c8a8a0">{item}</span></div>'
        )

    summary_html = (
        f'<div style="font-size:.88rem;color:#cbd5e1;line-height:1.5;margin-bottom:.55rem">'
        f'{summary}</div>'
    ) if summary else ""

    return (
        f'<div style="margin:1rem 0 1.5rem;background:#1a2535;'
        f'border-left:4px solid #475569;border-radius:10px;padding:1rem 1.25rem">'
        f'<div style="font-size:.68rem;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:.11em;color:#64748b;margin-bottom:.5rem">'
        f'&#128259; Since last audit ({days_label})</div>'
        f'{summary_html}{items_html}'
        f'</div>'
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def _render_agentic_brain_section(audit_data: dict) -> str:
    """Build the Reasoning Brain panel HTML for injection into the audit report.

    Shows: pattern badge, executive narrative, signal grid, cross-insights,
    collapsible reasoning trace and decisions log.
    Only rendered when agentic_meta is present in the audit data.
    """
    meta = audit_data.get("agentic_meta") or {}
    signals = audit_data.get("signals") or []
    cross_insights = audit_data.get("cross_insights") or []
    trace = audit_data.get("reasoning_trace") or []
    decisions = audit_data.get("decisions") or []

    if not meta and not signals:
        return ""

    # Pattern badge
    pattern = meta.get("pattern") or audit_data.get("pattern_detected")
    posture = meta.get("posture") or "optimize"
    pattern_colors = {
        "invisible_brand":       ("#ef4444", "Invisible Brand"),
        "ghost_advertiser":      ("#f97316", "Ghost Advertiser"),
        "social_darling":        ("#a855f7", "Social Darling"),
        "great_product_bad_store": ("#f59e0b", "Great Product, Bad Store"),
        "ai_search_gap":         ("#3b82f6", "AI Search Gap"),
        "conversion_crisis":     ("#ef4444", "Conversion Crisis"),
        "hidden_gem":            ("#22c55e", "Hidden Gem"),
    }
    posture_colors = {
        "triage": "#ef4444", "optimize": "#f59e0b",
        "accelerate": "#22c55e", "defend": "#3b82f6",
    }
    p_color, p_label = pattern_colors.get(pattern or "", ("#6b7280", pattern or ""))
    pos_color = posture_colors.get(posture, "#6b7280")

    pattern_badge = ""
    if pattern:
        pattern_badge = (
            f'<span style="display:inline-flex;align-items:center;gap:.3rem;'
            f'padding:.22rem .75rem;border-radius:999px;font-size:.73rem;font-weight:700;'
            f'background:{p_color}22;color:{p_color};border:1px solid {p_color}55;'
            f'margin-right:.5rem">'
            f'◈ {p_label}</span>'
        )
    posture_badge = (
        f'<span style="display:inline-flex;align-items:center;'
        f'padding:.22rem .75rem;border-radius:999px;font-size:.73rem;font-weight:700;'
        f'background:{pos_color}22;color:{pos_color};border:1px solid {pos_color}55">'
        f'{posture.capitalize()}</span>'
    )

    # Narrative
    narrative = meta.get("narrative", "")
    core_challenge = meta.get("core_challenge", "")
    root_cause = meta.get("root_cause", "")
    hidden_opp = meta.get("hidden_opportunity", "")
    contradictions = meta.get("contradictions") or []
    confidence = meta.get("confidence", "medium")
    conf_color = {"high": "#22c55e", "medium": "#f59e0b", "low": "#ef4444"}.get(confidence, "#6b7280")

    narrative_html = ""
    if narrative:
        narrative_html = (
            f'<p style="font-size:.9rem;color:#cbd5e1;line-height:1.75;'
            f'padding:.85rem 1.1rem;background:#0d1f3c;border-radius:8px;'
            f'border-left:3px solid #3b82f6;margin:.85rem 0 0">{narrative}</p>'
        )

    meta_grid_items = []
    if core_challenge:
        meta_grid_items.append(("Core Challenge", core_challenge, "#ef4444"))
    if root_cause:
        meta_grid_items.append(("Root Cause", root_cause, "#f97316"))
    if hidden_opp:
        meta_grid_items.append(("Hidden Opportunity", hidden_opp, "#22c55e"))
    if contradictions:
        meta_grid_items.append(("Contradiction", contradictions[0], "#a855f7"))

    meta_grid_html = ""
    if meta_grid_items:
        items = ""
        for label, text, color in meta_grid_items:
            items += (
                f'<div style="background:#111;border:1px solid #1e1e1e;border-left:3px solid {color};'
                f'border-radius:8px;padding:.75rem 1rem">'
                f'<div style="font-size:.62rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.09em;color:{color};margin-bottom:.3rem">{label}</div>'
                f'<div style="font-size:.82rem;color:#e2e8f0;line-height:1.55">{text}</div>'
                f'</div>'
            )
        meta_grid_html = (
            f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));'
            f'gap:.65rem;margin-top:.85rem">{items}</div>'
        )

    # Signal grid
    sev_colors = {"critical": "#ef4444", "high": "#f97316", "medium": "#f59e0b", "low": "#6b7280"}
    type_icons = {"risk": "⚠", "opportunity": "◆", "anomaly": "◉", "confirmation": "✓"}

    signals_html = ""
    if signals:
        sig_items = ""
        for s in signals:
            sc = sev_colors.get(s.get("severity", "medium"), "#6b7280")
            ic = type_icons.get(s.get("type", "risk"), "•")
            action = s.get("triggers_action") or ""
            action_tag = (
                f'<span style="font-size:.63rem;padding:.1rem .38rem;border-radius:4px;'
                f'background:#1e1e1e;color:#6b7280;margin-top:.25rem;display:inline-block">'
                f'→ {action}</span>'
            ) if action else ""
            sig_items += (
                f'<div style="background:#111;border:1px solid #1e1e1e;border-left:3px solid {sc};'
                f'border-radius:8px;padding:.6rem .9rem">'
                f'<div style="display:flex;align-items:center;gap:.4rem;margin-bottom:.18rem">'
                f'<span style="font-size:.68rem;font-weight:700;color:{sc};text-transform:uppercase">'
                f'{s.get("severity","").upper()}</span>'
                f'<span style="font-size:.68rem;color:#4b5563">{s.get("source","")}</span>'
                f'</div>'
                f'<div style="font-size:.82rem;color:#e2e8f0;font-weight:500;line-height:1.4">'
                f'{ic} {s.get("content","")}</div>'
                f'<div style="font-size:.72rem;color:#6b7280;margin-top:.18rem">{s.get("evidence","")}</div>'
                f'{action_tag}'
                f'</div>'
            )
        signals_html = (
            f'<div style="margin-top:1rem">'
            f'<div style="font-size:.63rem;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.09em;color:#6b7280;margin-bottom:.55rem">'
            f'Agentic Signals · {len(signals)} detected</div>'
            f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));'
            f'gap:.5rem">{sig_items}</div>'
            f'</div>'
        )

    # Cross-insights
    cross_html = ""
    if cross_insights:
        items = "".join(
            f'<div style="font-size:.82rem;color:#93c5fd;line-height:1.6;'
            f'padding:.35rem 0;border-bottom:1px solid #1e1e1e">'
            f'<span style="color:#3b82f6;margin-right:.4rem">◈</span>{ci}'
            f'</div>'
            for ci in cross_insights
        )
        cross_html = (
            f'<div style="margin-top:1rem;padding:.85rem 1rem;background:#0c1829;'
            f'border:1px solid #1e3a5f;border-radius:8px">'
            f'<div style="font-size:.63rem;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.09em;color:#3b82f6;margin-bottom:.5rem">Cross-Agent Insights</div>'
            f'{items}'
            f'</div>'
        )

    # Reasoning trace (collapsible)
    trace_html = ""
    if trace:
        trace_lines = "".join(
            f'<div style="font-size:.73rem;color:#4b5563;font-family:monospace;'
            f'padding:.18rem 0;border-bottom:1px solid #111;line-height:1.4">{t}</div>'
            for t in trace
        )
        trace_html = (
            f'<details style="margin-top:.85rem">'
            f'<summary style="font-size:.73rem;font-weight:700;color:#6b7280;'
            f'cursor:pointer;list-style:none;padding:.4rem 0">'
            f'▸ Reasoning Trace ({len(trace)} steps)</summary>'
            f'<div style="margin-top:.5rem;padding:.75rem;background:#080808;'
            f'border:1px solid #1e1e1e;border-radius:6px;max-height:280px;overflow-y:auto">'
            f'{trace_lines}</div></details>'
        )

    # Decisions log (collapsible)
    decisions_html = ""
    if decisions:
        d_rows = "".join(
            f'<div style="display:flex;gap:.75rem;padding:.35rem 0;'
            f'border-bottom:1px solid #111;align-items:flex-start">'
            f'<span style="font-size:.65rem;color:#f59e0b;font-family:monospace;'
            f'flex-shrink:0;margin-top:.05rem">{d.get("at_seconds","?")}s</span>'
            f'<div><span style="font-size:.73rem;color:#e2e8f0;font-weight:600">'
            f'{d.get("step","")}</span>'
            f'<div style="font-size:.71rem;color:#6b7280;margin-top:.1rem">'
            f'{d.get("rationale","")} → <em style="color:#a0a0a0">{d.get("action","")}</em>'
            f'</div></div></div>'
            for d in decisions
        )
        decisions_html = (
            f'<details style="margin-top:.5rem">'
            f'<summary style="font-size:.73rem;font-weight:700;color:#6b7280;'
            f'cursor:pointer;list-style:none;padding:.4rem 0">'
            f'▸ Brain Decisions ({len(decisions)} calls)</summary>'
            f'<div style="margin-top:.5rem;padding:.75rem;background:#080808;'
            f'border:1px solid #1e1e1e;border-radius:6px;max-height:260px;overflow-y:auto">'
            f'{d_rows}</div></details>'
        )

    return f"""
<div style="margin:2rem 1rem 0;font-family:system-ui,sans-serif">
  <details class="section-accordion" open>
    <summary class="section-header"
      style="background:linear-gradient(135deg,#0a0a1a,#0d1f3c)">
      <span class="section-num" style="color:#3b82f6">⬡</span>
      <span class="section-title">Agentic Reasoning Brain</span>
      <span class="section-score-badge" style="color:#3b82f6;border-color:#1e3a5f">
        {len(signals)} signals · {len(decisions)} decisions
        &nbsp;<span style="color:{conf_color}">{confidence} confidence</span>
      </span>
      <span class="accordion-arrow">⌄</span>
    </summary>
    <div class="section-body">
      <div style="display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;margin-bottom:.65rem">
        {pattern_badge}{posture_badge}
      </div>
      {narrative_html}
      {meta_grid_html}
      {signals_html}
      {cross_html}
      {trace_html}
      {decisions_html}
      <div style="font-size:.62rem;color:#374151;margin-top:.85rem;border-top:1px solid #1e1e1e;padding-top:.5rem">
        ReAct pattern (Reason → Act → Observe → Synthesize) ·
        {len(trace)} trace steps · {len(cross_insights)} cross-agent insights
      </div>
    </div>
  </details>
</div>"""


def _failed_section_placeholder(agent_key: str) -> str:
    label = _SECTION_LABELS.get(agent_key, agent_key)
    return (
        '<details class="section-accordion" open>'
        '<summary class="section-header" style="cursor:default">'
        f'<span class="section-title">{label}</span>'
        '<span class="section-score-badge" style="color:#6b7280">—</span>'
        '</summary>'
        '<div class="section-body" style="padding:1.25rem 1.5rem;color:#6b7280;font-size:.87rem">'
        '&#9888; This agent did not receive input or timed out — no data available for this section.'
        '</div></details>'
    )


def generate_audit_report(audit_data: dict) -> str:
    """Render the full 6-section audit report as an HTML string."""
    audit_data = validate_scores(audit_data)
    ctx = _build_audit_context(audit_data)
    template = _env.get_template("audit_report.html")
    html = template.render(**ctx)

    # Replace sections for failed/timed-out agents with a placeholder
    results = audit_data.get("results") or audit_data
    for key, result in results.items():
        if isinstance(result, dict) and result.get("status") in ("timeout", "failed"):
            placeholder = _failed_section_placeholder(key)
            section_pattern = rf'<!--\s*SECTION:{_re.escape(key)}\s*-->.*?<!--\s*/SECTION:{_re.escape(key)}\s*-->'
            replacement = f'<!-- SECTION:{key} -->{placeholder}<!-- /SECTION:{key} -->'
            html = _re.sub(section_pattern, replacement, html, flags=_re.DOTALL)

    # Inject agentic brain section before the standard footer callout
    agentic_brain_html = _render_agentic_brain_section(audit_data)
    if agentic_brain_html:
        html = html.replace("</body>", agentic_brain_html + "\n</body>", 1)

    # Inject "Skip the Re-audit" callout + sources panel before </body>
    skip_reaudit_html = """
<div style="margin:2rem 1rem 0;font-family:system-ui,sans-serif">
  <div style="background:#0b1220;border:1px solid #1e3a5f;border-left:3px solid #3b82f6;
    border-radius:10px;padding:1.1rem 1.4rem">
    <p style="margin:0;font-size:.84rem;color:#93c5fd;line-height:1.65;font-style:italic">
      <strong style="font-style:normal;color:#60a5fa">💡 You don't need to re-audit after every fix.</strong><br>
      Each recommendation above shows its expected impact.
      Implement 3–5 fixes, then re-audit in <strong style="color:#93c5fd">4–6 weeks</strong> to see
      compounded improvement. Re-auditing sooner than that won't reflect changes that need
      indexing time — especially schema markup, GEO signals, and SEO changes.
    </p>
  </div>
</div>"""

    sources_html = _render_sources_panel(
        audit_data.get("results", {}),
        brand_url=audit_data.get("url", ""),
        brand_name=ctx.get("brand_name", ""),
    )
    inject = skip_reaudit_html
    if sources_html:
        inject += "\n" + sources_html
    html = html.replace("</body>", inject + "\n</body>", 1)
    return html


def generate_virality_card(virality_data: dict) -> str:
    """Render the virality score card as an HTML string."""
    ctx = _build_virality_context(virality_data)
    template = _env.get_template("virality_card.html")
    return template.render(**ctx)


def save_report(html: str, filename: str) -> str:
    """Write html to reports/output/{filename} and return the absolute path."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / filename
    out_path.write_text(html, encoding="utf-8")
    return str(out_path.resolve())


# ── Internal helpers ───────────────────────────────────────────────────────────

def _get(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None:
            return default
    return cur


def _try_int(val, default: int = 0) -> int:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _overall_health(results: dict) -> int:
    """Weighted composite score (0-100):
    content 25% · store 20% · geo 20% · ads 15% · research 10% · brand_basics 10%
    """
    cc_a  = _get(results, "content_catalog", "analysis") or {}
    pa_a  = _get(results, "performance_ads", "analysis") or {}
    geo_a = _get(results, "geo_visibility",  "analysis") or {}
    bb_a  = _get(results, "brand_basics",    "analysis") or {}
    res_a = _get(results, "research",        "analysis") or {}

    cro_wrap = results.get("store_cro", {}) or {}
    ps       = cro_wrap.get("pagespeed", {}) or {}
    cro_a    = cro_wrap.get("analysis", {})  or {}
    mobile   = ps.get("mobile_score") or cro_a.get("pagespeed_mobile")

    # brand_basics: synthesised 0-100 from field presence
    _bb_fields = ["brand_name", "founding_year", "founders", "hq", "core_categories",
                  "target_audience", "key_strengths"]
    bb_present = sum(1 for f in _bb_fields if bb_a.get(f))
    bb100      = 50.0 + (bb_present / len(_bb_fields)) * 50.0

    def _to100(val, ten_scale: bool) -> float | None:
        if val is None:
            return None
        try:
            v = float(val)
            return v * 10.0 if ten_scale else v
        except (TypeError, ValueError):
            return None

    parts = [
        (_to100(cc_a.get("pdp_quality_score"),   True),  0.25),
        (_to100(mobile,                           False), 0.20),
        (_to100(geo_a.get("geo_score"),           False), 0.20),
        (_to100(pa_a.get("hook_strength_score"),  True),  0.15),
        (_to100(res_a.get("research_score"),      True),  0.10),
        (bb100,                                           0.10),
    ]

    total = 0.0
    total_w = 0.0
    for val, w in parts:
        if val is None:
            continue
        total   += max(0.0, min(100.0, val)) * w
        total_w += w

    return round(total / total_w) if total_w > 0 else 0


def _top_wins_gaps(results: dict) -> tuple[list[str], list[str]]:
    wins: list[str] = []
    gaps: list[str] = []

    bb = _get(results, "brand_basics", "analysis") or {}
    for s in _safe_list(bb.get("key_strengths"))[:2]:
        wins.append(s)

    cc = _get(results, "content_catalog", "analysis") or {}
    for s in _safe_list(cc.get("pdp_strengths"))[:1]:
        wins.append(s)
    for s in _safe_list(cc.get("pdp_weaknesses"))[:2]:
        gaps.append(s)
    for s in _safe_list(cc.get("top_3_improvements"))[:1]:
        gaps.append(s)

    geo = _get(results, "geo_visibility", "analysis") or {}
    for s in _safe_list(geo.get("schema_missing"))[:1]:
        gaps.append(f"Missing schema: {s}")

    cro = _get(results, "store_cro", "analysis") or {}
    fixes = _safe_list(cro.get("top_5_cro_fixes"))
    if fixes:
        first = fixes[0]
        if isinstance(first, dict):
            gaps.append(first.get("fix", ""))
        elif isinstance(first, str):
            gaps.append(first)

    return wins[:3], gaps[:3]


def _citation_status(idx: int, likelihood: str) -> str:
    """Map topic index + overall likelihood to per-row status string."""
    lk = (likelihood or "").lower()
    if lk == "high":
        return "yes"
    if lk == "medium":
        return "yes" if idx == 0 else ("partial" if idx < 3 else "no")
    return "no"


# ── Context builders ───────────────────────────────────────────────────────────

def _build_audit_context(audit_data: dict) -> dict:  # noqa: C901
    results = audit_data.get("results") or audit_data

    brand_basics      = results.get("brand_basics",      {}) or {}
    content_catalog   = results.get("content_catalog",   {}) or {}
    performance_ads   = results.get("performance_ads",   {}) or {}
    geo_visibility    = results.get("geo_visibility",    {}) or {}
    store_cro         = results.get("store_cro",         {}) or {}
    research          = results.get("research",          {}) or {}
    social_profile    = results.get("social_profile",    {}) or {}
    social_media_audit = results.get("social_media_audit", {}) or {}

    bb_a  = brand_basics.get("analysis")    or {}
    cc_a  = content_catalog.get("analysis") or {}
    pa_a  = performance_ads.get("analysis") or {}
    geo_a = geo_visibility.get("analysis")  or {}
    cro_a = store_cro.get("analysis")       or {}
    res_a = research.get("analysis")        or {}
    sp_a  = social_profile.get("analysis")  or {}

    health = _overall_health(results)
    wins, gaps = _top_wins_gaps(results)

    # ── Pagespeed: prefer wrapper, fall back to analysis fields ───────────────
    ps_wrap = store_cro.get("pagespeed") or {}
    mobile_score  = _try_int(ps_wrap.get("mobile_score")  or cro_a.get("pagespeed_mobile"),  0)
    desktop_score = _try_int(ps_wrap.get("desktop_score") or cro_a.get("pagespeed_desktop"), 0)
    cwv   = cro_a.get("core_web_vitals") or {}
    lcp   = cwv.get("lcp")  or ps_wrap.get("lcp",  "—")
    cls   = cwv.get("cls")  or ps_wrap.get("cls",  "—")
    inp   = cwv.get("fid")  or cwv.get("inp") or ps_wrap.get("fid", "—")

    # ── Ad format breakdown bars ──────────────────────────────────────────────
    fmt = pa_a.get("creative_format_breakdown") or {}
    ad_formats = [
        {"label": "Video",    "pct": _try_int(fmt.get("video_pct"),    0), "color": "#3b82f6"},
        {"label": "Static",   "pct": _try_int(fmt.get("static_pct"),   0), "color": "#22c55e"},
        {"label": "Carousel", "pct": _try_int(fmt.get("carousel_pct"), 0), "color": "#f59e0b"},
        {"label": "UGC",      "pct": _try_int(fmt.get("ugc_pct"),      0), "color": "#a78bfa"},
    ]

    # ── Schema checklist ──────────────────────────────────────────────────────
    KNOWN_SCHEMAS = [
        "Organization", "WebSite", "BreadcrumbList", "Product", "Offer",
        "FAQPage", "Review", "AggregateRating", "Article", "HowTo",
    ]
    found_set   = set(geo_a.get("schema_types_found") or [])
    missing_set = set(geo_a.get("schema_missing") or [])
    schema_checklist = []
    for s in KNOWN_SCHEMAS:
        if s in found_set:
            status = "found"
        elif s in missing_set:
            status = "missing"
        else:
            status = "unknown"
        schema_checklist.append({"name": s, "status": status})

    # ── GEO citation table ────────────────────────────────────────────────────
    topics     = geo_a.get("top_5_content_topics_for_ai_citation") or []
    likelihood = geo_a.get("ai_citation_likelihood") or ""
    geo_citation_rows = [
        {"query": t, "status": _citation_status(i, likelihood)}
        for i, t in enumerate(topics)
    ]

    # ── Health colour ─────────────────────────────────────────────────────────
    health_color = (
        "#22c55e" if health >= 75 else
        "#f59e0b" if health >= 50 else
        "#ef4444"
    )

    # ── Funnel coverage ───────────────────────────────────────────────────────
    funnel = pa_a.get("funnel_coverage") or {}

    # ── Sample ad headlines ───────────────────────────────────────────────────
    ads_scrape = performance_ads.get("ads_scrape") or {}
    sample_headlines = [sanitize_text(h) for h in (ads_scrape.get("sample_headlines") or [])]

    # ── Market forecast (from research agent TrendPredictor) ─────────────────
    market_forecast   = research.get("market_forecast") or {}
    price_trend       = market_forecast.get("price_trend") or {}
    review_velocity   = market_forecast.get("review_velocity") or {}

    # Build sparkline: list of 0-100 values representing historical + predicted
    sparkline_raw = review_velocity.get("weekly_sparkline") or []
    hist_len      = 12  # we always use 12-week history in benchmarks
    spark_hist    = sparkline_raw[:hist_len]
    spark_pred    = sparkline_raw[hist_len:]

    # ── One Thing banner ──────────────────────────────────────────────────────
    one_thing_sentence = audit_data.get("one_thing", "")
    one_thing_trigger  = _worst_score_trigger(results) if one_thing_sentence else ""
    one_thing_banner   = _render_one_thing_banner(one_thing_sentence, one_thing_trigger)

    # ── Score trend chips + sparklines (vs previous audits) ──────────────────
    audit_url = audit_data.get("url", "")
    _trends   = _get_score_trends(audit_url) if audit_url else {}
    _series   = _get_score_series(audit_url) if audit_url else {}

    # Pre-render sparkline SVGs for each scored section
    _spark_color = {
        "content":  "#22c55e",
        "ads":      "#a78bfa",
        "geo":      "#3b82f6",
        "store":    "#f59e0b",
        "research": "#ec4899",
        "overall":  "#ffffff",
    }
    sparklines: dict[str, str] = {
        key: _sparkline_svg(vals, color=_spark_color.get(key, "#3b82f6"))
        for key, vals in _series.items()
    }

    # Delta labels for health pulse ("↑ +24 pts since first audit")
    pulse_labels: dict[str, str] = {}
    for key, vals in _series.items():
        if len(vals) >= 2:
            delta = round(vals[-1] - vals[0], 1)
            sign  = "+" if delta >= 0 else ""
            color = "#22c55e" if delta > 0 else ("#ef4444" if delta < 0 else "#6b7280")
            pulse_labels[key] = (
                f'<span style="font-size:.68rem;color:{color};font-weight:700">'
                f'{sign}{delta:g} pts since first audit</span>'
            )

    def _chip(key: str) -> str:
        t = _trends.get(key)
        return _render_trend_chip(t["delta"], t["days_ago"], t["unit"]) if t else ""

    # ── Changes banner (LLM-generated diff from previous audit) ───────────────
    changes_raw    = audit_data.get("changes_summary")
    changes_parsed = {}
    if changes_raw:
        try:
            changes_parsed = json.loads(changes_raw) if isinstance(changes_raw, str) else changes_raw
        except (ValueError, TypeError):
            pass
    changes_banner = _render_changes_banner(changes_parsed)

    # ── 30-Day Roadmap ────────────────────────────────────────────────────────
    roadmap_raw = audit_data.get("roadmap") or audit_data.get("roadmap_json")
    roadmap: dict = {}
    if roadmap_raw:
        try:
            roadmap = json.loads(roadmap_raw) if isinstance(roadmap_raw, str) else roadmap_raw
        except (ValueError, TypeError):
            pass

    # ── Whitespace Score (from research agent) ────────────────────────────────
    whitespace = (results.get("research") or {}).get("whitespace") or {}

    # ── 10-Dimension Scorecard ────────────────────────────────────────────────
    def _to_score(val, maxval=10):
        try:
            v = float(val or 0)
            return round(v / maxval * 100)
        except (TypeError, ValueError):
            return None

    _sma_scores = (social_media_audit.get("scores") or {})
    _sma_overall = _try_int(_sma_scores.get("overall"), 0) * 10  # 1-10 → 0-100

    _bb_fields_present = sum(1 for f in ["brand_name", "founding_year", "founders", "hq",
                                          "core_categories", "target_audience", "key_strengths"]
                             if bb_a.get(f))
    _brand_pos_score = round(50 + (_bb_fields_present / 7) * 30 + min(20, _try_int(res_a.get("research_score"), 5) * 2))

    _ux_score_raw = cro_a.get("ux_score") or cro_a.get("cro_score")
    _ux_score = _to_score(_ux_score_raw, 10)

    scorecard_10 = [
        {
            "dim": "Brand Positioning",
            "score": min(100, _brand_pos_score),
            "note": bb_a.get("brand_positioning") or res_a.get("brand_positioning_vs_market", "")[:80],
            "icon": "◈",
        },
        {
            "dim": "Website UX",
            "score": _ux_score,
            "note": (cro_a.get("ux_audit") or {}).get("hero_cta_clarity", "") or
                    (cro_a.get("funnel_friction_points") or [""])[0],
            "icon": "⬡",
        },
        {
            "dim": "Mobile Performance",
            "score": _try_int(ps_wrap.get("mobile_score") or cro_a.get("pagespeed_mobile"), 0) or None,
            "note": f"LCP {lcp} · CLS {cls}" if lcp and cls else "",
            "icon": "⚡",
        },
        {
            "dim": "Content Quality",
            "score": _to_score(cc_a.get("pdp_quality_score"), 10),
            "note": (cc_a.get("pdp_weaknesses") or [""])[0],
            "icon": "✦",
        },
        {
            "dim": "SEO & Discoverability",
            "score": _try_int(geo_a.get("geo_score"), 0) or None,
            "note": geo_a.get("ai_citation_likelihood_reason", "")[:80],
            "icon": "⊕",
        },
        {
            "dim": "Social Presence",
            "score": _to_score(sp_a.get("social_presence_score") or
                               social_profile.get("social_presence_score"), 10),
            "note": (sp_a.get("instagram") or {}).get("followers", "") or "",
            "icon": "◉",
        },
        {
            "dim": "Social Content Quality",
            "score": _to_score(_sma_scores.get("content_quality"), 10),
            "note": (social_media_audit.get("top_3_strengths") or [""])[0],
            "icon": "▲",
        },
        {
            "dim": "Paid Ads Strength",
            "score": _to_score(pa_a.get("hook_strength_score"), 10),
            "note": pa_a.get("best_performing_creative_type", "")[:80],
            "icon": "◆",
        },
        {
            "dim": "Conversion Optimization",
            "score": _to_score(cro_a.get("cro_score"), 10),
            "note": ((cro_a.get("top_5_cro_fixes") or [{}])[0] or {}).get("fix", "")[:80],
            "icon": "↗",
        },
        {
            "dim": "Competitive Position",
            "score": _to_score(res_a.get("research_score"), 10),
            "note": (res_a.get("where_brand_wins") or [""])[0],
            "icon": "⚔",
        },
    ]
    # Filter None scores to avoid rendering broken cards
    for card in scorecard_10:
        if card["score"] is None:
            card["score"] = 50  # default neutral when data unavailable
        else:
            card["score"] = max(0, min(100, card["score"]))

    scorecard_overall = round(sum(c["score"] for c in scorecard_10) / len(scorecard_10))

    # ── Priority Framework (🔴/🟡/🟢 recommendations) ────────────────────────
    def _collect_all_recs(results_dict: dict) -> list[dict]:
        recs = []
        for section_key, rec_field in [
            ("content_catalog",  "top_3_improvements"),
            ("geo_visibility",   "geo_improvement_roadmap"),
            ("store_cro",        "top_5_cro_fixes"),
            ("performance_ads",  "top_3_ad_quick_wins"),
            ("research",         "strategic_recommendations"),
        ]:
            sec = results_dict.get(section_key, {}) or {}
            ana = sec.get("analysis", {}) or {}
            items = ana.get(rec_field) or []
            for item in items:
                if isinstance(item, dict) and item.get("fix"):
                    recs.append({
                        "fix": item.get("fix", ""),
                        "effort": item.get("effort", "Med"),
                        "impact_metric": item.get("impact_metric", ""),
                        "impact_estimate": item.get("impact_estimate", ""),
                        "time_to_see_results": item.get("time_to_see_results", ""),
                        "confidence": item.get("confidence", "medium"),
                        "source": section_key.replace("_", " ").title(),
                    })
        return recs

    def _classify_priority(rec: dict) -> str:
        effort = (rec.get("effort") or "").lower()
        confidence = (rec.get("confidence") or "").lower()
        time_str = (rec.get("time_to_see_results") or "").lower()
        # Red: low effort, high confidence, fast results
        if effort == "low" and confidence == "high":
            return "red"
        if effort == "low" and ("24" in time_str or "48" in time_str or "hours" in time_str):
            return "red"
        # Green: high effort or slow results
        if effort == "high" or "month" in time_str or "quarter" in time_str:
            return "green"
        return "yellow"

    all_recs = _collect_all_recs(results)
    priority_recs = {
        "red":    [r for r in all_recs if _classify_priority(r) == "red"][:4],
        "yellow": [r for r in all_recs if _classify_priority(r) == "yellow"][:4],
        "green":  [r for r in all_recs if _classify_priority(r) == "green"][:4],
    }

    return {
        "audit_id":      audit_data.get("audit_id", ""),
        "url":           audit_data.get("url", ""),
        "brand_name":    bb_a.get("brand_name") or audit_data.get("brand_name", "Brand"),
        "generated_at":  _now_ist(),
        "total_time":    audit_data.get("total_time_seconds", ""),
        "overall_health": health,
        "health_color":   health_color,
        "top_wins":       wins,
        "top_gaps":       gaps,
        "one_thing_banner": one_thing_banner,
        "changes_banner":   changes_banner,
        # trend chips — pre-rendered HTML strings (empty str when no history)
        "trend_content": _chip("content"),
        "trend_ads":     _chip("ads"),
        "trend_geo":     _chip("geo"),
        "trend_store":   _chip("store"),
        "trend_overall": _chip("overall"),
        # raw analysis dicts
        "bb":   bb_a,
        "cc":   cc_a,
        "pa":   pa_a,
        "geo":  geo_a,
        "cro":  cro_a,
        "res":  res_a,
        # pre-computed for template
        "mobile_score":   mobile_score,
        "desktop_score":  desktop_score,
        "lcp":  lcp,
        "cls":  cls,
        "inp":  inp,
        "ad_formats":         ad_formats,
        "schema_checklist":   schema_checklist,
        "geo_citation_rows":  geo_citation_rows,
        "funnel":             funnel,
        "sample_headlines":   sample_headlines,
        "ps_recommendations": ps_wrap.get("recommendations") or [],
        # extra metadata from agent wrappers
        "platform":             brand_basics.get("platform", ""),
        "source_confidence":    brand_basics.get("source_confidence", ""),
        "ads_scrape":           ads_scrape,
        "cro_signals":          store_cro.get("cro_signals") or {},
        "pagespeed":            ps_wrap,
        "category_inferred":    geo_visibility.get("category_inferred", ""),
        "ai_vis_pct":           geo_visibility.get("ai_simulation_visibility_pct", 0),
        "meta_ads_library_url": performance_ads.get("meta_ads_library_url", ""),
        "pagespeed_report_url": f"https://pagespeed.web.dev/report?url={_url_quote(audit_data.get('url', ''), safe='')}",
        # brand health pulse
        "sparklines":    sparklines,
        "pulse_labels":  pulse_labels,
        # 30-day roadmap
        "roadmap":       roadmap,
        # whitespace score
        "whitespace":    whitespace,
        # market forecast
        "market_forecast":  market_forecast,
        "price_trend":      price_trend,
        "review_velocity":  review_velocity,
        "spark_hist":       spark_hist,
        "spark_pred":       spark_pred,
        # social profile (Agent 7)
        "sp":                  sp_a,
        "sp_instagram":        social_profile.get("instagram")              or {},
        "sp_linkedin":         social_profile.get("linkedin")               or {},
        "sp_ads":              social_profile.get("ad_creative_intelligence") or {},
        "sp_score":            social_profile.get("social_presence_score",  0),
        "sp_reasoning":        social_profile.get("social_score_reasoning", ""),
        "sp_improvements":     social_profile.get("top_3_social_improvements") or [],
        # social media deep audit (Agent 8)
        "sma":                   social_media_audit,
        "sma_ig":                (social_media_audit.get("platforms") or {}).get("instagram") or {},
        "sma_yt":                (social_media_audit.get("platforms") or {}).get("youtube")   or {},
        "sma_visual":            social_media_audit.get("visual_analysis")        or {},
        "sma_text":              social_media_audit.get("text_analysis")          or {},
        "sma_scores":            social_media_audit.get("scores")                 or {},
        "sma_gallery":           social_media_audit.get("image_gallery")          or [],
        "sma_strengths":         social_media_audit.get("top_3_strengths")        or [],
        "sma_gaps":              social_media_audit.get("top_3_gaps")             or [],
        "sma_recs":              social_media_audit.get("top_3_recommendations")  or [],
        "sma_assessment":        social_media_audit.get("overall_assessment",     ""),
        "sma_edge":              social_media_audit.get("competitive_edge",       ""),
        "sma_urgency":           social_media_audit.get("urgency_areas")          or [],
        # Reels TRIBE v2 neural engagement
        "sma_reels_tribe":       social_media_audit.get("reels_tribe")            or [],
        "sma_brand_brain_map":   social_media_audit.get("brand_brain_map")        or {},
        "sma_tribe_available":   social_media_audit.get("tribe_available",        False),
        # 10-Dimension Scorecard
        "scorecard_10":          scorecard_10,
        "scorecard_overall":     scorecard_overall,
        # Priority Framework (🔴 Fix now / 🟡 Q3 / 🟢 Medium-term)
        "priority_recs":         priority_recs,
        # Brand basics extras
        "bb_domain_variants":    bb_a.get("domain_variants") or {},
        "bb_category_expansion": bb_a.get("category_expansion") or [],
        "bb_parent_company":     bb_a.get("parent_company", ""),
        "bb_ceo":                bb_a.get("ceo", ""),
        "bb_store_count":        bb_a.get("store_count", ""),
        "bb_awards":             bb_a.get("awards") or [],
        "bb_revenue_range":      bb_a.get("revenue_range", ""),
        "bb_yoy_growth":         bb_a.get("yoy_growth", ""),
        "bb_valuation":          bb_a.get("valuation", ""),
        "bb_moat":               bb_a.get("competitive_moat", ""),
        # Research extras
        "res_moat":              res_a.get("competitive_moat", ""),
        "res_omnichannel":       res_a.get("omnichannel_signals") or {},
        "res_international":     res_a.get("international_signals", ""),
        # CRO extras
        "cro_ux_audit":          cro_a.get("ux_audit") or {},
        "cro_omnichannel":       cro_a.get("omnichannel_ux") or {},
        "cro_signals_raw":       store_cro.get("cro_signals") or {},
        # Source hyperlinks (for in-report citations)
        "cc_product_urls":       content_catalog.get("product_urls_found") or [],
        "sp_instagram_url":      (social_profile.get("instagram") or {}).get("profile_url") or "",
        "sp_linkedin_url":       (social_profile.get("linkedin") or {}).get("profile_url") or "",
        "geo_wikipedia_url":     geo_visibility.get("wikipedia_url") or "",
        "res_google_trends_url": (
            f"https://trends.google.com/trends/explore?geo=IN&q={_url_quote(bb_a.get('brand_name') or audit_data.get('brand_name', ''), safe='')}"
            if (bb_a.get("brand_name") or audit_data.get("brand_name")) else ""
        ),
        "res_tracxn_url":        (research.get("tracxn") or {}).get("tracxn_url") or "",
    }


_SECTION_LABELS = {
    "brand_basics":       "Brand Basics",
    "content_catalog":    "Content & Catalog",
    "performance_ads":    "Performance & Ads",
    "geo_visibility":     "GEO & AI Visibility",
    "store_cro":          "Store & CRO",
    "research":           "Competitive Intel",
    "social_profile":     "Social & Brand Presence",
    "social_media_audit": "Social Media Deep Audit",
}

_CONFIDENCE_BADGE = {
    "verified":    ('<span style="color:#22c55e;font-weight:700">Verified</span>', "#22c55e"),
    "inferred":    ('<span style="color:#f59e0b;font-weight:700">Inferred</span>', "#f59e0b"),
    "unavailable": ('<span style="color:#6b7280;font-weight:700">Unavailable</span>', "#6b7280"),
}


def _linkify(url: str, label: str = None) -> str:
    """Return an <a> tag for a URL, or a plain label/dash if the URL is absent."""
    if not url or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return label or url or "—"
    display = label or (url[:50] + ("…" if len(url) > 50 else ""))
    return f'<a href="{url}" target="_blank" rel="noopener" style="color:#60a5fa;text-decoration:underline">{display}</a>'


_env.globals["linkify"] = _linkify


def is_valid_url(url: object) -> bool:
    """Return True only for URLs that are safe to render as <a href>."""
    return (
        isinstance(url, str)
        and url.startswith(("http://", "https://"))
        and "." in url
        and len(url) > 10
    )


def _source_fallback_url(source: str, brand_url: str, brand_name: str) -> str | None:
    """Construct a best-effort clickable link for a known source type."""
    bn = _url_quote(brand_name)
    bu = _url_quote(brand_url)
    s  = source.lower()
    if "meta" in s or "facebook" in s or "ads" in s:
        return f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=IN&q={bn}"
    if "pagespeed" in s or "psi" in s:
        return f"https://pagespeed.web.dev/report?url={bu}"
    if "wikipedia" in s or "wiki" in s:
        return f"https://en.wikipedia.org/wiki/Special:Search?search={bn}"
    if "trends" in s or "google_trend" in s:
        return f"https://trends.google.com/trends/explore?q={bn}&geo=IN"
    if "homepage" in s:
        return brand_url if is_valid_url(brand_url) else None
    if "duckduckgo" in s or "search" in s:
        return f"https://duckduckgo.com/?q={bn}"
    return None


def _render_sources_panel(results: dict, brand_url: str = "", brand_name: str = "") -> str:
    """Build a collapsible Data Sources attribution table for the report footer."""
    rows: list[dict] = []
    for agent_key, section_label in _SECTION_LABELS.items():
        agent_result = results.get(agent_key) or {}
        for dr in (agent_result.get("sources_used") or []):
            if not isinstance(dr, dict):
                continue
            source = dr.get("source", "")
            if not source:
                continue
            rows.append({
                "section": section_label,
                "source": source.replace("_", " ").title(),
                "source_raw": source,
                "confidence": dr.get("confidence", "inferred"),
                "source_url": dr.get("source_url"),
                "manual_check_url": dr.get("manual_check_url"),
                "error": dr.get("error"),
                "fallback_method": dr.get("fallback_method"),
            })

    if not rows:
        return ""

    def _badge(conf: str) -> str:
        return _CONFIDENCE_BADGE.get(conf, _CONFIDENCE_BADGE["inferred"])[0]

    def _link(row: dict) -> str:
        # Prefer explicit source_url → manual_check_url → constructed fallback
        url = row.get("source_url") or row.get("manual_check_url")
        if not is_valid_url(url):
            url = _source_fallback_url(row.get("source_raw", ""), brand_url, brand_name)
        if is_valid_url(url):
            return f'<a href="{url}" target="_blank" rel="noopener" style="color:#f59e0b">↗</a>'
        return "—"

    rows_html = "\n".join(
        f"""<tr style="border-bottom:1px solid #1e1e1e">
          <td style="padding:.45rem .7rem;font-size:.78rem;color:#a0a0a0">{r["section"]}</td>
          <td style="padding:.45rem .7rem;font-size:.78rem">{r["source"]}</td>
          <td style="padding:.45rem .7rem;font-size:.78rem">{_badge(r["confidence"])}</td>
          <td style="padding:.45rem .7rem;font-size:.78rem">{_link(r)}</td>
        </tr>"""
        for r in rows
    )

    return f"""
<div style="margin:2.5rem 1rem 1rem;font-family:system-ui,sans-serif">
  <details style="background:#111;border:1px solid #242424;border-radius:10px;overflow:hidden">
    <summary style="padding:.75rem 1.1rem;font-size:.8rem;font-weight:700;
      text-transform:uppercase;letter-spacing:.07em;color:#585858;cursor:pointer;
      list-style:none;display:flex;align-items:center;gap:.5rem">
      <span>&#9660;</span> Data Sources &amp; Attribution
    </summary>
    <div style="padding:0 0 .5rem">
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr style="background:#1a1a1a">
            <th style="padding:.5rem .7rem;font-size:.72rem;font-weight:700;text-transform:uppercase;
              letter-spacing:.06em;color:#585858;text-align:left">Section</th>
            <th style="padding:.5rem .7rem;font-size:.72rem;font-weight:700;text-transform:uppercase;
              letter-spacing:.06em;color:#585858;text-align:left">Source</th>
            <th style="padding:.5rem .7rem;font-size:.72rem;font-weight:700;text-transform:uppercase;
              letter-spacing:.06em;color:#585858;text-align:left">Confidence</th>
            <th style="padding:.5rem .7rem;font-size:.72rem;font-weight:700;text-transform:uppercase;
              letter-spacing:.06em;color:#585858;text-align:left">Link</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
      <p style="padding:.6rem 1rem 0;font-size:.71rem;color:#444;line-height:1.5">
        <strong style="color:#585858">Confidence legend:</strong>
        <span style="color:#22c55e">Verified</span> — data extracted directly from the source &nbsp;·&nbsp;
        <span style="color:#f59e0b">Inferred</span> — LLM interpretation of indirect signals &nbsp;·&nbsp;
        <span style="color:#6b7280">Unavailable</span> — source blocked or returned no data
      </p>
    </div>
  </details>
</div>"""


def generate_section(agent_key: str, audit_data: dict) -> str:
    """Return the HTML for a single audit section, identified by agent_key.

    Renders the full report template and extracts the section between
    ``<!-- SECTION:agent_key -->`` and ``<!-- /SECTION:agent_key -->`` markers.
    The returned section always has its ``<details>`` element set to ``open``
    so it's immediately visible when appended during progressive reveal.
    """
    # Short-circuit: agent timed out or failed — return placeholder without rendering
    results = audit_data.get("results") or audit_data
    agent_result = results.get(agent_key) or {}
    if isinstance(agent_result, dict) and agent_result.get("status") in ("timeout", "failed"):
        return _failed_section_placeholder(agent_key)

    audit_data = validate_scores(audit_data)
    ctx = _build_audit_context(audit_data)
    template = _env.get_template("audit_report.html")
    full_html = template.render(**ctx)

    pattern = rf'<!--\s*SECTION:{_re.escape(agent_key)}\s*-->(.*?)<!--\s*/SECTION:{_re.escape(agent_key)}\s*-->'
    match = _re.search(pattern, full_html, _re.DOTALL)
    if not match:
        return f'<div style="padding:1rem;color:#6b7280;font-size:.85rem">Section <em>{agent_key}</em> not available yet.</div>'

    section_html = match.group(1).strip()
    # Force open so the section is visible during live reveal
    for closed_tag in ('<details class="section-accordion">', '<details class="audit-section">', '<details>'):
        if closed_tag in section_html:
            section_html = section_html.replace(closed_tag, closed_tag.replace('>', ' open>', 1), 1)
            break
    return section_html


def _build_virality_context(virality_data: dict) -> dict:
    analysis = virality_data.get("analysis") or virality_data
    dims_raw = analysis.get("dimensions") or {}

    dimensions: list[dict] = []
    dim_order = [
        ("emotional_trigger",      "Emotional Trigger"),
        ("visual_stopping_power",  "Visual Stopping Power"),
        ("transformation_clarity", "Transformation Clarity"),
        ("social_currency",        "Social Currency"),
        ("trend_alignment",        "Trend Alignment"),
        ("share_trigger",          "Share Trigger"),
        ("hook_strength",          "Hook Strength"),
    ]
    for key, label in dim_order:
        raw = dims_raw.get(key, {})
        if isinstance(raw, dict):
            score     = raw.get("score", 0)
            reasoning = raw.get("reasoning", "")
            signals   = raw.get("signals", [])
            evidence  = raw.get("evidence", "")
        else:
            score, reasoning, signals, evidence = (raw or 0), "", [], ""
        dimensions.append({
            "key":       key,
            "label":     label,
            "score":     score,
            "pct":       round((score / 10) * 100),
            "reasoning": reasoning,
            "signals":   signals,
            "evidence":  evidence,
        })

    score = virality_data.get("score") or analysis.get("overall_virality_score") or 0
    grade = virality_data.get("grade") or analysis.get("grade") or ""
    g_char = (grade or "D")[:1].upper()
    grade_color = {
        "S": "#f59e0b", "A": "#22c55e", "B": "#3b82f6",
        "C": "#eab308", "D": "#ef4444",
    }.get(g_char, "#6b7280")

    # Scrape mode + visual/text signals
    scrape_mode      = virality_data.get("scrape_mode", "text_only")
    visual_signals   = virality_data.get("visual_signals") or {}
    text_signals     = virality_data.get("text_signals")   or {}
    fallback_warning = analysis.get("_fallback_warning", "")

    # Viral angles — support both new dict format and legacy string format
    raw_angles   = _safe_list(analysis.get("viral_content_angles"))
    viral_angles = []
    for a in raw_angles:
        if isinstance(a, dict):
            viral_angles.append({
                "text":     a.get("angle", ""),
                "platform": a.get("best_platform", ""),
                "hook":     a.get("hook_line", ""),
                "reach":    a.get("expected_reach_multiplier", ""),
            })
        else:
            viral_angles.append({"text": str(a), "platform": "", "hook": "", "reach": ""})

    return {
        "product_name":        virality_data.get("product_name") or analysis.get("product_name") or "Product",
        "url":                 virality_data.get("url") or "",
        "score":               score,
        "grade":               grade,
        "grade_color":         grade_color,
        "dimensions":          dimensions,
        "killer_hook":         analysis.get("killer_hook", ""),
        "viral_angles":        viral_angles,
        "best_platforms":      _safe_list(analysis.get("best_platforms")),
        "ideal_creator":       analysis.get("ideal_creator_profile", ""),
        "risk_factors":        _safe_list(analysis.get("risk_factors")),
        "comparable":          _safe_list(analysis.get("comparable_viral_products")),
        "generated_at":        _now_ist(),
        "scrape_mode":         scrape_mode,
        "visual_signals":      visual_signals,
        "text_signals":        text_signals,
        "fallback_warning":    fallback_warning,
        "brain_map_svg":       virality_data.get("brain_map_svg") or "",
        "brain_map_source":    virality_data.get("brain_map_source") or "virality_dims",
        "brain_network_scores": virality_data.get("brain_network_scores") or {},
    }
