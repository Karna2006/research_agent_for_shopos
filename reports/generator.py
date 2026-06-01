"""Compiles agent outputs into standalone HTML reports (no external deps)."""
from __future__ import annotations

import copy
import json
import logging
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


_env.globals["bm_chip"] = _bm_chip
_env.filters["tojson"] = lambda v: json.dumps(v, ensure_ascii=False)


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

def generate_audit_report(audit_data: dict) -> str:
    """Render the full 6-section audit report as an HTML string."""
    audit_data = validate_scores(audit_data)
    ctx = _build_audit_context(audit_data)
    template = _env.get_template("audit_report.html")
    html = template.render(**ctx)

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

    brand_basics    = results.get("brand_basics",    {}) or {}
    content_catalog = results.get("content_catalog", {}) or {}
    performance_ads = results.get("performance_ads", {}) or {}
    geo_visibility  = results.get("geo_visibility",  {}) or {}
    store_cro       = results.get("store_cro",       {}) or {}
    research        = results.get("research",        {}) or {}

    bb_a  = brand_basics.get("analysis")    or {}
    cc_a  = content_catalog.get("analysis") or {}
    pa_a  = performance_ads.get("analysis") or {}
    geo_a = geo_visibility.get("analysis")  or {}
    cro_a = store_cro.get("analysis")       or {}
    res_a = research.get("analysis")        or {}

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

    # ── Score trend chips (vs previous audit) ─────────────────────────────────
    audit_url = audit_data.get("url", "")
    _trends   = _get_score_trends(audit_url) if audit_url else {}

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

    return {
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
        # market forecast
        "market_forecast":  market_forecast,
        "price_trend":      price_trend,
        "review_velocity":  review_velocity,
        "spark_hist":       spark_hist,
        "spark_pred":       spark_pred,
    }


_SECTION_LABELS = {
    "brand_basics":    "Brand Basics",
    "content_catalog": "Content & Catalog",
    "performance_ads": "Performance & Ads",
    "geo_visibility":  "GEO & AI Visibility",
    "store_cro":       "Store & CRO",
    "research":        "Competitive Intel",
}

_CONFIDENCE_BADGE = {
    "verified":    ('<span style="color:#22c55e;font-weight:700">Verified</span>', "#22c55e"),
    "inferred":    ('<span style="color:#f59e0b;font-weight:700">Inferred</span>', "#f59e0b"),
    "unavailable": ('<span style="color:#6b7280;font-weight:700">Unavailable</span>', "#6b7280"),
}


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
            score = raw.get("score", 0)
            reasoning = raw.get("reasoning", "")
            signals = raw.get("signals", [])
        else:
            score, reasoning, signals = (raw or 0), "", []
        dimensions.append({
            "key":       key,
            "label":     label,
            "score":     score,
            "pct":       round((score / 10) * 100),
            "reasoning": reasoning,
            "signals":   signals,
        })

    score = virality_data.get("score") or analysis.get("overall_virality_score") or 0
    grade = virality_data.get("grade") or analysis.get("grade") or ""
    g_char = (grade or "D")[:1].upper()
    grade_color = {
        "S": "#f59e0b", "A": "#22c55e", "B": "#3b82f6",
        "C": "#eab308", "D": "#ef4444",
    }.get(g_char, "#6b7280")

    return {
        "product_name":   virality_data.get("product_name") or analysis.get("product_name") or "Product",
        "url":            virality_data.get("url") or "",
        "score":          score,
        "grade":          grade,
        "grade_color":    grade_color,
        "dimensions":     dimensions,
        "killer_hook":    analysis.get("killer_hook", ""),
        "viral_angles":   _safe_list(analysis.get("viral_content_angles")),
        "best_platforms": _safe_list(analysis.get("best_platforms")),
        "ideal_creator":  analysis.get("ideal_creator_profile", ""),
        "risk_factors":   _safe_list(analysis.get("risk_factors")),
        "comparable":     _safe_list(analysis.get("comparable_viral_products")),
        "generated_at":   _now_ist(),
    }
