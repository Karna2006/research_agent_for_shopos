"""Research Agent FastAPI app — web UI + all API endpoints."""
from __future__ import annotations

# Load .env before any module that reads env vars
from dotenv import load_dotenv
load_dotenv()

import asyncio
import io as _io
import logging as _logging
import os as _os
import sys as _sys


class _SafeStream:
    """Wraps stdout/stderr so that writes never raise if the underlying fd is closed.

    tqdm, logging StreamHandler, and other libs call write/flush on sys.stderr
    directly. If the fd is closed (e.g. server started as a background task and
    the output file was deleted), those calls raise ValueError. This wrapper
    silently swallows the error so TRIBE v2 and other thread-executor code keeps
    running.
    """
    def __init__(self, wrapped: _io.TextIOWrapper) -> None:
        self._wrapped = wrapped

    def write(self, msg: str) -> int:
        try:
            return self._wrapped.write(msg)
        except (ValueError, OSError):
            return 0

    def flush(self) -> None:
        try:
            self._wrapped.flush()
        except (ValueError, OSError):
            pass

    def fileno(self) -> int:
        try:
            return self._wrapped.fileno()
        except (ValueError, OSError, _io.UnsupportedOperation):
            return -1

    @property
    def closed(self) -> bool:
        return getattr(self._wrapped, "closed", True)

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)


_sys.stdout = _SafeStream(_sys.stdout)  # type: ignore[assignment]
_sys.stderr = _SafeStream(_sys.stderr)  # type: ignore[assignment]

# Route all logging to a file — never trust sys.stderr in long-running server
_log_handler = _logging.FileHandler("shopos_agent.log", encoding="utf-8")
_log_handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
_logging.basicConfig(level=_logging.INFO, handlers=[_log_handler], force=True)
_logging.lastResort = _logging.NullHandler()
import hashlib
import json
import secrets
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime as _datetime, timezone as _tz, timedelta as _td
from pathlib import Path
from typing import AsyncGenerator, Optional

_IST = _tz(_td(hours=5, minutes=30))


def _fmt_ist(dt: _datetime) -> str:
    """Format a UTC datetime as IST for display. Handles naive datetimes."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return dt.astimezone(_IST).strftime("%d %b %Y, %I:%M %p IST")

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator, model_validator
from sqlmodel import Session, select as _sql_select

from db.database import engine, get_session, init_db, db_backend
from db.models import AGENT_SEQUENCE, AuditRun, BrandConnector, CompareRun, ScoreHistory, ViralityRun
from agents.agentic_orchestrator import run_all as _orchestrate
from agents.virality import ViralityPredictor
from llm.client import get_client
from scrapers.web_scraper import WebScraper
from scrapers.search import SearchAgent
from reports.generator import (
    generate_audit_report, generate_virality_card,
    extract_native_scores, _overall_health,
)
from reports.compare_generator import generate_compare_report, extract_dim_scores, overall_score
from agents.brain_map import generate_activation_heatmap, virality_dims_to_network_scores
from cache.redis_cache import CacheManager, TTL

_cache = CacheManager()

# ── Internal security (Mastra ↔ Python calls) ──────────────────────────────────
import os as _os
_INTERNAL_KEY     = _os.getenv("INTERNAL_SECRET_KEY", "dev-internal-key-change-in-prod")
_MASTRA_URL_RAW   = _os.getenv("MASTRA_URL", "").strip()
_MASTRA_URL       = _MASTRA_URL_RAW or "http://localhost:4111"
_MASTRA_ENABLED   = bool(_MASTRA_URL_RAW)  # only attempt if explicitly configured


def _require_internal(x_internal_key: str = Header(...)) -> None:
    if x_internal_key != _INTERNAL_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")


_AGENT_LABELS: dict[str, str] = {
    "brand_basics":       "Brand Basics",
    "content_catalog":    "Content Audit",
    "performance_ads":    "Ad Intelligence",
    "geo_visibility":     "GEO Visibility",
    "store_cro":          "Store & CRO",
    "research":           "Competitive Research",
    "social_profile":     "Social & Brand Presence",
    "social_media_audit": "Social Media Deep Audit",
}

# ── "Still running" placeholder page (auto-refreshes) ─────────────────────────

_IS_DEV = not _os.getenv("DATABASE_URL", "").strip()  # True when using SQLite

_LOADING_PAGE = """\
<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8"/><meta http-equiv="refresh" content="4"/>
<title>Audit Running…</title>
<style>
body{background:#080808;color:#e8e8e8;font-family:system-ui,sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.box{text-align:center}.ring{width:44px;height:44px;border:3px solid #222;
  border-top-color:#f59e0b;border-radius:50%;animation:sp .9s linear infinite;
  margin:0 auto 1rem}
@keyframes sp{to{transform:rotate(360deg)}}
h2{font-size:1rem;font-weight:600;margin-bottom:.3rem}p{color:#555;font-size:.82rem}
</style></head>
<body><div class="box">
  <div class="ring"></div><h2>Audit in progress…</h2>
  <p>This page auto-refreshes every 4 seconds.</p>
</div></body></html>"""


def _html_error_page(
    title: str,
    message: str,
    detail: str = "",
    help_url: str = "/",
    help_label: str = "Return Home",
) -> str:
    """Branded HTML error page for browser-facing routes (report, share)."""
    detail_block = (
        f'<p style="font-size:.77rem;color:#3a3a3a;margin-top:.5rem;'
        f'font-family:monospace;word-break:break-all">{detail}</p>'
        if detail and _IS_DEV else ""
    )
    return (
        f'<!DOCTYPE html><html lang="en"><head>'
        f'<meta charset="UTF-8"/>'
        f'<meta name="viewport" content="width=device-width,initial-scale=1.0"/>'
        f'<title>{title}</title>'
        f'<style>'
        f'*{{box-sizing:border-box;margin:0;padding:0}}'
        f'body{{background:#080808;color:#e8e8e8;font-family:system-ui,sans-serif;'
        f'display:flex;align-items:center;justify-content:center;min-height:100vh;padding:2rem}}'
        f'.box{{text-align:center;max-width:480px}}'
        f'.icon{{font-size:2.5rem;margin-bottom:1rem;opacity:.7}}'
        f'h1{{font-size:1.35rem;font-weight:800;margin-bottom:.6rem;letter-spacing:-.3px}}'
        f'p{{color:#666;font-size:.88rem;line-height:1.6;margin-bottom:1.25rem}}'
        f'a.btn{{display:inline-flex;align-items:center;gap:.35rem;padding:.55rem 1.25rem;'
        f'background:#f59e0b;color:#000;border-radius:8px;font-size:.87rem;font-weight:700;'
        f'text-decoration:none;transition:opacity .15s}}'
        f'a.btn:hover{{opacity:.85}}'
        f'</style></head>'
        f'<body><div class="box">'
        f'<div class="icon">⚡</div>'
        f'<h1>{title}</h1>'
        f'<p>{message}</p>'
        f'{detail_block}'
        f'<a href="{help_url}" class="btn">{help_label}</a>'
        f'</div></body></html>'
    )


def _std_error(
    error: str,
    message: str,
    status: int = 400,
    detail: str = "",
    help_url: str = "",
) -> JSONResponse:
    """Standard JSON error response used by all API endpoints."""
    body: dict = {"error": error, "message": message}
    if detail and _IS_DEV:
        body["detail"] = detail
    if help_url:
        body["help_url"] = help_url
    return JSONResponse(body, status_code=status)

# ── Web UI (inline, zero external deps) ───────────────────────────────────────

UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Research Agent — Ecommerce Intelligence</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080808;--surface:#111;--surface2:#1c1c1c;--border:#242424;
  --text:#e8e8e8;--muted:#585858;--r:12px;
  --amber:#f59e0b;--green:#22c55e;--blue:#3b82f6;--red:#ef4444;
}
html{font-size:15px;background:var(--bg);color:var(--text)}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh}
input,textarea,button,select{font-family:inherit}
a{color:var(--amber);text-decoration:none}

/* layout */
.hdr{display:flex;align-items:center;gap:1rem;padding:1.1rem 1.75rem;
  border-bottom:1px solid var(--border)}
.logo{font-size:1.05rem;font-weight:800;letter-spacing:-.3px}
.logo em{color:var(--amber);font-style:normal}
.sub{font-size:.72rem;color:var(--muted);margin-top:.08rem}
main{max-width:1200px;margin:0 auto;padding:2.25rem 1.5rem 5rem}

/* tabs */
.tabs{display:flex;border-bottom:1px solid var(--border);margin-bottom:2rem}
.tab{background:none;border:none;border-bottom:2px solid transparent;
  margin-bottom:-1px;padding:.5rem 1.1rem;font-size:.86rem;font-weight:500;
  color:var(--muted);cursor:pointer;transition:color .15s}
.tab.on{color:var(--text);border-bottom-color:var(--amber)}
.pane{display:none}.pane.on{display:block}

/* form */
.field{margin-bottom:.95rem}
.field label{display:block;font-size:.74rem;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:.07em;margin-bottom:.35rem}
.opt{text-transform:none;letter-spacing:0;font-weight:400}
input[type=text],input[type=url],textarea{
  display:block;width:100%;background:var(--surface);border:1px solid var(--border);
  border-radius:8px;padding:.62rem .85rem;font-size:.9rem;color:var(--text);
  outline:none;transition:border-color .15s}
input:focus,textarea:focus{border-color:var(--amber)}
textarea{min-height:88px;resize:vertical;line-height:1.55}
.row{display:flex;gap:.6rem;align-items:flex-end}.row input{flex:1}
.two{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}

/* buttons */
.btn{display:inline-flex;align-items:center;gap:.35rem;padding:.58rem 1.2rem;
  border-radius:8px;border:none;font-size:.87rem;font-weight:600;
  cursor:pointer;white-space:nowrap;transition:opacity .15s}
.btn:disabled{opacity:.42;cursor:not-allowed}
.btn-p{background:var(--amber);color:#000}.btn-p:hover:not(:disabled){opacity:.87}
.btn-g{background:var(--surface2);color:var(--text);border:1px solid var(--border)}
.btn-g:hover:not(:disabled){border-color:var(--amber)}

/* card */
.card{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--r);padding:1.2rem;margin-top:1.15rem}

/* pipeline */
.pl-hd{font-size:.7rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.09em;color:var(--muted);margin-bottom:.7rem}
.ag-row{display:flex;align-items:center;gap:.65rem;padding:.42rem 0;
  border-bottom:1px solid var(--border)}
.ag-row:last-of-type{border-bottom:none}
.ag-ic{width:22px;font-size:.88rem;flex-shrink:0;text-align:center;transition:color .2s}
.ag-name{flex:1;font-size:.86rem}
.ag-st{font-size:.75rem;transition:color .2s}
.prog-wrap{margin-top:.85rem;height:3px;background:var(--border);
  border-radius:2px;overflow:hidden}
.prog-fill{height:100%;background:var(--amber);border-radius:2px;
  transition:width .5s ease}

/* spinner */
@keyframes spin{to{transform:rotate(360deg)}}
.spin{display:inline-block;border-radius:50%;animation:spin .85s linear infinite;
  flex-shrink:0}
.spin-lg{width:38px;height:38px;border:3px solid var(--border);
  border-top-color:var(--amber);margin:0 auto .7rem;display:block}
.ic-spin{display:inline-block;animation:spin 1.1s linear infinite}

/* status badges */
.badge{display:inline-flex;align-items:center;gap:.3rem;padding:.18rem .55rem;
  border-radius:4px;font-size:.73rem;font-weight:600}
.b-ok{background:rgba(34,197,94,.12);color:var(--green)}
.b-err{background:rgba(239,68,68,.12);color:var(--red)}

/* ── Split-pane audit layout ───────────────────────────────────────────────── */
#audit-layout{width:100%}
#audit-sidebar{width:100%}
#audit-content{display:none;flex:1;min-width:0}
#audit-layout.audit-split{display:flex;gap:1.25rem;align-items:flex-start}
#audit-layout.audit-split #audit-sidebar{width:310px;flex-shrink:0}
#audit-layout.audit-split #audit-content{display:block}

/* ── Inline report iframe ──────────────────────────────────────────────────── */
.report-wrap{display:none}
.report-toolbar{display:flex;align-items:center;justify-content:space-between;
  margin-bottom:.55rem}
.report-frame{width:100%;height:calc(100vh - 130px);min-height:560px;
  border:1px solid #1e1e1e;border-radius:10px;background:#0a0a0a;display:block}

/* ── Live section styles — full report CSS injected so fragments render properly ── */
#live-sections{padding-bottom:2rem}
@keyframes sec-appear{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
#live-sections>[data-agent]{animation:sec-appear .35s ease forwards}
#live-sections details.section-accordion,#live-sections details.audit-section{
  border:1px solid var(--border);border-radius:var(--r);margin-top:1.25rem;overflow:hidden}
#live-sections details[open] summary .accordion-arrow{transform:rotate(180deg)}
#live-sections summary.section-header{display:flex;align-items:center;gap:.75rem;
  padding:1rem 1.25rem;cursor:pointer;list-style:none;background:var(--surface);user-select:none}
#live-sections summary.section-header::-webkit-details-marker,
#live-sections summary::-webkit-details-marker{display:none}
#live-sections .section-num{font-size:.7rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.1em;color:var(--blue)}
#live-sections .section-title{font-size:1.1rem;font-weight:700;letter-spacing:-.2px}
#live-sections .section-score-badge{font-size:.8rem;font-weight:700;padding:.18rem .65rem;
  border-radius:999px;background:rgba(255,255,255,.06);border:1px solid var(--border)}
#live-sections .accordion-arrow{margin-left:auto;transition:transform .2s;color:var(--blue);font-size:1.1rem;flex-shrink:0}
#live-sections .section-body{padding:1.25rem}
/* ── Report fragment CSS — identical to audit_report.html so fragments render correctly ── */
#live-sections .card{background:#141414;border:1px solid #2a2a2a;border-radius:10px;padding:1.25rem}
#live-sections .card+.card{margin-top:.75rem}
#live-sections .sh{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;
  color:#6b7280;margin-bottom:.65rem;padding-bottom:.4rem;border-bottom:1px solid #2a2a2a}
#live-sections .dim-bg{height:7px;background:#1e1e1e;border-radius:999px;margin:.3rem 0}
#live-sections .dim-fill{height:7px;border-radius:999px;transition:width .6s ease}
#live-sections .pill{display:inline-flex;align-items:center;gap:.3rem;padding:.18rem .65rem;
  border-radius:999px;font-size:.78rem;font-weight:700;border:1px solid transparent}
#live-sections .pill-green{background:rgba(34,197,94,.12);color:#22c55e;border-color:rgba(34,197,94,.25)}
#live-sections .pill-amber{background:rgba(245,158,11,.12);color:#f59e0b;border-color:rgba(245,158,11,.25)}
#live-sections .pill-red{background:rgba(239,68,68,.12);color:#ef4444;border-color:rgba(239,68,68,.25)}
#live-sections .pill-blue{background:rgba(59,130,246,.12);color:#3b82f6;border-color:rgba(59,130,246,.25)}
#live-sections .pill-muted{background:rgba(107,114,128,.1);color:#6b7280;border-color:rgba(107,114,128,.2)}
#live-sections .pill-tribe{background:#052e16;color:#22c55e}
#live-sections .score-grid{display:flex;flex-wrap:wrap;gap:.6rem;margin-bottom:1rem}
#live-sections .score-item{background:#1e1e1e;border:1px solid #2a2a2a;border-radius:8px;
  padding:.6rem .9rem;min-width:130px;flex:1}
#live-sections .score-item-label{font-size:.7rem;color:#6b7280;font-weight:600;
  text-transform:uppercase;letter-spacing:.06em;margin-bottom:.25rem}
#live-sections .score-item-val{font-size:1.25rem;font-weight:800;line-height:1}
#live-sections .check-list{list-style:none;padding:0}
#live-sections .check-list li{display:flex;align-items:flex-start;gap:.55rem;
  font-size:.87rem;padding:.32rem 0;border-bottom:1px solid #2a2a2a}
#live-sections .check-list li:last-child{border-bottom:none}
#live-sections .ci{flex-shrink:0;font-size:.9rem;margin-top:.05rem}
#live-sections .ci-ok{color:#22c55e}
#live-sections .ci-warn{color:#f59e0b}
#live-sections .ci-bad{color:#ef4444}
#live-sections .info-table{width:100%;border-collapse:collapse;font-size:.88rem}
#live-sections .info-table tr{border-bottom:1px solid #2a2a2a}
#live-sections .info-table tr:last-child{border-bottom:none}
#live-sections .info-table td{padding:.6rem .85rem;vertical-align:top;line-height:1.5}
#live-sections .info-table td:first-child{color:#6b7280;font-weight:600;width:175px;background:#1e1e1e}
#live-sections .ba-wrap{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:1rem}
#live-sections .ba-box{border-radius:8px;padding:1rem 1.15rem;font-size:.86rem;line-height:1.65}
#live-sections .ba-before{background:#1c0a0a;border-left:4px solid #ef4444}
#live-sections .ba-after{background:#0a1c0a;border-left:4px solid #22c55e}
#live-sections .ba-label{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;margin-bottom:.4rem}
#live-sections .ba-before .ba-label{color:#ef4444}
#live-sections .ba-after .ba-label{color:#22c55e}
#live-sections .fmt-bars{display:flex;flex-direction:column;gap:.55rem}
#live-sections .fmt-bar{display:flex;align-items:center;gap:.7rem;font-size:.83rem}
#live-sections .fmt-bar-label{min-width:90px;color:#e8e8e8;font-weight:500}
#live-sections .fmt-bar-track{flex:1;height:8px;background:#1e1e1e;border-radius:999px;overflow:hidden}
#live-sections .fmt-bar-fill{height:8px;border-radius:999px;background:#3b82f6}
#live-sections .fmt-bar-val{min-width:36px;text-align:right;color:#6b7280;font-size:.78rem}
#live-sections .swot-grid{display:grid;grid-template-columns:1fr 1fr;gap:.75rem;margin-top:.5rem}
#live-sections .swot-cell{padding:.75rem .9rem;border-radius:8px;font-size:.83rem;line-height:1.55}
#live-sections .swot-s{background:rgba(34,197,94,.07);border-left:3px solid #22c55e}
#live-sections .swot-w{background:rgba(239,68,68,.07);border-left:3px solid #ef4444}
#live-sections .swot-o{background:rgba(59,130,246,.07);border-left:3px solid #3b82f6}
#live-sections .swot-t{background:rgba(245,158,11,.07);border-left:3px solid #f59e0b}
#live-sections .swot-ttl{font-size:.67rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.1em;margin-bottom:.4rem}
#live-sections .wspace{border-radius:10px;padding:1rem 1.2rem;margin-top:.65rem}
#live-sections .wspace-blue{background:rgba(59,130,246,.07);border:1px solid rgba(59,130,246,.2)}
#live-sections .wspace-amber{background:rgba(245,158,11,.07);border:1px solid rgba(245,158,11,.2)}
#live-sections .wspace-red{background:rgba(239,68,68,.07);border:1px solid rgba(239,68,68,.2)}

/* ── FIX 3: Audit example chips ───────────────────────────────────────────── */
.chip-bar{display:flex;align-items:center;gap:.45rem;flex-wrap:wrap;margin-bottom:.9rem}
.chip-lbl{font-size:.7rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.07em;color:var(--muted)}
.chip{padding:.28rem .78rem;border-radius:999px;font-size:.79rem;font-weight:600;
  cursor:pointer;border:1px solid var(--border);background:var(--surface);
  color:var(--text);transition:border-color .15s,color .15s}
.chip:hover{border-color:var(--amber);color:var(--amber)}

/* virality result */
.v-center{text-align:center;padding:1.2rem 0 .9rem}
.v-num{font-size:3.4rem;font-weight:900;line-height:1;letter-spacing:-.5px}
.v-denom{font-size:.82rem;color:var(--muted)}
.v-badge{display:inline-block;padding:.28rem .9rem;border-radius:999px;
  font-size:.8rem;font-weight:700;margin-top:.6rem;color:#000}
.dim-row{margin-bottom:.65rem}
.dim-top{display:flex;justify-content:space-between;align-items:baseline;
  margin-bottom:.2rem}
.dim-label{font-size:.82rem;font-weight:500}
.dim-score{font-size:.82rem;font-weight:700}
.dim-bg{height:5px;background:var(--border);border-radius:3px;overflow:hidden}
.dim-fill{height:100%;border-radius:3px;transition:width .65s .05s ease}
.dim-rsn{font-size:.74rem;color:var(--muted);margin-top:.12rem}
.sh{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
  color:var(--muted);margin:1.05rem 0 .55rem}
.hook-box{background:linear-gradient(135deg,#1a1000,#0d0d00);
  border:1px solid #3a2900;border-radius:10px;padding:.9rem 1.15rem;margin:.9rem 0}
.hook-lbl{font-size:.66rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.1em;color:#f59e0b;margin-bottom:.28rem}
.hook-txt{font-size:.97rem;font-weight:600;color:#fde68a;line-height:1.4}
.ang-item{padding:.32rem 0;font-size:.85rem;border-bottom:1px solid var(--border);
  display:flex;gap:.45rem;align-items:flex-start}
.ang-item:last-child{border-bottom:none}
.ang-n{width:20px;height:20px;border-radius:50%;font-size:.7rem;font-weight:700;
  color:#000;display:flex;align-items:center;justify-content:center;flex-shrink:0;
  margin-top:.05rem}
.plat-wrap{display:flex;flex-wrap:wrap;gap:.3rem;margin-bottom:.5rem}
.plat{padding:.2rem .6rem;background:var(--surface2);border:1px solid var(--border);
  border-radius:6px;font-size:.77rem}
.err-box{background:rgba(239,68,68,.07);border:1px solid rgba(239,68,68,.22);
  border-radius:8px;padding:.65rem .9rem;font-size:.83rem;color:var(--red);
  margin-top:.9rem}

/* ── Compare pane ─────────────────────────────────────────────────────────── */
.cmp-vs{color:var(--muted);font-size:.82rem;align-self:center;padding-top:1.6rem;
  flex-shrink:0;font-weight:700}
.cmp-dual{display:grid;grid-template-columns:1fr 1fr;gap:.75rem;margin-top:.85rem}
.cmp-col-hdr{font-size:.7rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.08em;margin-bottom:.5rem}
.cmp-col-hdr.a{color:#60a5fa}.cmp-col-hdr.b{color:#fcd34d}
.cmp-agent-row{display:flex;align-items:center;gap:.5rem;padding:.32rem 0;
  border-bottom:1px solid var(--border);font-size:.82rem}
.cmp-agent-row:last-child{border-bottom:none}
.cmp-ic{width:20px;font-size:.85rem;flex-shrink:0;text-align:center}
.cmp-st{margin-left:auto;font-size:.72rem;color:var(--muted)}

/* ── FIX 3: Virality example cards ───────────────────────────────────────── */
.ex-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:.55rem;margin-top:.5rem}
.ex-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:.85rem .7rem;cursor:pointer;transition:border-color .15s;text-align:center}
.ex-card:hover{border-color:var(--amber)}
.ex-card.active{border-color:var(--amber);background:rgba(245,158,11,.06)}
.ex-icon{font-size:1.5rem;margin-bottom:.2rem}
.ex-name{font-size:.77rem;font-weight:600;margin-bottom:.12rem;line-height:1.3}
.ex-meta{font-size:.66rem;color:var(--muted);margin-bottom:.3rem}
.ex-badge{display:inline-block;padding:.1rem .42rem;border-radius:4px;
  font-size:.64rem;font-weight:700;background:var(--surface2);color:var(--muted);
  border:1px solid var(--border)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
.ex-badge.loading{background:rgba(245,158,11,.15);color:var(--amber);
  border-color:rgba(245,158,11,.4);animation:pulse 1.1s ease-in-out infinite}

/* loading state */
.ld{margin-top:1rem;text-align:center;color:var(--muted);font-size:.84rem}

/* ── Cache indicator ───────────────────────────────────────────────────────── */
.cache-hit{display:inline-flex;align-items:center;gap:.3rem;padding:.2rem .6rem;
  border-radius:6px;font-size:.73rem;font-weight:600;
  background:rgba(245,158,11,.12);color:var(--amber);border:1px solid rgba(245,158,11,.25)}

/* ── Feature 1: Live finding cards ─────────────────────────────────────────── */
@keyframes slide-in{from{opacity:0;transform:translateX(16px)}to{opacity:1;transform:translateX(0)}}
.findings-feed{display:flex;flex-direction:column;gap:.45rem;margin-top:.85rem}
.finding-card{background:var(--surface2);border:1px solid var(--border);border-left:3px solid var(--amber);
  border-radius:8px;padding:.7rem 1rem;animation:slide-in .4s ease forwards}
.finding-agent{font-size:.63rem;font-weight:700;text-transform:uppercase;letter-spacing:.09em;
  color:var(--muted);margin-bottom:.2rem}
.finding-headline{font-size:.88rem;font-weight:700;color:var(--text);margin-bottom:.16rem;line-height:1.35}
.finding-stat{font-size:.75rem;font-weight:600;color:var(--amber);margin-bottom:.12rem}
.finding-insight{font-size:.75rem;color:var(--muted);line-height:1.5}

/* ── Feature 2: Data counter ────────────────────────────────────────────────── */
.data-counter{text-align:center;font-size:.77rem;color:var(--muted);margin-top:.55rem;
  padding:.28rem;letter-spacing:.01em;transition:opacity .3s}
.data-counter strong{color:var(--text);font-variant-numeric:tabular-nums}

/* ── Feature 3: Insights panel ──────────────────────────────────────────────── */
.insight-panel{margin-top:.85rem;border-left:3px solid var(--blue)}
.insight-hdr{font-size:.63rem;font-weight:700;text-transform:uppercase;
  letter-spacing:.09em;color:var(--blue);margin-bottom:.5rem}
#insight-text{font-size:.84rem;line-height:1.7;color:var(--text);
  min-height:3rem;transition:opacity .3s ease}
.insight-dots-row{display:flex;justify-content:center;gap:5px;margin-top:.65rem}
.insight-dot{width:5px;height:5px;border-radius:50%;background:var(--border);
  display:inline-block;transition:background .3s}
.insight-dot.on{background:var(--blue)}

/* ── Feature 4: Phase 3 parallel dots ──────────────────────────────────────── */
@keyframes p3pulse{0%,100%{opacity:.2}50%{opacity:1}}
.phase3-dots{display:inline-flex;gap:3px;align-items:center}
.phase3-dots span{width:5px;height:5px;border-radius:50%;background:var(--amber);
  animation:p3pulse 1.4s ease-in-out infinite;display:inline-block}
.phase3-dots span:nth-child(1){animation-delay:0s}
.phase3-dots span:nth-child(2){animation-delay:.2s}
.phase3-dots span:nth-child(3){animation-delay:.4s}
.phase3-dots span:nth-child(4){animation-delay:.6s}
</style>
</head>
<body>

<header class="hdr">
  <div>
    <div class="logo">Research <em>Agent</em></div>
    <div class="sub">Ecommerce Intelligence</div>
  </div>
</header>

<main>
<!-- tabs -->
<div class="tabs">
  <button class="tab on" onclick="showTab('audit',this)">Brand Audit</button>
  <button class="tab"    onclick="showTab('virality',this)">Virality Score</button>
  <button class="tab"    onclick="showTab('compare',this)">Compare Brands</button>
  <button class="tab"    onclick="showTab('brands',this);loadBrands()">My Brands</button>
  <button class="tab"    onclick="showTab('connectors',this);loadConnectors()">🔌 Connectors</button>
</div>

<!-- ── Brand Audit pane ──────────────────────────────────────────────────── -->
<div id="pane-audit" class="pane on">
<div id="audit-layout">

  <!-- LEFT SIDEBAR: form + pipeline tracker -->
  <div id="audit-sidebar">
    <!-- Example chips -->
    <div class="chip-bar">
      <span class="chip-lbl">Examples:</span>
      <button class="chip" onclick="loadAuditExample('https://rarerabbit.in','demo')">⚡ Rare Rabbit</button>
      <button class="chip" onclick="loadAuditExample('https://www.hoka.com/en-in/')">Hoka</button>
      <button class="chip" onclick="loadAuditExample('https://www.boat-lifestyle.com/')">boAt</button>
    </div>

    <div class="field">
      <label>Brand URL</label>
      <div class="row">
        <input type="url" id="a-url" placeholder="https://rarerabbit.in"
               onkeydown="if(event.key==='Enter')startAudit()"/>
        <button class="btn btn-p" id="a-btn" onclick="startAudit()">Run →</button>
      </div>
    </div>
    <div style="margin-top:.55rem;display:flex;align-items:center;gap:.5rem">
      <label style="display:flex;align-items:center;gap:.4rem;cursor:pointer;font-size:.78rem;color:var(--muted)">
        <input type="checkbox" id="a-deep-visual" style="accent-color:#a855f7;width:13px;height:13px"/>
        <span>Deep Visual Analysis</span>
        <span style="font-size:.68rem;color:#4b5563">(Instagram Reels + TRIBE v2 fMRI · adds ~20 min)</span>
      </label>
    </div>

    <div id="a-pipeline" style="display:none">
      <div class="card" style="margin-top:.7rem">
        <div class="pl-hd">Analysis Pipeline</div>
        <div id="a-agents"></div>
        <div class="prog-wrap">
          <div class="prog-fill" id="a-prog" style="width:0"></div>
        </div>
        <div class="data-counter" id="data-counter" style="display:none">
          Analysed <strong id="data-count-num">0</strong> data points&hellip;
        </div>
      </div>

      <!-- While-you-wait insights -->
      <div id="insights-panel" class="card insight-panel" style="display:none">
        <div class="insight-hdr">Did you know?</div>
        <div id="insight-text"></div>
        <div class="insight-dots-row" id="insight-dots"></div>
      </div>

      <!-- finding cards removed — sections are the live feed -->
      <div id="live-findings" class="findings-feed" style="display:none"></div>
    </div>

    <div id="a-result"></div>
  </div>

  <!-- RIGHT CONTENT: report fills this area -->
  <div id="audit-content">
    <!-- Progressive sections while report is loading -->
    <div id="live-sections" style="display:none"></div>

    <div id="a-report-wrap" class="report-wrap">
      <div class="report-toolbar">
        <div style="display:flex;align-items:center;gap:.5rem">
          <span class="badge b-ok">✓ Audit complete</span>
          <span id="a-cache-badge" style="display:none" class="cache-hit">⚡ Loaded from cache</span>
        </div>
        <button class="btn btn-g" onclick="printReport()"
          style="font-size:.8rem;padding:.38rem .85rem">⬇ Download PDF</button>
      </div>
      <iframe id="a-report-frame" src="" frameborder="0" class="report-frame"></iframe>
    </div>
  </div>

</div><!-- /audit-layout -->
</div>

<!-- ── Virality pane ─────────────────────────────────────────────────────── -->
<div id="pane-virality" class="pane">
  <div class="field">
    <label>Product URL <span class="opt">(optional — we'll scrape it)</span></label>
    <input type="url" id="v-url" placeholder="https://example.com/products/item"/>
  </div>
  <div class="two">
    <div class="field">
      <label>Product Name</label>
      <input type="text" id="v-name" placeholder="Hydrogel Eye Patches"/>
    </div>
    <div class="field">
      <label>Category <span class="opt">(optional)</span></label>
      <input type="text" id="v-cat" placeholder="skincare"/>
    </div>
  </div>
  <div class="field">
    <label>Product Description</label>
    <textarea id="v-desc"
      placeholder="Describe the product — key features, bold claims, target audience, price point…"></textarea>
  </div>
  <button class="btn btn-p" id="v-btn" onclick="startVirality()">Score It →</button>

  <!-- FIX 3: Example cards -->
  <div style="margin-top:1.15rem">
    <div class="sh">Try an example</div>
    <div class="ex-cards">
      <div class="ex-card" id="exc-0" onclick="loadViralityExample(0)">
        <div class="ex-icon">✨</div>
        <div class="ex-name">Chanel Foundation</div>
        <div class="ex-meta">Nykaa · Luxury Beauty</div>
        <div class="ex-badge" id="excb-0">Pre-cached</div>
      </div>
      <div class="ex-card" id="exc-1" onclick="loadViralityExample(1)">
        <div class="ex-icon">🎧</div>
        <div class="ex-name">boAt Airdopes 141</div>
        <div class="ex-meta">TWS Earbuds · TRIBE v2 fMRI</div>
        <div class="ex-badge" id="excb-1">Pre-cached</div>
      </div>
      <div class="ex-card" id="exc-2" onclick="loadViralityExample(2)">
        <div class="ex-icon">👕</div>
        <div class="ex-name">Generic White Tee</div>
        <div class="ex-meta">Basics · 3-Pack</div>
        <div class="ex-badge" id="excb-2">Live</div>
      </div>
    </div>
  </div>

  <div id="v-loading" style="display:none" class="ld">
    <span class="spin spin-lg"></span>
    Analysing virality potential… this takes ~20 seconds.
  </div>
  <div id="v-result"></div>

  <!-- ── TRIBE v2 Video Neural Analysis ──────────────────────────────────── -->
  <div style="margin-top:2rem;padding-top:1.25rem;border-top:1px solid var(--border)">
    <div class="sh" style="font-size:.78rem;margin-bottom:.55rem">
      Neural Video Analysis
      <span style="font-size:.62rem;color:var(--muted);font-weight:400;margin-left:.35rem">· Meta TRIBE v2 fMRI · any video platform</span>
    </div>
    <p style="font-size:.78rem;color:#4b5563;margin-bottom:.75rem;line-height:1.5">
      Paste any video URL — YouTube, Instagram Reels, TikTok, Vimeo, direct .mp4, or any yt-dlp-supported platform.
      TRIBE v2 predicts which brain networks the video activates. Takes ~10–30 min on CPU.
    </p>
    <div style="display:flex;gap:.5rem;align-items:flex-end">
      <div class="field" style="flex:1;margin-bottom:0">
        <label style="font-size:.72rem">Video URL</label>
        <input type="url" id="nv-url"
          placeholder="https://youtu.be/… or instagram.com/reel/… or direct .mp4"
          style="font-size:.83rem"/>
      </div>
      <div class="field" style="width:160px;margin-bottom:0">
        <label style="font-size:.72rem">Label <span class="opt">(optional)</span></label>
        <input type="text" id="nv-label" placeholder="Brand Ad — Q1"
          style="font-size:.83rem"/>
      </div>
    </div>
    <button class="btn btn-g" id="nv-btn" onclick="startVideoAnalysis()"
      style="margin-top:.65rem;font-size:.82rem;padding:.45rem 1.1rem">
      Analyze with TRIBE v2 →
    </button>
    <div id="nv-loading" style="display:none;margin-top:.65rem" class="ld">
      <span class="spin spin-lg"></span>
      Running TRIBE v2 fMRI inference… this takes 10–30 minutes on CPU.
      <div style="font-size:.68rem;color:#374151;margin-top:.25rem">
        Downloading video → extracting audio/visual features → predicting cortical activations
      </div>
    </div>
    <div id="nv-result" style="margin-top:.75rem"></div>
  </div>
</div>

<!-- ── Compare Brands pane ───────────────────────────────────────────────── -->
<div id="pane-compare" class="pane">

  <div class="chip-bar">
    <span class="chip-lbl">Examples:</span>
    <button class="chip" onclick="loadCmpExample('https://rarerabbit.in','https://www.boat-lifestyle.com/')">Rare Rabbit vs boAt</button>
    <button class="chip" onclick="loadCmpExample('https://rarerabbit.in','https://bewakoof.com')">Rare Rabbit vs Bewakoof</button>
    <button class="chip" onclick="loadCmpExample('https://www.nike.com/in/','https://www.hoka.com/en-in/')">Nike vs Hoka India</button>
  </div>

  <div style="display:flex;gap:.6rem;align-items:flex-end;flex-wrap:wrap">
    <div class="field" style="flex:1;min-width:200px;margin-bottom:0">
      <label style="color:#60a5fa">Brand A URL</label>
      <input type="url" id="cmp-url-a" placeholder="https://rarerabbit.in"
             onkeydown="if(event.key==='Enter')startCompare()"/>
    </div>
    <div class="cmp-vs">vs</div>
    <div class="field" style="flex:1;min-width:200px;margin-bottom:0">
      <label style="color:#fcd34d">Brand B URL</label>
      <input type="url" id="cmp-url-b" placeholder="https://bewakoof.com"
             onkeydown="if(event.key==='Enter')startCompare()"/>
    </div>
    <button class="btn btn-p" id="cmp-btn" onclick="startCompare()"
      style="white-space:nowrap">Compare →</button>
  </div>

  <!-- Dual pipeline progress -->
  <div id="cmp-pipeline" style="display:none;margin-top:.85rem">
    <div class="card">
      <div class="cmp-dual" id="cmp-dual-cols">
        <!-- filled by JS -->
      </div>
      <div class="prog-wrap" style="margin-top:.75rem">
        <div class="prog-fill" id="cmp-prog" style="width:0"></div>
      </div>
    </div>
  </div>

  <div id="cmp-result"></div>

  <div id="cmp-report-wrap" class="report-wrap">
    <div class="report-toolbar">
      <span class="badge b-ok">✓ Comparison ready</span>
      <button class="btn btn-g" onclick="printCmpReport()"
        style="font-size:.8rem;padding:.38rem .85rem">⬇ Download PDF</button>
    </div>
    <iframe id="cmp-report-frame" src="" frameborder="0" class="report-frame"></iframe>
  </div>
</div>

<!-- ── My Brands pane ────────────────────────────────────────────────────── -->
<div id="pane-brands" class="pane">
  <div class="card" style="margin-top:1.5rem">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:1.25rem;flex-wrap:wrap;gap:.75rem">
      <div>
        <div style="font-size:1.05rem;font-weight:700">Tracked Brands</div>
        <div style="font-size:.78rem;color:var(--muted);margin-top:.2rem">All brands ever audited — enable monitoring to track weekly changes</div>
      </div>
      <div style="display:flex;gap:.5rem;align-items:center">
        <input id="brands-url-input" type="url" placeholder="https://yourbrand.com"
          style="padding:.4rem .75rem;border-radius:6px;border:1px solid var(--border);
          background:var(--card2);color:var(--text);font-size:.84rem;width:220px"/>
        <button onclick="addBrandMonitor()"
          style="padding:.4rem .9rem;border-radius:6px;border:1px solid var(--blue);
          background:transparent;color:var(--blue);font-size:.84rem;font-weight:600;cursor:pointer">
          + Add Brand
        </button>
        <button onclick="loadBrands()"
          style="padding:.4rem .75rem;border-radius:6px;border:1px solid var(--border);
          background:transparent;color:var(--muted);font-size:.82rem;cursor:pointer">
          ↻ Refresh
        </button>
      </div>
    </div>
    <div id="brands-table-wrap">
      <div style="text-align:center;padding:2rem;color:var(--muted);font-size:.88rem">Loading…</div>
    </div>
  </div>
</div>

<!-- ── Connectors pane ──────────────────────────────────────────────────── -->
<div id="pane-connectors" class="pane">

  <!-- Brand URL selector + status lookup -->
  <div class="card" style="margin-top:1.5rem">
    <div style="font-size:1.05rem;font-weight:700;margin-bottom:.35rem">API Connectors</div>
    <div style="font-size:.78rem;color:var(--muted);margin-bottom:1.25rem">
      Connect private API keys for deeper brand analysis — private store data, real ad spend, ROAS, and customer metrics.
    </div>

    <div class="field">
      <label>Brand URL to configure</label>
      <div class="row">
        <input type="url" id="conn-brand-url" placeholder="https://yourbrand.com"
               oninput="onConnBrandUrl(this.value)"/>
        <button class="btn btn-g" onclick="checkConnectorStatus()">Check Status</button>
      </div>
    </div>

    <!-- Status indicator -->
    <div id="conn-status-row" style="display:none;gap:.65rem;margin-bottom:1.1rem;display:none;flex-wrap:wrap"></div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:.5rem" class="conn-forms">

      <!-- ── Shopify ────────────────────────────────────────────────── -->
      <div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:1.1rem">
        <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.85rem">
          <span style="font-size:1.3rem">🛒</span>
          <div>
            <div style="font-weight:700;font-size:.95rem">Shopify</div>
            <div style="font-size:.72rem;color:var(--muted)">Private store data — orders, inventory, customers</div>
          </div>
          <span id="shopify-badge" style="margin-left:auto;font-size:.7rem;font-weight:600;
            padding:.15rem .5rem;border-radius:4px;display:none"></span>
        </div>

        <div class="field">
          <label>Store URL</label>
          <input type="url" id="shopify-store-url" placeholder="https://mystore.myshopify.com"/>
        </div>
        <div class="field">
          <label>Admin API Access Token</label>
          <input type="password" id="shopify-token" placeholder="shpat_xxxxxxxxxxxxxxxx"/>
        </div>
        <div style="display:flex;gap:.5rem;margin-top:.6rem">
          <button class="btn btn-p" style="flex:1" onclick="saveShopify()">Connect Shopify</button>
          <button class="btn btn-g" id="shopify-disconnect-btn" style="display:none" onclick="disconnectConnector('shopify')">Disconnect</button>
        </div>
        <div id="shopify-msg" style="font-size:.78rem;margin-top:.5rem;display:none"></div>

        <div style="margin-top:.85rem;padding:.7rem;background:var(--surface);border-radius:7px;font-size:.72rem;color:var(--muted)">
          <strong style="color:var(--text)">How to get token:</strong><br/>
          Shopify Admin → Settings → Apps → Develop apps → Create app →
          Configure Admin API scopes (read_orders, read_products, read_customers, read_analytics) → Install → copy token
        </div>
      </div>

      <!-- ── Meta Ads ───────────────────────────────────────────────── -->
      <div style="background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:1.1rem">
        <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.85rem">
          <span style="font-size:1.3rem">📘</span>
          <div>
            <div style="font-weight:700;font-size:.95rem">Meta Ads</div>
            <div style="font-size:.72rem;color:var(--muted)">Real ad performance — ROAS, spend, CTR, campaigns</div>
          </div>
          <span id="meta-badge" style="margin-left:auto;font-size:.7rem;font-weight:600;
            padding:.15rem .5rem;border-radius:4px;display:none"></span>
        </div>

        <div class="field">
          <label>Access Token</label>
          <input type="password" id="meta-token" placeholder="EAAxxxxxxxxxxxxxxxx"/>
        </div>
        <div class="field">
          <label>Ad Account ID</label>
          <input type="text" id="meta-account-id" placeholder="act_1234567890  or  1234567890"/>
        </div>
        <div style="display:flex;gap:.5rem;margin-top:.6rem">
          <button class="btn btn-p" style="flex:1" onclick="saveMeta()">Connect Meta</button>
          <button class="btn btn-g" id="meta-disconnect-btn" style="display:none" onclick="disconnectConnector('meta')">Disconnect</button>
        </div>
        <div id="meta-msg" style="font-size:.78rem;margin-top:.5rem;display:none"></div>

        <div style="margin-top:.85rem;padding:.7rem;background:var(--surface);border-radius:7px;font-size:.72rem;color:var(--muted)">
          <strong style="color:var(--text)">How to get token:</strong><br/>
          Meta Business Suite → Business Settings → Users → System Users → Add system user →
          Generate token with ads_read, ads_management scopes → copy token + Ad Account ID
        </div>
      </div>

    </div>
  </div>

  <!-- Connected brands list -->
  <div class="card" style="margin-top:1.25rem">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:1rem">
      <div style="font-weight:700;font-size:.95rem">Connected Brands</div>
      <button class="btn btn-g" onclick="loadConnectors()" style="font-size:.78rem;padding:.35rem .75rem">↻ Refresh</button>
    </div>
    <div id="connectors-list">
      <div style="text-align:center;padding:1.5rem;color:var(--muted);font-size:.85rem">Loading…</div>
    </div>
  </div>

</div>
</main>

<script>
/* ── Tabs ─────────────────────────────────────────────────────────────────── */
function showTab(id, btn) {
  document.querySelectorAll('.pane').forEach(el => el.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('on'));
  document.getElementById('pane-' + id).classList.add('on');
  btn.classList.add('on');
}

/* ── Agent pipeline ───────────────────────────────────────────────────────── */
// [key, label, est_seconds]
const AGENTS = [
  ['brand_basics',       'Brand Basics',           15],
  ['content_catalog',    'Content Audit',           20],
  ['performance_ads',    'Ad Intelligence',         35],
  ['geo_visibility',     'GEO Visibility',          15],
  ['store_cro',          'Store & CRO',             20],
  ['research',           'Competitive Research',    40],
  ['social_profile',     'Social & Brand Presence', 25],
  ['social_media_audit', 'Social Media Deep Audit', 50],
];

function buildPipeline() {
  document.getElementById('a-agents').innerHTML = AGENTS.map(([k, lbl, est]) =>
    `<div class="ag-row" id="row-${k}">
       <span class="ag-ic" id="ic-${k}" style="color:var(--border)">○</span>
       <div style="flex:1;min-width:0">
         <span class="ag-name">${lbl}</span>
         <span class="ag-eta" id="eta-${k}" style="font-size:.62rem;color:#4b5563;margin-left:.35rem">~${est}s</span>
       </div>
       <span class="ag-st" id="st-${k}" style="color:var(--muted)">Pending</span>
     </div>`
  ).join('');
}

function setAgent(key, state, elapsed) {
  const ic  = document.getElementById('ic-'  + key);
  const st  = document.getElementById('st-'  + key);
  const eta = document.getElementById('eta-' + key);
  if (!ic) return;
  if (state === 'running') {
    ic.innerHTML = '<span class="ic-spin" style="color:var(--amber)">↻</span>';
    ic.style.color = 'var(--amber)';
    st.textContent = 'Running…'; st.style.color = 'var(--amber)';
    if (eta) eta.style.display = 'inline';
  } else if (state === 'done') {
    ic.innerHTML = '✓'; ic.style.color = 'var(--green)';
    st.textContent = elapsed ? elapsed + 's' : 'Done'; st.style.color = 'var(--green)';
    if (eta) eta.style.display = 'none';
  } else if (state === 'error') {
    ic.innerHTML = '✗'; ic.style.color = 'var(--red)';
    st.textContent = 'Error'; st.style.color = 'var(--red)';
    if (eta) eta.style.display = 'none';
  }
}

/* ── Feature 1: Progressive finding cards ────────────────────────────────── */
function addFindingCard(key, label, preview) {
  if (!preview) return;
  var feed = document.getElementById('live-findings');
  if (!feed) return;
  var card = document.createElement('div');
  card.className = 'finding-card';
  card.innerHTML =
    '<div class="finding-agent">' + (label || key) + '</div>' +
    '<div class="finding-headline">' + (preview.headline || '') + '</div>' +
    (preview.stat ? '<div class="finding-stat">' + preview.stat + '</div>' : '') +
    (preview.insight ? '<div class="finding-insight">' + preview.insight + '</div>' : '');
  feed.appendChild(card);
  card.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

/* ── Feature 2: Live data counter ────────────────────────────────────────── */
var _dataPoints = 0;
var AGENT_DATA_COUNTS = {
  brand_basics: 47, content_catalog: 123, performance_ads: 89,
  geo_visibility: 64, store_cro: 38, research: 156,
  social_profile: 74, social_media_audit: 210,
};

function countUp(el, from, to, ms) {
  var start = performance.now();
  var diff = to - from;
  function tick(now) {
    var t = Math.min((now - start) / ms, 1);
    // ease-out cubic
    var ease = 1 - Math.pow(1 - t, 3);
    el.textContent = Math.round(from + diff * ease).toLocaleString();
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function updateDataCounter(final) {
  var el = document.getElementById('data-counter');
  var numEl = document.getElementById('data-count-num');
  if (!el || !numEl) return;
  el.style.display = 'block';
  if (final) {
    el.innerHTML = 'Analysed <strong>801</strong> data points across 8 intelligence agents';
    return;
  }
  var prev = parseInt(numEl.textContent.replace(/,/g, '')) || 0;
  countUp(numEl, prev, _dataPoints, 1000);
}

/* ── Feature 3: Rotating insights ───────────────────────────────────────── */
var _insightsTimer = null;
var _insightIdx = 0;
var INSIGHTS = [
  '💡 Brands with FAQ schema are 2.3x more likely to appear in AI search answers.',
  '📱 Mobile PageSpeed above 80 correlates with 12% higher conversion rate on average.',
  '🎯 UGC ads outperform studio product shots by 20-30% CTR in Indian D2C.',
  '🔍 70% of purchase journeys now start with an AI engine query, not Google Search.',
  '⭐ Review velocity (reviews per week) is a stronger demand signal than total review count.',
  '🛒 Adding a trust badge above Add-to-Cart lifts conversion 3-5% with zero dev effort.',
  '📦 Brands that show delivery date at cart stage (not checkout) see 8% lower abandonment.',
  '💬 ‘Benefit-first’ PDP headlines outperform feature-first by 15% in time-on-page.',
  '🎨 Carousel ads have 72% higher engagement than static for fashion categories.',
  '🔗 Brands with Wikipedia pages are cited by ChatGPT 4x more than those without.'
];

function _showInsight(idx) {
  var textEl = document.getElementById('insight-text');
  var dotsEl = document.getElementById('insight-dots');
  if (!textEl) return;
  textEl.style.opacity = '0';
  setTimeout(function() {
    textEl.textContent = INSIGHTS[idx];
    textEl.style.opacity = '1';
    if (dotsEl) {
      dotsEl.innerHTML = INSIGHTS.map(function(_, i) {
        return '<span class="insight-dot' + (i === idx ? ' on' : '') + '"></span>';
      }).join('');
    }
  }, 280);
}

function startInsightsRotator() {
  var panel = document.getElementById('insights-panel');
  if (!panel) return;
  panel.style.display = 'block';
  _insightIdx = Math.floor(Math.random() * INSIGHTS.length);
  _showInsight(_insightIdx);
  _insightsTimer = setInterval(function() {
    _insightIdx = (_insightIdx + 1) % INSIGHTS.length;
    _showInsight(_insightIdx);
  }, 8000);
}

function stopInsightsRotator() {
  if (_insightsTimer) { clearInterval(_insightsTimer); _insightsTimer = null; }
  var panel = document.getElementById('insights-panel');
  if (panel) panel.style.display = 'none';
}

/* ── TRIBE v2 background polling ─────────────────────────────────────────── */
function _startTribePolling(auditId, reportUrl) {
  var tribeBar = document.createElement('div');
  tribeBar.id = 'tribe-bar';
  tribeBar.style.cssText = 'margin-top:.75rem;padding:.5rem .85rem;border-radius:8px;' +
    'background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.25);' +
    'font-size:.8rem;color:var(--amber);display:flex;align-items:center;gap:.5rem';
  tribeBar.innerHTML = '<span class="phase3-dots"><span></span><span></span>' +
    '<span></span><span></span></span>' +
    '<span>TRIBE v2 fMRI running in background — brain maps will appear in report when ready</span>';
  var res = document.getElementById('a-result');
  if (res) res.appendChild(tribeBar);

  var _pollCount = 0;
  var _pollMax = 30; // 30 × 20s = 10 min hard cap
  var _poll = setInterval(function() {
    _pollCount++;
    if (_pollCount > _pollMax) {
      clearInterval(_poll);
      var bar = document.getElementById('tribe-bar');
      if (bar) {
        bar.style.color = 'var(--muted)';
        bar.innerHTML = 'Deep Visual Analysis timed out — brain maps may still be processing. '
          + '<a href="' + (reportUrl || '/report/' + auditId) + '" target="_blank" style="color:var(--blue)">Check report</a>';
      }
      return;
    }
    fetch('/audit/' + auditId + '/tribe-status')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        if (d.status === 'complete' || d.status === 'failed') {
          clearInterval(_poll);
          var bar = document.getElementById('tribe-bar');
          if (bar) {
            if (d.status === 'complete') {
              bar.style.background = 'rgba(34,197,94,.08)';
              bar.style.borderColor = 'rgba(34,197,94,.25)';
              bar.style.color = 'var(--green)';
              bar.innerHTML = '&#x2713; TRIBE v2 brain maps ready — ' +
                '<a href="' + (reportUrl || '/report/' + auditId) + '" ' +
                'target="_blank" style="color:var(--green)">open report to view</a>';
            } else {
              bar.style.color = 'var(--muted)';
              bar.innerHTML = 'Deep Visual Analysis could not complete: ' + (d.error || 'unknown error');
            }
          }
        }
      })
      .catch(function() {}); // silent — network blip during long background job
  }, 20000); // poll every 20s
}

/* ── Feature 4: Phase 3 parallel indicator ───────────────────────────────── */
function setPhase3Running() {
  var P3 = ['content_catalog', 'performance_ads', 'geo_visibility', 'store_cro'];
  P3.forEach(function(k, i) {
    var ic = document.getElementById('ic-' + k);
    var st = document.getElementById('st-' + k);
    if (ic) ic.innerHTML = '<span class="phase3-dots"><span></span><span></span><span></span><span></span></span>';
    if (ic) ic.style.color = 'var(--amber)';
    if (st) { st.textContent = i === 0 ? 'In parallel' : '…'; st.style.color = 'var(--amber)'; }
  });
  // Annotate first row to signal parallelism
  var firstRow = document.getElementById('row-content_catalog');
  if (firstRow) {
    var nm = firstRow.querySelector('.ag-name');
    if (nm) nm.innerHTML = 'Content Audit <span style="font-size:.63rem;color:var(--amber);font-weight:700;margin-left:.3rem">✜ +3 parallel</span>';
  }
}

function restorePhase3AgentName(key) {
  var row = document.getElementById('row-' + key);
  if (!row) return;
  var nm = row.querySelector('.ag-name');
  if (!nm) return;
  var entry = AGENTS.find(function(a) { return a[0] === key; });
  if (entry) nm.textContent = entry[1];
}

/* ── FIX 3: Audit example chips ───────────────────────────────────────────── */
function loadAuditExample(url, mode) {
  document.getElementById('a-url').value = url;
  if (mode === 'demo') {
    loadDemoAudit();
  } else {
    startAudit();
  }
}

async function loadDemoAudit() {
  const btn = document.getElementById('a-btn');
  btn.disabled = true; btn.textContent = '…';
  document.getElementById('a-result').innerHTML = '';
  document.getElementById('a-report-wrap').style.display = 'none';
  document.getElementById('a-pipeline').style.display = 'block';
  buildPipeline();

  // Activate split layout
  document.getElementById('audit-layout').classList.add('audit-split');

  // Pre-cached — mark all agents done instantly
  AGENTS.forEach(([k]) => setAgent(k, 'done', '—'));
  document.getElementById('a-prog').style.width = '100%';

  // Load /demo into the inline iframe
  document.getElementById('a-report-frame').src = '/demo';
  document.getElementById('a-report-wrap').style.display = 'block';
  btn.disabled = false; btn.textContent = 'Run Audit →';
}

/* ── FIX 2: PDF print via iframe contentWindow ────────────────────────────── */
function printReport() {
  const iframe = document.getElementById('a-report-frame');
  if (iframe && iframe.contentWindow) {
    iframe.contentWindow.print();
  }
}

/* ── Brand Audit ──────────────────────────────────────────────────────────── */
async function startAudit() {
  const url = document.getElementById('a-url').value.trim();
  if (!url) { document.getElementById('a-url').focus(); return; }

  const btn = document.getElementById('a-btn');
  btn.disabled = true; btn.textContent = '…';
  document.getElementById('a-result').innerHTML = '';
  document.getElementById('a-report-wrap').style.display = 'none';
  document.getElementById('a-pipeline').style.display = 'block';
  document.getElementById('a-prog').style.width = '0';
  buildPipeline();

  // Activate split layout
  document.getElementById('audit-layout').classList.add('audit-split');

  // Reset live-experience state
  _dataPoints = 0;
  document.getElementById('live-findings').innerHTML = '';
  var liveSec0 = document.getElementById('live-sections');
  if (liveSec0) { liveSec0.innerHTML = ''; liveSec0.style.display = 'none'; }
  var dc = document.getElementById('data-counter');
  if (dc) { dc.style.display = 'none'; }
  var dcn = document.getElementById('data-count-num');
  if (dcn) dcn.textContent = '0';
  stopInsightsRotator();

  const deepVisual = document.getElementById('a-deep-visual')?.checked || false;

  let auditId, _fromCache = false;
  try {
    const r = await fetch('/audit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, deep_visual: deepVisual}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Failed to start audit');
    auditId = d.audit_id;
    _fromCache = d.from_cache || false;
    btn.textContent = 'Running…';
    if (deepVisual) {
      // Update social_media_audit ETA to reflect TRIBE v2 time
      const etaEl = document.getElementById('eta-social_media_audit');
      if (etaEl) etaEl.textContent = '~20min (TRIBE v2)';
    }
    startInsightsRotator();
  } catch (e) {
    document.getElementById('a-pipeline').style.display = 'none';
    showErr('a-result', e.message);
    btn.disabled = false; btn.textContent = 'Run Audit →';
    return;
  }

  /* SSE stream */
  const es = new EventSource('/audit/stream/' + auditId + (deepVisual ? '?deep_visual=1' : ''));

  es.onmessage = ev => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }

    if (m.status === 'cache_hit') {
      _fromCache = true;
      AGENTS.forEach(([k]) => setAgent(k, 'done', '—'));
      document.getElementById('a-prog').style.width = '100%';

    } else if (m.status === 'gathering') {
      // Phase 1: parallel data gathering
      AGENTS.forEach(([k]) => {
        var ic = document.getElementById('ic-' + k);
        var st = document.getElementById('st-' + k);
        if (ic) { ic.innerHTML = '<span style="color:var(--border)">○</span>'; ic.style.color = 'var(--border)'; }
        if (st) { st.textContent = 'Queued'; st.style.color = 'var(--muted)'; }
      });

    } else if (m.status === 'running') {
      setAgent(m.key, 'running');

    } else if (m.status === 'phase3_start') {
      // Feature 4: show 4 parallel pulsing dots
      setPhase3Running();

    } else if (m.status === 'phase3_done') {
      // All 6 parallel agents done — tick any that haven't been individually ticked yet
      var p3keys = ['content_catalog', 'performance_ads', 'geo_visibility', 'store_cro', 'research', 'social_profile'];
      p3keys.forEach(function(k) {
        restorePhase3AgentName(k);
        setAgent(k, 'done');
      });
      if (m.progress_pct != null)
        document.getElementById('a-prog').style.width = m.progress_pct + '%';
      // Show finding cards for all 6 parallel agents (agents array now always present)
      if (m.agents) {
        m.agents.forEach(function(a) {
          if (a.preview) addFindingCard(a.key, a.preview.label, a.preview);
        });
      }
      // Increment counter for all 6 parallel agents
      _dataPoints += (AGENT_DATA_COUNTS.content_catalog || 0)
        + (AGENT_DATA_COUNTS.performance_ads || 0)
        + (AGENT_DATA_COUNTS.geo_visibility || 0)
        + (AGENT_DATA_COUNTS.store_cro || 0)
        + (AGENT_DATA_COUNTS.research || 0)
        + (AGENT_DATA_COUNTS.social_profile || 0);
      updateDataCounter();

    } else if (m.status === 'tribe_started') {
      // TRIBE v2 fMRI processing started — update social_media_audit row status
      setAgent('social_media_audit', 'running');

    } else if (m.status === 'tribe_complete') {
      // TRIBE v2 done — already marked done via the column-watch done event
      console.log('[SSE] TRIBE v2 complete, reels:', m.reels_processed);

    } else if (m.status === 'done' && m.key) {
      setAgent(m.key, 'done', m.elapsed);
      if (m.progress_pct != null)
        document.getElementById('a-prog').style.width = m.progress_pct + '%';
      // Feature 2: increment counter (finding cards removed — sections are the live feed)
      var inc = AGENT_DATA_COUNTS[m.key] || 0;
      if (inc) { _dataPoints += inc; updateDataCounter(); }
      // Progressive section reveal — fetch and append beautifully styled section HTML
      (function(key, id) {
        fetch('/report/section/' + id + '/' + key)
          .then(function(r) { return r.ok ? r.text() : Promise.reject(r.status); })
          .then(function(html) {
            if (!html) return;
            var ls = document.getElementById('live-sections');
            if (!ls) return;
            ls.style.display = 'block';
            // Auto-collapse all previously open sections except brand_basics
            ls.querySelectorAll('[data-agent] details[open]').forEach(function(d) {
              var ag = d.closest('[data-agent]');
              if (ag && ag.dataset.agent !== 'brand_basics') d.removeAttribute('open');
            });
            var wrapper = document.createElement('div');
            wrapper.setAttribute('data-agent', key);
            wrapper.innerHTML = html;
            ls.appendChild(wrapper);
            wrapper.scrollIntoView({behavior: 'smooth', block: 'nearest'});
          })
          .catch(function(err) {
            var ls = document.getElementById('live-sections');
            if (!ls) return;
            ls.style.display = 'block';
            var wrapper = document.createElement('div');
            wrapper.setAttribute('data-agent', key);
            wrapper.innerHTML = '<div style="margin:.75rem 0;padding:.85rem 1rem;'
              + 'background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;'
              + 'color:#6b7280;font-size:.82rem">&#9888; Section failed to load'
              + ' — <a href="/report/' + id + '" target="_blank" style="color:var(--blue)">open full report</a></div>';
            ls.appendChild(wrapper);
          });
      })(m.key, auditId);

    } else if (m.status === 'complete') {
      document.getElementById('a-prog').style.width = '100%';
      updateDataCounter(true);
      stopInsightsRotator();
      es.close();
      // Hide finding cards — pipeline stays visible in sidebar
      var feed = document.getElementById('live-findings');
      if (feed) feed.style.display = 'none';
      // Collapse all live sections except brand_basics, keep visible as the report
      var liveSec = document.getElementById('live-sections');
      var hasSections = liveSec && liveSec.querySelectorAll('[data-agent]').length > 0;
      var reportUrl = m.report_url;
      if (hasSections) {
        liveSec.querySelectorAll('[data-agent] details').forEach(function(d) {
          var agentDiv = d.closest('[data-agent]');
          if (agentDiv && agentDiv.dataset.agent !== 'brand_basics') {
            d.removeAttribute('open');
          }
        });
        liveSec.style.display = 'block';
        // Show toolbar without iframe
        document.getElementById('a-report-wrap').style.display = 'block';
        document.getElementById('a-report-frame').style.display = 'none';
        var toolbar = document.querySelector('.report-toolbar');
        if (toolbar && reportUrl) {
          var dlBtn = toolbar.querySelector('button');
          if (dlBtn) dlBtn.style.display = 'none';
          toolbar.insertAdjacentHTML('beforeend',
            '<a href="' + reportUrl + '" target="_blank" class="btn btn-g" ' +
            'style="font-size:.8rem;padding:.38rem .85rem;text-decoration:none">↗ Full Report</a>');
        }
      } else {
        // Cache hit — no live sections, load iframe
        document.getElementById('a-report-frame').src = reportUrl;
        document.getElementById('a-report-frame').style.display = '';
        document.getElementById('a-report-wrap').style.display = 'block';
      }
      const cacheBadge = document.getElementById('a-cache-badge');
      if (cacheBadge) cacheBadge.style.display = (_fromCache || m.from_cache) ? 'inline-flex' : 'none';
      document.getElementById('a-result').innerHTML = '';
      btn.disabled = false; btn.textContent = 'Run Audit →';
      if (_brandsLoaded) loadBrands();
      // If Deep Visual was requested, poll for TRIBE v2 brain maps in background
      if (deepVisual && auditId) _startTribePolling(auditId, reportUrl);

    } else if (m.status === 'failed') {
      es.close();
      stopInsightsRotator();
      showErr('a-result', m.error || 'Audit failed');
      btn.disabled = false; btn.textContent = 'Run Audit →';

    } else if (m.status === 'timeout') {
      es.close();
      stopInsightsRotator();
      document.getElementById('a-result').innerHTML =
        '<div class="err-box">Audit is taking longer than expected. ' +
        '<a href="/report/' + auditId + '" target="_blank">Open report</a> when ready.</div>';
      btn.disabled = false; btn.textContent = 'Run Audit →';
    }
  };
  es.onerror = () => {
    es.close();
    stopInsightsRotator();
    document.getElementById('a-result').innerHTML =
      '<div class="err-box">Connection lost — ' +
      '<a href="/report/' + auditId + '" target="_blank">check report</a>.</div>';
    btn.disabled = false; btn.textContent = 'Run Audit →';
  };
}

/* ── FIX 3: Virality example data ─────────────────────────────────────────── */
const VIRALITY_EXAMPLES = [
  {
    url: 'https://rarerabbit.in/products/rare-rabbit-men-shirts',
    product_name: 'Rare Rabbit Oxford Shirt',
    description: '100% premium cotton Oxford weave shirt. Available in 12 colors. Regular fit.',
    category: 'premium menswear',
    _cached: true,
    _idx: 0,
  },
  {
    url: 'https://www.boat-lifestyle.com/products/airdopes-141',
    product_name: 'boAt Airdopes 141',
    description: 'True wireless earbuds with 42 hours playtime, BEAST™ mode for gaming, IPX4 water resistance, and low-latency Bluetooth 5.1. Available in 10 colours.',
    category: 'audio / TWS earbuds',
    _cached: true,
    _idx: 3,
  },
  {
    url: '',
    product_name: '3-Pack Basic White T-Shirt',
    description: 'Pack of 3 plain white cotton t-shirts. Standard fit. Machine washable.',
    category: 'basics / essentials',
    _cached: false,
  },
];

let _examples = null;

async function loadViralityExample(idx) {
  const ex    = VIRALITY_EXAMPLES[idx];
  const card  = document.getElementById('exc-' + idx);
  const badge = document.getElementById('excb-' + idx);

  // Fill form fields
  document.getElementById('v-url').value  = ex.url || '';
  document.getElementById('v-name').value = ex.product_name || '';
  document.getElementById('v-desc').value = ex.description || '';
  document.getElementById('v-cat').value  = ex.category || '';

  // Mark card as loading
  document.querySelectorAll('.ex-card').forEach(c => c.classList.remove('active'));
  card.classList.add('active');
  const origLabel = badge.textContent;
  badge.textContent = '(Example)';
  badge.classList.add('loading');

  try {
    if (ex._cached) {
      if (!_examples) {
        const r = await fetch('/demo/virality');
        if (!r.ok) throw new Error('Could not load demo data');
        _examples = await r.json();
      }
      renderVirality(_examples[ex._idx || 0]);
    } else {
      await startVirality();
    }
  } catch (e) {
    showErr('v-result', 'Could not load example: ' + e.message);
  } finally {
    badge.textContent = origLabel;
    badge.classList.remove('loading');
    card.classList.remove('active');
  }
}

/* ── Virality Predictor ───────────────────────────────────────────────────── */
async function startVirality() {
  const url  = document.getElementById('v-url').value.trim();
  const name = document.getElementById('v-name').value.trim();
  const desc = document.getElementById('v-desc').value.trim();
  const cat  = document.getElementById('v-cat').value.trim();

  if (!url && !name) {
    showErr('v-result', 'Enter at least a product URL or product name.');
    return;
  }

  const btn = document.getElementById('v-btn');
  btn.disabled = true;
  document.getElementById('v-loading').style.display = 'block';
  document.getElementById('v-result').innerHTML = '';

  try {
    const r = await fetch('/virality', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        url:          url  || null,
        product_name: name || null,
        description:  desc || null,
        category:     cat  || null,
      }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Scoring failed');
    renderVirality(d);
  } catch (e) {
    showErr('v-result', e.message);
  } finally {
    btn.disabled = false;
    document.getElementById('v-loading').style.display = 'none';
  }
}

function gradeColor(grade) {
  const g = ((grade || '')[0] || 'D').toUpperCase();
  return {S:'#f59e0b', A:'#22c55e', B:'#3b82f6', C:'#eab308', D:'#ef4444'}[g] || '#6b7280';
}

const DIM_KEYS = [
  ['emotional_trigger',      'Emotional Trigger'],
  ['visual_stopping_power',  'Visual Stopping Power'],
  ['transformation_clarity', 'Transformation Clarity'],
  ['social_currency',        'Social Currency'],
  ['trend_alignment',        'Trend Alignment'],
  ['share_trigger',          'Share Trigger'],
  ['hook_strength',          'Hook Strength'],
];

function renderVirality(data) {
  // If there's a pre-rendered server report (e.g. TRIBE v2), show it in an iframe
  if (data.virality_card_url) {
    document.getElementById('v-result').innerHTML =
      `<iframe src="${data.virality_card_url}" frameborder="0"
        style="width:100%;height:82vh;min-height:600px;border:1px solid #1e1e1e;
               border-radius:10px;background:#0a0a0a;display:block"></iframe>`;
    return;
  }
  const score    = data.score || 0;
  const grade    = data.grade || 'D';
  const analysis = data.analysis || data;
  const color    = gradeColor(grade);
  const name     = data.product_name || analysis.product_name || 'Product';
  const dims     = analysis.dimensions || {};

  const dimsHtml = DIM_KEYS.map(([k, lbl]) => {
    const d   = dims[k] || {};
    const s   = typeof d === 'object' ? (d.score || 0) : (d || 0);
    const rsn = typeof d === 'object' ? (d.reasoning || '') : '';
    const pct = Math.round(s / 10 * 100);
    return `<div class="dim-row">
      <div class="dim-top">
        <span class="dim-label">${lbl}</span>
        <span class="dim-score" style="color:${color}">${s}/10</span>
      </div>
      <div class="dim-bg">
        <div class="dim-fill" style="width:${pct}%;background:${color}"></div>
      </div>
      ${rsn ? `<div class="dim-rsn">${rsn}</div>` : ''}
    </div>`;
  }).join('');

  const hook   = analysis.killer_hook || '';
  const angles = analysis.viral_content_angles || [];
  const plats  = analysis.best_platforms || [];

  const hookHtml = hook ? `
    <div class="hook-box">
      <div class="hook-lbl">✦ Killer Hook</div>
      <div class="hook-txt">"${hook}"</div>
    </div>` : '';

  // angles may be strings OR objects {angle, hook_line, best_platform, ...}
  const angHtml = angles.length ? '<div class="sh">Viral Content Angles</div>' +
    angles.map(function(a, i) {
      var txt   = typeof a === 'string' ? a : (a.angle || a.hook_line || JSON.stringify(a));
      var extra = typeof a === 'object' && a.best_platform
        ? ' <span style="font-size:.68rem;color:var(--muted);margin-left:.3rem">· ' + esc(a.best_platform) + '</span>'
        : '';
      return '<div class="ang-item"><span class="ang-n" style="background:' + color + '">' + (i+1) + '</span>'
        + '<span>' + esc(txt) + extra + '</span></div>';
    }).join('') : '';

  const platHtml = plats.length ? `<div class="sh">Best Platforms</div>
    <div class="plat-wrap">${plats.map(p => `<span class="plat">${p}</span>`).join('')}</div>` : '';

  /* Visual Analysis — from Llama 4 Scout image analysis */
  let visualHtml = '';
  const va = data.llm_visual_analysis;
  // Only render if we have at least one real field (not just an empty object)
  const _vaHasData = va && typeof va === 'object' && !va._parse_error &&
    (va.visual_hook_strength != null || va.dominant_emotion || (va.visual_strengths && va.visual_strengths.length));
  if (_vaHasData) {
    const hookScore = typeof va.visual_hook_strength === 'number' ? va.visual_hook_strength : 0;
    const hookPct   = Math.round(hookScore / 10 * 100);
    const emotion   = va.dominant_emotion || '';
    const lvsLabel  = va.lifestyle_vs_studio ? va.lifestyle_vs_studio.replace(/-/g, ' ') : '';
    const rec       = va.recommended_visual_change || '';
    const strengths = Array.isArray(va.visual_strengths) ? va.visual_strengths.slice(0, 2) : [];
    const gaps      = Array.isArray(va.visual_gaps)      ? va.visual_gaps.slice(0, 2)      : [];
    visualHtml =
      '<div class="sh">Visual Analysis <span style="font-size:.62rem;color:var(--muted);font-weight:400">· Llama 4 Scout</span></div>' +
      '<div class="dim-row">' +
        '<div class="dim-top">' +
          '<span class="dim-label">Visual Hook Strength</span>' +
          '<span class="dim-score" style="color:' + color + '">' + hookScore + '/10</span>' +
        '</div>' +
        '<div class="dim-bg"><div class="dim-fill" style="width:' + hookPct + '%;background:' + color + '"></div></div>' +
      '</div>' +
      (emotion   ? '<div style="margin:.45rem 0 .3rem;display:flex;gap:.35rem;flex-wrap:wrap">' +
                     '<span class="plat" style="border-color:' + color + ';color:' + color + '">' + emotion + '</span>' +
                     (lvsLabel ? '<span class="plat">' + lvsLabel + '</span>' : '') +
                   '</div>' : '') +
      (strengths.length ? '<div style="font-size:.73rem;color:var(--green);margin-top:.3rem">✓ ' + strengths.join(' · ') + '</div>' : '') +
      (gaps.length      ? '<div style="font-size:.73rem;color:var(--muted);margin-top:.2rem">△ ' + gaps.join(' · ') + '</div>'      : '') +
      (rec ? '<div style="font-size:.78rem;color:var(--amber);margin-top:.45rem;line-height:1.45">💡 ' + rec + '</div>' : '');
  }

  /* Neural Engagement — TRIBE v2 fMRI prediction */
  let neHtml = '';
  const ne = data.neural_engagement;
  if (ne && (ne.neural_engagement_score != null || ne.error)) {
    const neScore = ne.neural_engagement_score;
    const neTier  = ne.tier || '';
    const tierColor = neTier === 'High' ? 'var(--green)' : neTier === 'Medium' ? 'var(--amber)' : 'var(--red)';
    const nePct   = neScore != null ? Math.round(neScore) : 0;
    neHtml =
      '<div class="sh">Neural Engagement' +
        '<span style="font-size:.62rem;color:var(--muted);font-weight:400"> · Meta TRIBE v2</span>' +
      '</div>' +
      (ne.error && !neScore
        ? '<div style="margin:.5rem 0 .65rem;padding:.7rem;background:#0f172a;' +
            'border-radius:8px;border:1px dashed var(--border);' +
            'font-size:.8rem;color:var(--muted);text-align:center">' +
            '⚠ ' + esc(ne.error) +
          '</div>'
        : '<div style="margin:.45rem 0 .75rem">' +
            '<div style="display:flex;align-items:baseline;gap:.55rem;margin-bottom:.5rem">' +
              '<span style="font-size:2.4rem;font-weight:900;line-height:1;color:' + tierColor + '">' + nePct + '</span>' +
              '<span style="font-size:.8rem;color:var(--muted)">/100</span>' +
              (neTier ? '<span style="display:inline-block;padding:.18rem .65rem;border-radius:999px;' +
                'font-size:.75rem;font-weight:700;background:' + tierColor + ';color:#000;margin-left:.2rem">' + esc(neTier) + ' Neural Engagement</span>' : '') +
            '</div>' +
            '<div class="dim-bg" style="margin-bottom:.5rem">' +
              '<div class="dim-fill" style="width:' + nePct + '%;background:' + tierColor + '"></div>' +
            '</div>' +
            (ne.consistency_score != null
              ? '<div style="display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.4rem">' +
                  '<span style="font-size:.71rem;padding:.16rem .48rem;border-radius:5px;background:#1e1e1e;color:var(--muted)">' +
                    'Consistency ' + ne.consistency_score + '%</span>' +
                  (ne.early_hook_strong
                    ? '<span style="font-size:.71rem;padding:.16rem .48rem;border-radius:5px;background:rgba(34,197,94,.1);color:var(--green)">Strong opening hook</span>'
                    : ne.early_hook_ratio != null && ne.early_hook_ratio < 0.85
                      ? '<span style="font-size:.71rem;padding:.16rem .48rem;border-radius:5px;background:rgba(239,68,68,.08);color:var(--red)">Weak opening hook</span>'
                      : '') +
                  (ne.n_trs_analyzed
                    ? '<span style="font-size:.71rem;padding:.16rem .48rem;border-radius:5px;background:#1e1e1e;color:var(--muted)">' + ne.n_trs_analyzed + ' TRs analysed</span>'
                    : '') +
                '</div>'
              : '') +
            (ne.interpretation
              ? '<div style="font-size:.78rem;color:#cbd5e1;line-height:1.5;' +
                  'padding:.5rem .65rem;background:#0f172a;border-radius:6px;' +
                  'border-left:2px solid ' + tierColor + '">' + esc(ne.interpretation) + '</div>'
              : '') +
          '</div>') +
      '<div style="font-size:.61rem;color:#374151;margin-top:.35rem">' +
        'Research Preview — powered by Meta TRIBE v2 · CC-BY-NC-4.0 (non-commercial research use only)' +
      '</div>';
  }

  /* Visual Attention Map — DeepGaze IIE heatmap */
  let attnHtml = '';
  const va2 = data.visual_attention;
  // Only show when DeepGaze actually ran (not just "analysis not run" placeholder)
  if (va2 && va2.error !== 'analysis not run') {
    const focusLabel = {
      'upper-left': 'Upper Left', 'upper-center': 'Upper Center', 'upper-right': 'Upper Right',
      'middle-left': 'Middle Left', 'middle-center': 'Center', 'middle-right': 'Middle Right',
      'lower-left': 'Lower Left', 'lower-center': 'Lower Center', 'lower-right': 'Lower Right',
    };
    const distColor = va2.attention_distribution === 'concentrated' ? 'var(--green)' : 'var(--amber)';
    const focusName = focusLabel[va2.attention_focus] || va2.attention_focus || '—';
    attnHtml =
      '<div class="sh">Visual Attention Map' +
        '<span style="font-size:.62rem;color:var(--muted);font-weight:400"> · DeepGaze IIE</span>' +
      '</div>' +
      (va2.heatmap_available && va2.heatmap_base64
        ? '<div style="position:relative;margin:.5rem 0 .65rem;border-radius:8px;overflow:hidden">' +
            '<img src="data:image/png;base64,' + va2.heatmap_base64 + '" ' +
              'style="width:100%;display:block;border-radius:8px" ' +
              'alt="Visual attention heatmap — red = high attention"/>' +
            '<div style="position:absolute;bottom:0;left:0;right:0;' +
              'background:linear-gradient(transparent,rgba(0,0,0,.65));' +
              'padding:.4rem .6rem;font-size:.67rem;color:#e8e8e8">' +
              '🔴 Red = high attention &nbsp;·&nbsp; 🔵 Blue = low attention</div>' +
          '</div>'
        : '<div style="margin:.5rem 0 .65rem;padding:.75rem;background:#0f172a;' +
            'border-radius:8px;border:1px dashed var(--border);' +
            'font-size:.8rem;color:var(--muted);text-align:center">' +
            '⚠ Image not accessible — visual attention analysis unavailable' +
            (va2.error ? '<div style="font-size:.68rem;margin-top:.3rem;color:#6b7280">' + esc(va2.error) + '</div>' : '') +
          '</div>') +
      '<div style="display:flex;gap:.6rem;flex-wrap:wrap;margin-bottom:.45rem">' +
        (va2.attention_focus
          ? '<span style="font-size:.72rem;padding:.18rem .5rem;border-radius:5px;' +
              'background:#1e2d45;color:#60a5fa">Focus: ' + esc(focusName) + '</span>'
          : '') +
        (va2.attention_distribution
          ? '<span style="font-size:.72rem;padding:.18rem .5rem;border-radius:5px;' +
              'background:#1e1e1e;color:' + distColor + '">' + esc(va2.attention_distribution) + '</span>'
          : '') +
        (va2.concentration_pct != null
          ? '<span style="font-size:.72rem;padding:.18rem .5rem;border-radius:5px;' +
              'background:#1e1e1e;color:var(--muted)">Top 90% mass in ' + va2.concentration_pct + '% of pixels</span>'
          : '') +
      '</div>' +
      (va2.interpretation
        ? '<div style="font-size:.78rem;color:#cbd5e1;line-height:1.5;' +
            'padding:.5rem .65rem;background:#0f172a;border-radius:6px;' +
            'border-left:2px solid var(--blue)">' + esc(va2.interpretation) + '</div>'
        : '') +
      '<div style="font-size:.62rem;color:#374151;margin-top:.4rem">Powered by DeepGaze IIE — predicts human visual attention</div>';
  }

  /* Brain activation map */
  let brainHtml = '';
  const bm = data.brain_map_svg;
  const bn = data.brain_network_scores || {};
  const bmSrc = data.brain_map_source || '';
  if (bm) {
    const srcLabel = bmSrc === 'tribe_v2'
      ? 'Meta TRIBE v2 · fMRI predictions'
      : 'Estimated · based on virality dimensions';
    const srcColor = bmSrc === 'tribe_v2' ? 'var(--green)' : 'var(--amber)';
    brainHtml =
      '<div class="sh">Brain Activation Map' +
        '<span style="font-size:.62rem;color:var(--muted);font-weight:400"> · How this content triggers the mind</span>' +
      '</div>' +
      '<div style="margin:.5rem 0 .75rem">' + bm + '</div>' +
      '<div style="font-size:.61rem;color:#374151;margin-top:.25rem">' +
        'Source: <span style="color:' + srcColor + '">' + srcLabel + '</span>' +
        ' &nbsp;·&nbsp; CC-BY-NC-4.0 (non-commercial research use only)' +
      '</div>';
  }

  document.getElementById('v-result').innerHTML = `
    <div class="card">
      <div class="v-center">
        <div style="font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;
          color:var(--muted);margin-bottom:.4rem">${name}</div>
        <div class="v-num" style="color:${color}">${score}</div>
        <div class="v-denom">/100</div>
        <div class="v-badge" style="background:${color}">${grade}</div>
      </div>
      ${brainHtml}
      <div class="sh">Score Breakdown</div>
      ${dimsHtml}
      ${visualHtml}
      ${attnHtml}
      ${neHtml}
      ${hookHtml}
      ${angHtml}
      ${platHtml}
    </div>`;
}

function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* ── TRIBE v2 Video Neural Analysis ──────────────────────────────────────── */
async function startVideoAnalysis() {
  const url   = document.getElementById('nv-url').value.trim();
  const label = document.getElementById('nv-label').value.trim();
  if (!url) {
    document.getElementById('nv-result').innerHTML =
      '<div class="err-box">⚠ Enter a video URL to analyze.</div>';
    return;
  }

  const btn = document.getElementById('nv-btn');
  btn.disabled = true;
  document.getElementById('nv-loading').style.display = 'block';
  document.getElementById('nv-result').innerHTML = '';

  try {
    const r = await fetch('/analyze-video', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({video_url: url, label: label || ''}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Analysis failed');
    renderVideoAnalysis(d);
  } catch (e) {
    document.getElementById('nv-result').innerHTML =
      '<div class="err-box">⚠ ' + esc(e.message) + '</div>';
  } finally {
    btn.disabled = false;
    document.getElementById('nv-loading').style.display = 'none';
  }
}

function renderVideoAnalysis(d) {
  const score    = d.neural_engagement_score;
  const tier     = d.tier || '';
  const tierColor = tier === 'High' ? 'var(--green)' : tier === 'Medium' ? 'var(--amber)' : 'var(--red)';
  const nePct    = score != null ? Math.round(score) : 0;
  const bmSrc    = d.brain_map_source || '';
  const srcLabel = bmSrc === 'tribe_v2' ? 'Meta TRIBE v2 · fMRI' : 'Estimated';
  const srcColor = bmSrc === 'tribe_v2' ? 'var(--green)' : 'var(--amber)';

  let html = '<div class="card">';

  if (d.error && score == null) {
    html += '<div style="color:var(--red);font-size:.85rem;padding:.5rem">⚠ ' + esc(d.error) + '</div>';
  } else {
    html +=
      '<div style="display:flex;align-items:baseline;gap:.55rem;margin-bottom:.75rem">' +
        '<span style="font-size:2.6rem;font-weight:900;line-height:1;color:' + tierColor + '">' + nePct + '</span>' +
        '<span style="font-size:.85rem;color:var(--muted)">/100 Neural Engagement</span>' +
        (tier ? '<span style="display:inline-block;padding:.18rem .65rem;border-radius:999px;' +
          'font-size:.75rem;font-weight:700;background:' + tierColor + ';color:#000">' + esc(tier) + '</span>' : '') +
      '</div>' +
      '<div class="dim-bg" style="margin-bottom:.75rem">' +
        '<div class="dim-fill" style="width:' + nePct + '%;background:' + tierColor + '"></div>' +
      '</div>' +
      (d.consistency_score != null
        ? '<div style="display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:.65rem">' +
            '<span style="font-size:.71rem;padding:.16rem .48rem;border-radius:5px;background:#1e1e1e;color:var(--muted)">' +
              'Consistency ' + d.consistency_score + '%</span>' +
            (d.n_trs_analyzed
              ? '<span style="font-size:.71rem;padding:.16rem .48rem;border-radius:5px;background:#1e1e1e;color:var(--muted)">' + d.n_trs_analyzed + ' TRs</span>'
              : '') +
            (d.early_hook_strong
              ? '<span style="font-size:.71rem;padding:.16rem .48rem;border-radius:5px;background:rgba(34,197,94,.1);color:var(--green)">Strong opening hook</span>'
              : (d.early_hook_ratio != null && d.early_hook_ratio < 0.85
                ? '<span style="font-size:.71rem;padding:.16rem .48rem;border-radius:5px;background:rgba(239,68,68,.08);color:var(--red)">Weak opening hook</span>'
                : '')) +
          '</div>'
        : '') +
      (d.interpretation
        ? '<div style="font-size:.8rem;color:#cbd5e1;line-height:1.55;padding:.55rem .75rem;' +
            'background:#0f172a;border-radius:7px;border-left:2px solid ' + tierColor + ';margin-bottom:.75rem">' +
            esc(d.interpretation) + '</div>'
        : '');

    if (d.brain_map_svg) {
      html +=
        '<div class="sh" style="margin-top:.5rem">Brain Activation Map' +
          '<span style="font-size:.62rem;color:var(--muted);font-weight:400;margin-left:.35rem">' +
            '· Source: <span style="color:' + srcColor + '">' + srcLabel + '</span></span>' +
        '</div>' +
        '<div style="margin:.5rem 0 .5rem">' + d.brain_map_svg + '</div>';
    }

    html +=
      '<div style="font-size:.62rem;color:#374151;margin-top:.5rem;padding-top:.5rem;border-top:1px solid #1e1e1e">' +
        'Powered by Meta TRIBE v2 · CC-BY-NC-4.0 (non-commercial research use only) · ' +
        '<a href="/brain-map" target="_blank" style="color:#475569">Brain map explainer</a>' +
      '</div>';
  }

  html += '</div>';
  document.getElementById('nv-result').innerHTML = html;
}

function showErr(id, msg) {
  document.getElementById(id).innerHTML =
    `<div class="err-box">⚠ ${msg}</div>`;
}

/* ── Compare Brands ───────────────────────────────────────────────────────── */
function loadCmpExample(urlA, urlB) {
  document.getElementById('cmp-url-a').value = urlA;
  document.getElementById('cmp-url-b').value = urlB;
  startCompare();
}

function printCmpReport() {
  const iframe = document.getElementById('cmp-report-frame');
  if (iframe && iframe.contentWindow) iframe.contentWindow.print();
}

function buildComparePipeline(labelA, labelB) {
  const agentRows = AGENTS.map(([k, lbl]) =>
    `<div class="cmp-agent-row" id="crow-a-${k}">
       <span class="cmp-ic" id="cic-a-${k}" style="color:#2a2a2a">○</span>
       <span style="font-size:.82rem">${lbl}</span>
       <span class="cmp-st" id="cst-a-${k}">—</span>
     </div>`
  ).join('');
  const agentRowsB = AGENTS.map(([k, lbl]) =>
    `<div class="cmp-agent-row" id="crow-b-${k}">
       <span class="cmp-ic" id="cic-b-${k}" style="color:#2a2a2a">○</span>
       <span style="font-size:.82rem">${lbl}</span>
       <span class="cmp-st" id="cst-b-${k}">—</span>
     </div>`
  ).join('');
  document.getElementById('cmp-dual-cols').innerHTML =
    `<div>
       <div class="cmp-col-hdr a">${labelA || 'Brand A'}</div>
       ${agentRows}
     </div>
     <div>
       <div class="cmp-col-hdr b">${labelB || 'Brand B'}</div>
       ${agentRowsB}
     </div>`;
}

function setCmpAgent(side, key, state, elapsed) {
  const ic  = document.getElementById(`cic-${side}-${key}`);
  const st  = document.getElementById(`cst-${side}-${key}`);
  if (!ic) return;
  const col = side === 'a' ? '#60a5fa' : '#fcd34d';
  if (state === 'running') {
    ic.innerHTML = `<span class="ic-spin" style="color:${col}">↻</span>`;
    if (st) { st.textContent = 'Running…'; st.style.color = col; }
  } else if (state === 'done') {
    ic.innerHTML = `<span style="color:var(--green)">✓</span>`;
    if (st) { st.textContent = elapsed ? elapsed + 's' : 'Done'; st.style.color = 'var(--green)'; }
  } else if (state === 'cached') {
    ic.innerHTML = `<span style="color:${col}">⚡</span>`;
    if (st) { st.textContent = 'Cached'; st.style.color = col; }
    AGENTS.forEach(([k]) => {
      const i2 = document.getElementById(`cic-${side}-${k}`);
      const s2 = document.getElementById(`cst-${side}-${k}`);
      if (i2) i2.innerHTML = `<span style="color:${col}">⚡</span>`;
      if (s2) { s2.textContent = 'Cached'; s2.style.color = col; }
    });
  }
}

async function startCompare() {
  const urlA = document.getElementById('cmp-url-a').value.trim();
  const urlB = document.getElementById('cmp-url-b').value.trim();
  if (!urlA || !urlB) {
    showErr('cmp-result', 'Enter both Brand A and Brand B URLs.');
    return;
  }

  const btn = document.getElementById('cmp-btn');
  btn.disabled = true; btn.textContent = 'Starting…';
  document.getElementById('cmp-result').innerHTML = '';
  document.getElementById('cmp-report-wrap').style.display = 'none';
  document.getElementById('cmp-pipeline').style.display = 'block';
  document.getElementById('cmp-prog').style.width = '0';

  // Derive brand names for pipeline headers
  const nameA = urlA.replace(/^https?:\\/\\/(?:www\\.)?/, '').split('.')[0];
  const nameB = urlB.replace(/^https?:\\/\\/(?:www\\.)?/, '').split('.')[0];
  buildComparePipeline(nameA, nameB);

  let compareId;
  try {
    const r = await fetch('/compare', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url_a: urlA, url_b: urlB}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Failed to start comparison');
    compareId = d.compare_id;
    btn.textContent = 'Comparing…';
  } catch (e) {
    document.getElementById('cmp-pipeline').style.display = 'none';
    showErr('cmp-result', e.message);
    btn.disabled = false; btn.textContent = 'Compare →';
    return;
  }

  const es = new EventSource('/compare/stream/' + compareId);
  let pctA = 0, pctB = 0;

  function updateProg() {
    document.getElementById('cmp-prog').style.width =
      Math.round((pctA + pctB) / 2) + '%';
  }

  es.onmessage = ev => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }

    if (m.side === 'a' || m.side === 'b') {
      const side = m.side;
      if (m.status === 'cache_hit') {
        setCmpAgent(side, null, 'cached');
        if (side === 'a') pctA = 100; else pctB = 100;
        updateProg();
      } else if (m.status === 'running') {
        setCmpAgent(side, m.key, 'running');
      } else if (m.status === 'done') {
        setCmpAgent(side, m.key, 'done', m.elapsed);
        if (side === 'a') pctA = m.progress_pct || pctA;
        else pctB = m.progress_pct || pctB;
        updateProg();
      }
    } else if (m.status === 'complete') {
      pctA = 100; pctB = 100; updateProg();
      es.close();
      document.getElementById('cmp-report-frame').src = m.compare_url;
      document.getElementById('cmp-report-wrap').style.display = 'block';
      document.getElementById('cmp-result').innerHTML = '';
      btn.disabled = false; btn.textContent = 'Compare →';
    } else if (m.status === 'failed') {
      es.close();
      showErr('cmp-result', m.error || 'Comparison failed');
      btn.disabled = false; btn.textContent = 'Compare →';
    } else if (m.status === 'timeout') {
      es.close();
      document.getElementById('cmp-result').innerHTML =
        `<div class="err-box">Comparison is taking longer than expected.
           <a href="/compare/${compareId}" target="_blank">Open when ready</a>.</div>`;
      btn.disabled = false; btn.textContent = 'Compare →';
    }
  };
  es.onerror = () => {
    es.close();
    document.getElementById('cmp-result').innerHTML =
      `<div class="err-box">Connection lost —
         <a href="/compare/${compareId}" target="_blank">check comparison</a>.</div>`;
    btn.disabled = false; btn.textContent = 'Compare →';
  };
}

/* ── My Brands ────────────────────────────────────────────────────────── */
function _fmtIst(isoStr) {
  const utc = new Date(isoStr);
  // Shift to IST (+05:30)
  const ist = new Date(utc.getTime() + (5*60 + 30)*60000);
  const now = new Date(new Date().getTime() + (5*60 + 30)*60000);
  const diffMs = now - ist;
  const diffH = diffMs / 3600000;
  const diffD = diffMs / 86400000;

  const hh = ist.getUTCHours(), mm = String(ist.getUTCMinutes()).padStart(2,'0');
  const ampm = hh >= 12 ? 'PM' : 'AM';
  const h12 = ((hh % 12) || 12);
  const timePart = `${h12}:${mm} ${ampm} IST`;

  if (diffH < 1) return 'just now';
  if (diffH < 24) return `${Math.floor(diffH)}h ago`;
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const day = ist.getUTCDate(), mon = months[ist.getUTCMonth()];
  if (diffD < 2) return `Yesterday, ${timePart}`;
  if (diffD < 365) return `${day} ${mon}, ${timePart}`;
  return `${day} ${mon} ${ist.getUTCFullYear()}, ${timePart}`;
}

let _brandsLoaded = false;

async function loadBrands() {
  _brandsLoaded = true;
  const wrap = document.getElementById('brands-table-wrap');
  wrap.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--muted);font-size:.88rem">Loading…</div>';
  try {
    const data = await fetch('/brands').then(r => r.json());
    const brands = data.brands || [];
    if (!brands.length) {
      wrap.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--muted);font-size:.88rem">No brands audited yet. Run your first audit above.</div>';
      return;
    }
    const rows = brands.map(b => {
      const score = b.overall_score != null ? Math.round(b.overall_score) : '—';
      const scoreColor = b.overall_score == null ? 'var(--muted)'
        : b.overall_score >= 70 ? 'var(--green)'
        : b.overall_score >= 50 ? 'var(--amber)' : 'var(--red)';
      const date = _fmtIst(b.last_audited);
      const monIcon = b.monitoring
        ? '<span style="color:var(--green);font-weight:700" title="Monitoring active">● Active</span>'
        : '<span style="color:var(--muted)" title="Not monitoring">○ Off</span>';
      return `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:.6rem .75rem;font-size:.84rem">
          <a href="${b.url}" target="_blank" rel="noopener" style="color:var(--blue)">${b.url.replace(/^https?:\\/\\//, '')}</a>
        </td>
        <td style="padding:.6rem .75rem;font-size:.88rem;font-weight:700;color:${scoreColor}">${score}</td>
        <td style="padding:.6rem .75rem;font-size:.82rem;color:var(--muted)">${date}</td>
        <td style="padding:.6rem .75rem;font-size:.82rem">${monIcon}</td>
        <td style="padding:.5rem .75rem;white-space:nowrap">
          <div style="display:flex;gap:6px;align-items:center">
            <button onclick="toggleMonitoring(${b.audit_id}, this)"
              style="padding:4px 10px;border-radius:5px;border:1px solid var(--border);
              background:transparent;color:var(--muted);font-size:.72rem;
              cursor:pointer;white-space:nowrap">
              ${b.monitoring ? 'Disable' : 'Monitor'}
            </button>
            <a href="/report/${b.audit_id}" target="_blank"
              style="padding:4px 10px;border-radius:5px;border:1px solid var(--border);
              background:transparent;color:var(--blue);font-size:.72rem;
              text-decoration:none;white-space:nowrap">
              Report
            </a>
          </div>
        </td>
      </tr>`;
    }).join('');
    wrap.innerHTML = `<table style="width:100%;border-collapse:collapse">
      <thead><tr style="background:var(--card2)">
        <th style="padding:.5rem .75rem;text-align:left;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)">Brand</th>
        <th style="padding:.5rem .75rem;text-align:left;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)">Score</th>
        <th style="padding:.5rem .75rem;text-align:left;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)">Last Audited</th>
        <th style="padding:.5rem .75rem;text-align:left;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)">Monitoring</th>
        <th style="padding:.5rem .75rem;text-align:left;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)">Actions</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  } catch(e) {
    wrap.innerHTML = `<div style="text-align:center;padding:2rem;color:var(--red);font-size:.88rem">Error loading brands: ${e.message}</div>`;
  }
}

async function toggleMonitoring(auditId, btn) {
  try {
    const data = await fetch(`/audit/${auditId}/monitoring`, {method:'PATCH'}).then(r => r.json());
    btn.textContent = data.monitoring ? 'Disable' : 'Monitor';
    const row = btn.closest('tr');
    const monCell = row.querySelector('td:nth-child(4)');
    monCell.innerHTML = data.monitoring
      ? '<span style="color:var(--green);font-weight:700" title="Monitoring active">● Active</span>'
      : '<span style="color:var(--muted)" title="Not monitoring">○ Off</span>';
  } catch(e) { alert('Failed to toggle: ' + e.message); }
}

async function addBrandMonitor() {
  const input = document.getElementById('brands-url-input');
  const url = input.value.trim();
  if (!url.startsWith('http')) { alert('Enter a valid URL starting with http'); return; }
  input.value = '';
  // Switch to audit tab and kick off a full streaming audit
  const auditTab = document.querySelector('.tab');
  showTab('audit', auditTab);
  document.getElementById('a-url').value = url;
  startAudit();
}

/* ── System status bar ────────────────────────────────────────────────── */
(async () => {
  try {
    const s = await fetch('/status').then(r => r.json());
    const dot = (v, ok, warn, grey) => {
      const color = v === ok ? 'var(--green)'
                  : (warn && v === warn) ? 'var(--amber)'
                  : (grey && v === grey) ? 'var(--muted)'
                  : 'var(--red)';
      return `<span style="display:inline-flex;align-items:center;gap:.28rem">
        <span style="width:7px;height:7px;border-radius:50%;background:${color};flex-shrink:0"></span>
        <span>${v}</span></span>`;
    };
    document.getElementById('sys-bar').innerHTML = `
      <span style="font-weight:700;text-transform:uppercase;letter-spacing:.07em;margin-right:.2rem">System</span>
      <span style="color:var(--border)">|</span>
      ${dot(s.api, 'ok', null)} API
      <span style="color:var(--border)">·</span>
      ${dot(s.database, 'postgresql', 'sqlite')} DB
      <span style="color:var(--border)">·</span>
      ${dot(s.cache, 'redis', 'in-memory')} Cache
      <span style="color:var(--border)">·</span>
      ${dot(s.mastra, 'connected', null, 'not configured')} Mastra
      <span style="color:var(--border)">·</span>
      ${dot(s.groq, 'ok', null)} Groq
      <span style="color:var(--border)">·</span>
      ${dot(s.playwright, 'ok', null)} Playwright
      <span style="color:var(--border)">·</span>
      ${dot(s.tribe_v2, 'loaded', 'checkpoint needed', 'not installed')} TRIBE v2`;
  } catch (_) {}
})();

/* ── Connectors ────────────────────────────────────────────────────────────── */
function onConnBrandUrl(val) {
  if (!val) return;
  document.getElementById('conn-status-row').style.display = 'none';
}

async function checkConnectorStatus() {
  const url = document.getElementById('conn-brand-url').value.trim();
  if (!url) return;
  const res = await fetch('/connect/status/' + encodeURIComponent(url));
  const data = await res.json();
  _applyConnectorStatus(data);
}

function _applyConnectorStatus(data) {
  // Shopify badge
  const sb = document.getElementById('shopify-badge');
  const sdb = document.getElementById('shopify-disconnect-btn');
  if (data.shopify) {
    sb.textContent = '✓ Connected'; sb.style.display = '';
    sb.style.background = 'rgba(34,197,94,.15)'; sb.style.color = '#22c55e';
    if (data.shopify_store_url) document.getElementById('shopify-store-url').value = data.shopify_store_url;
    sdb.style.display = '';
  } else {
    sb.textContent = 'Not connected'; sb.style.display = '';
    sb.style.background = 'rgba(88,88,88,.18)'; sb.style.color = 'var(--muted)';
    sdb.style.display = 'none';
  }
  // Meta badge
  const mb = document.getElementById('meta-badge');
  const mdb = document.getElementById('meta-disconnect-btn');
  if (data.meta) {
    mb.textContent = '✓ Connected'; mb.style.display = '';
    mb.style.background = 'rgba(34,197,94,.15)'; mb.style.color = '#22c55e';
    if (data.meta_account_id) document.getElementById('meta-account-id').value = data.meta_account_id;
    mdb.style.display = '';
  } else {
    mb.textContent = 'Not connected'; mb.style.display = '';
    mb.style.background = 'rgba(88,88,88,.18)'; mb.style.color = 'var(--muted)';
    mdb.style.display = 'none';
  }
}

async function saveShopify() {
  const brandUrl = document.getElementById('conn-brand-url').value.trim();
  const storeUrl = document.getElementById('shopify-store-url').value.trim();
  const token    = document.getElementById('shopify-token').value.trim();
  const msgEl    = document.getElementById('shopify-msg');
  if (!brandUrl || !storeUrl || !token) {
    _connMsg(msgEl, 'Fill in all fields.', false); return;
  }
  _connMsg(msgEl, 'Verifying…', null);
  try {
    const res = await fetch('/connect/shopify', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({brand_url: brandUrl, store_url: storeUrl, access_token: token})
    });
    const data = await res.json();
    if (res.ok) {
      _connMsg(msgEl, '✓ Shopify connected!', true);
      document.getElementById('shopify-token').value = '';
      document.getElementById('shopify-badge').textContent = '✓ Connected';
      document.getElementById('shopify-badge').style.display = '';
      document.getElementById('shopify-badge').style.background = 'rgba(34,197,94,.15)';
      document.getElementById('shopify-badge').style.color = '#22c55e';
      document.getElementById('shopify-disconnect-btn').style.display = '';
      loadConnectors();
    } else {
      _connMsg(msgEl, '✗ ' + (data.detail || 'Error'), false);
    }
  } catch(e) { _connMsg(msgEl, '✗ Network error', false); }
}

async function saveMeta() {
  const brandUrl   = document.getElementById('conn-brand-url').value.trim();
  const token      = document.getElementById('meta-token').value.trim();
  const accountId  = document.getElementById('meta-account-id').value.trim();
  const msgEl      = document.getElementById('meta-msg');
  if (!brandUrl || !token || !accountId) {
    _connMsg(msgEl, 'Fill in all fields.', false); return;
  }
  _connMsg(msgEl, 'Verifying…', null);
  try {
    const res = await fetch('/connect/meta', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({brand_url: brandUrl, access_token: token, ad_account_id: accountId})
    });
    const data = await res.json();
    if (res.ok) {
      _connMsg(msgEl, '✓ Meta Ads connected!', true);
      document.getElementById('meta-token').value = '';
      document.getElementById('meta-badge').textContent = '✓ Connected';
      document.getElementById('meta-badge').style.display = '';
      document.getElementById('meta-badge').style.background = 'rgba(34,197,94,.15)';
      document.getElementById('meta-badge').style.color = '#22c55e';
      document.getElementById('meta-disconnect-btn').style.display = '';
      loadConnectors();
    } else {
      _connMsg(msgEl, '✗ ' + (data.detail || 'Error'), false);
    }
  } catch(e) { _connMsg(msgEl, '✗ Network error', false); }
}

async function disconnectConnector(provider) {
  const brandUrl = document.getElementById('conn-brand-url').value.trim();
  if (!brandUrl) { alert('Enter brand URL first.'); return; }
  if (!confirm('Disconnect ' + provider + ' for ' + brandUrl + '?')) return;
  const res = await fetch('/connect/' + encodeURIComponent(brandUrl) + '/' + provider, {method:'DELETE'});
  if (res.ok) {
    checkConnectorStatus();
    loadConnectors();
  }
}

async function loadConnectors() {
  const listEl = document.getElementById('connectors-list');
  if (!listEl) return;
  try {
    const res = await fetch('/connect/list');
    const data = await res.json();
    if (!data.length) {
      listEl.innerHTML = '<div style="text-align:center;padding:1.5rem;color:var(--muted);font-size:.85rem">No connectors configured yet. Add API keys above to enable deeper analysis.</div>';
      return;
    }
    listEl.innerHTML = data.map(c => `
      <div style="display:flex;align-items:center;gap:.75rem;padding:.65rem 0;border-bottom:1px solid var(--border)">
        <div style="flex:1;min-width:0">
          <div style="font-size:.88rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${c.brand_url}</div>
          <div style="font-size:.72rem;color:var(--muted);margin-top:.15rem">Updated: ${c.updated_at ? c.updated_at.split('T')[0] : '—'}</div>
        </div>
        <div style="display:flex;gap:.4rem;flex-shrink:0">
          ${c.shopify ? '<span style="font-size:.7rem;font-weight:600;padding:.15rem .5rem;border-radius:4px;background:rgba(34,197,94,.12);color:#22c55e">🛒 Shopify</span>' : ''}
          ${c.meta    ? '<span style="font-size:.7rem;font-weight:600;padding:.15rem .5rem;border-radius:4px;background:rgba(59,130,246,.12);color:#3b82f6">📘 Meta Ads</span>' : ''}
        </div>
        <button onclick="document.getElementById(\'conn-brand-url\').value=\'${c.brand_url}\';checkConnectorStatus();showTab(\'connectors\',document.querySelector(\'.tab:last-child\'))"
          style="font-size:.75rem;padding:.3rem .6rem;border-radius:5px;border:1px solid var(--border);
          background:transparent;color:var(--muted);cursor:pointer">Edit</button>
      </div>`
    ).join('');
  } catch(e) {
    listEl.innerHTML = '<div style="color:var(--red);padding:1rem;font-size:.85rem">Error loading connectors.</div>';
  }
}

function _connMsg(el, msg, ok) {
  el.style.display = '';
  el.textContent = msg;
  el.style.color = ok === true ? 'var(--green)' : ok === false ? 'var(--red)' : 'var(--muted)';
}
</script>

<div id="sys-bar" style="position:fixed;bottom:0;left:0;right:0;
  background:var(--surface);border-top:1px solid var(--border);
  padding:.35rem 1.75rem;display:flex;align-items:center;gap:.65rem;
  font-size:.68rem;color:var(--muted);z-index:100;flex-wrap:wrap">
  <span style="color:var(--border)">Loading status…</span>
</div>

<div style="height:2rem"></div>
</body>
</html>"""


# ── App setup ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    db_label = "postgresql" if db_backend() == "postgresql" else "sqlite"
    cache_label = "redis" if _cache.backend not in ("memory", "in-memory") else "memory"

    groq_ok = bool(_os.getenv("GROQ_API_KEY", "").strip())
    try:
        from playwright.async_api import async_playwright as _apw
        async with _apw() as _pw:
            await _pw.chromium.launch(headless=True)
        pw_ok = True
    except Exception:
        pw_ok = False

    print(f"  ✓ Database  : {db_label}")
    print(f"  ✓ Cache     : {cache_label}")
    print(f"  {'✓' if groq_ok else '✗'} Groq       : {'connected' if groq_ok else 'no GROQ_API_KEY'}")
    print(f"  {'✓' if pw_ok else '✗'} Playwright : {'installed' if pw_ok else 'not found — run: playwright install chromium'}")
    print(f"  Research Agent ready at http://127.0.0.1:8000")
    yield


app = FastAPI(title="Research Agent", version="0.3.0", lifespan=lifespan, docs_url="/api/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception:
    pass


# ── Global exception handlers ─────────────────────────────────────────────────

_HTML_ROUTES = ("/report/", "/share/", "/compare/", "/demo", "/virality/")


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    msgs = "; ".join(
        e.get("msg", "Validation error").replace("Value error, ", "")
        for e in exc.errors()
    )
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "message": msgs},
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    path = request.url.path
    accept = request.headers.get("accept", "")
    is_browser = "text/html" in accept and any(path.startswith(r) for r in _HTML_ROUTES)
    if is_browser and exc.status_code == 404:
        return HTMLResponse(
            _html_error_page(
                "Not Found",
                str(exc.detail),
                help_url="/",
            ),
            status_code=404,
        )
    _error_code = {
        400: "bad_request", 401: "unauthorized", 403: "forbidden",
        404: "not_found", 409: "conflict", 422: "validation_error",
        429: "rate_limited", 500: "server_error", 503: "service_unavailable",
    }.get(exc.status_code, "error")
    return JSONResponse(
        {"error": _error_code, "message": str(exc.detail)},
        status_code=exc.status_code,
    )


# ── Request / Response models ──────────────────────────────────────────────────

class AuditRequest(BaseModel):
    url: str
    scheduled: bool = False
    deep_visual: bool = False

    @field_validator("url")
    @classmethod
    def must_be_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("Invalid URL — must start with http:// or https://")
        return v.rstrip("/")


class ViralityRequest(BaseModel):
    url: Optional[str] = None
    product_name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None

    @model_validator(mode="after")
    def at_least_name_or_description(self) -> "ViralityRequest":
        has_url  = bool(self.url and self.url.strip())
        has_name = bool(self.product_name and self.product_name.strip())
        has_desc = bool(self.description and self.description.strip())
        if not has_url and not has_name and not has_desc:
            raise ValueError(
                "Please provide either a product URL or a product name and description."
            )
        return self


class ActionPlanRequest(BaseModel):
    finding: str
    brand_name: str
    platform: str = "shopify"
    audit_id: Optional[int] = None


class CompareRequest(BaseModel):
    url_a: str
    url_b: str

    @field_validator("url_a", "url_b")
    @classmethod
    def must_be_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("Invalid URL — must start with http:// or https://")
        return v.rstrip("/")


# ── Action plan LLM prompt ────────────────────────────────────────────────────

_ACTION_PLAN_PROMPT_TEMPLATE = (
    "You are a Shopify CRO specialist. Generate a step-by-step implementation plan "
    "for this specific fix for __BRAND_NAME__ on __PLATFORM__.\n\n"
    "Finding: __FINDING__\n\n"
    'Output JSON:\n{\n'
    '  "title": "Fix: [specific name]",\n'
    '  "estimated_time": "2-4 hours",\n'
    '  "estimated_impact": "+5-8% mobile conversion rate",\n'
    '  "difficulty": "Low | Medium | High",\n'
    '  "steps": [\n'
    '    {"step": 1, "action": "specific action", '
    '"detail": "exactly how to do it", '
    '"tool": "Shopify theme editor / liquid code / app name"}\n'
    '  ],\n'
    '  "if_not_on_shopify": "alternative approach for custom platforms",\n'
    '  "resources": ["specific documentation or app links"]\n'
    "}"
)


def _build_action_plan_prompt(brand_name: str, platform: str, finding: str) -> str:
    """Build action plan prompt safely — finding may contain curly braces."""
    return (
        _ACTION_PLAN_PROMPT_TEMPLATE
        .replace("__BRAND_NAME__", brand_name)
        .replace("__PLATFORM__", platform)
        .replace("__FINDING__", finding)
    )


# ── Compare LLM prompt ────────────────────────────────────────────────────────

_COMPARE_FINDINGS_PROMPT = (
    "You are a senior ecommerce strategist doing a deep competitive analysis of two D2C brands.\n"
    "You have comprehensive audit data for both brands — cite specific numbers and findings.\n\n"
    "BRAND A: {brand_a}\n"
    "BRAND B: {brand_b}\n\n"
    "BRAND A AUDIT DATA:\n{context_a}\n\n"
    "BRAND B AUDIT DATA:\n{context_b}\n\n"
    "Output ONLY valid JSON (no markdown fences, no comments):\n"
    '{{\n'
    '  "dimension_verdicts": [\n'
    '    {{"dimension":"Brand Basics","winner":"Brand A or B","loser":"other brand","gap":1.5,'
    '"why_winner_wins":"specific reason citing audit data","loser_fix":"single most impactful fix"}}\n'
    '  ],\n'
    '  "steal_this": [\n'
    '    {{"what":"specific tactic","from_brand":"Brand A or B","why":"why this works — cite data","how":"how the other brand implements this"}}\n'
    '  ],\n'
    '  "customer_journey_battleground": {{\n'
    '    "awareness":{{"winner":"...","evidence":"cite specific data","verdict":"one sentence"}},\n'
    '    "consideration":{{"winner":"...","evidence":"...","verdict":"..."}},\n'
    '    "conversion":{{"winner":"...","evidence":"...","verdict":"..."}},\n'
    '    "retention":{{"winner":"...","evidence":"...","verdict":"..."}}\n'
    '  }},\n'
    '  "head_to_head_verdict":"2-3 sentences: who wins overall and why — cite top 2 differentiating scores",\n'
    '  "the_underdog_opportunity":"1-2 sentences: single highest-leverage move the losing brand can make — be specific",\n'
    '  "shared_blindspot":"1-2 sentences: weakness both brands share that a competitor could exploit"\n'
    '}}\n\n'
    "dimension_verdicts must have exactly 6 items (Brand Basics, Content Quality, Ad Performance, "
    "GEO Visibility, Store CRO, Research Fit). steal_this must have exactly 3 items. "
    "No generic statements — every field must cite actual data."
)


# ── SWOT LLM prompt ───────────────────────────────────────────────────────────

_SWOT_PROMPT = (
    "You are a senior brand strategist. Generate a SWOT analysis for two competing D2C brands "
    "based on comprehensive audit data. Each SWOT item must cite specific data.\n\n"
    "Brand A: {brand_a}\n"
    "Brand B: {brand_b}\n\n"
    "BRAND A AUDIT DATA:\n{context_a}\n\n"
    "BRAND B AUDIT DATA:\n{context_b}\n\n"
    "Output ONLY valid JSON (no markdown fences, no comments):\n"
    '{{\n'
    '  "brand_a_swot":{{\n'
    '    "strengths":[{{"point":"specific strength vs Brand B","evidence":"cite score or finding"}},{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}}],\n'
    '    "weaknesses":[{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}}],\n'
    '    "opportunities":[{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}}],\n'
    '    "threats":[{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}}]\n'
    '  }},\n'
    '  "brand_b_swot":{{\n'
    '    "strengths":[{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}}],\n'
    '    "weaknesses":[{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}}],\n'
    '    "opportunities":[{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}}],\n'
    '    "threats":[{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}},{{"point":"...","evidence":"..."}}]\n'
    '  }},\n'
    '  "overall_winner":"Brand A or Brand B",\n'
    '  "winning_margin":"narrow or moderate or decisive",\n'
    '  "match_summary":"Brand A leads on X. Brand B leads on Y. Overall: [winner] by [margin].",\n'
    '  "head_to_head_verdict":"2-3 sentences synthesizing the matchup — cite top differentiating scores"\n'
    '}}\n\n'
    "Each SWOT array must have exactly 3 items. winning_margin: narrow, moderate, or decisive. "
    "Every point must be specific to the brand matchup — no generic statements.\n\n"
    "Output ONLY the JSON. No preamble. Start your response with { and end with }"
)


# ── Strategy LLM prompt ───────────────────────────────────────────────────────

_STRATEGY_PROMPT = (
    "You are a growth strategist for {brand_name}. Based on comprehensive audit data, "
    "create a targeted 90-day battle plan to outperform {competitor_name}.\n\n"
    "Goal: {goal}\n\n"
    "{brand_name} AUDIT DATA:\n{context_brand}\n\n"
    "{competitor_name} AUDIT DATA:\n{context_competitor}\n\n"
    "Output ONLY valid JSON (no markdown fences, no comments):\n"
    '{{\n'
    '  "situation_in_one_line":"one sentence: current position vs competitor right now — cite scores",\n'
    '  "the_gap_that_matters_most":{{\n'
    '    "dimension":"single highest-leverage dimension to attack or defend",\n'
    '    "current_gap":"e.g. us 6.1 vs them 3.1 — we lead by 3 pts — cite actual numbers",\n'
    '    "why_it_matters":"why closing or widening this gap has the biggest revenue impact"\n'
    '  }},\n'
    '  "30_day_quick_wins":[\n'
    '    {{"action":"specific action","closes_gap_in":"dimension name","effort":"Low","expected_impact":"measurable outcome","why_this_works":"cite audit data"}},\n'
    '    {{"action":"...","closes_gap_in":"...","effort":"Low","expected_impact":"...","why_this_works":"..."}},\n'
    '    {{"action":"...","closes_gap_in":"...","effort":"Low","expected_impact":"...","why_this_works":"..."}}\n'
    '  ],\n'
    '  "60_day_plays":[\n'
    '    {{"action":"...","closes_gap_in":"...","effort":"Medium","expected_impact":"...","why_this_works":"..."}},\n'
    '    {{"action":"...","closes_gap_in":"...","effort":"Medium","expected_impact":"...","why_this_works":"..."}},\n'
    '    {{"action":"...","closes_gap_in":"...","effort":"Medium","expected_impact":"...","why_this_works":"..."}}\n'
    '  ],\n'
    '  "90_day_moat":[\n'
    '    {{"action":"...","closes_gap_in":"...","effort":"High","expected_impact":"...","why_this_works":"..."}},\n'
    '    {{"action":"...","closes_gap_in":"...","effort":"High","expected_impact":"...","why_this_works":"..."}},\n'
    '    {{"action":"...","closes_gap_in":"...","effort":"High","expected_impact":"...","why_this_works":"..."}}\n'
    '  ],\n'
    '  "dont_waste_time_on":["specific thing to avoid — cite audit data","another waste to avoid"],\n'
    '  "if_competitor_does_this_respond_with":{{\n'
    '    "trigger":"specific action the competitor might take based on their audit data",\n'
    '    "response":"exactly how to counter it — cite our strengths"\n'
    '  }}\n'
    '}}\n\n'
    "30_day_quick_wins, 60_day_plays, 90_day_moat: exactly 3 items each. "
    "dont_waste_time_on: exactly 2 items. effort: Low, Medium, or High. "
    "Every item must cite actual scores or findings from the audit data."
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_json(val):
    if val is None:
        return None
    try:
        return json.loads(val)
    except Exception:
        return val


def _build_rich_context(brand_name: str, results: dict) -> dict:
    """Extract all meaningful content from 6 agent results into a flat dict for LLM consumption."""
    ctx: dict = {"brand_name": brand_name}

    # brand_basics
    bb = results.get("brand_basics") or {}
    if isinstance(bb, str):
        bb = _parse_json(bb) or {}
    bb_a = bb.get("analysis") or {}
    ctx["founding_year"] = bb_a.get("founding_year")
    ctx["target_audience"] = bb_a.get("target_audience")
    ctx["brand_positioning"] = bb_a.get("brand_positioning")
    ctx["tone_of_voice"] = bb_a.get("tone_of_voice")
    ctx["key_strengths"] = (bb_a.get("key_strengths") or [])[:5]
    ctx["key_weaknesses"] = (bb_a.get("key_weaknesses") or [])[:3]
    ctx["social_channels"] = list((bb_a.get("social_channels") or {}).keys())
    ctx["price_range"] = bb_a.get("price_range")
    ctx["usp"] = bb_a.get("usp") or bb_a.get("unique_selling_proposition")
    ctx["core_categories"] = bb_a.get("core_categories") or bb_a.get("product_categories")

    # content_catalog
    cc = results.get("content_catalog") or {}
    if isinstance(cc, str):
        cc = _parse_json(cc) or {}
    cc_a = cc.get("analysis") or {}
    ctx["pdp_quality_score"] = cc_a.get("pdp_quality_score")
    ctx["pdp_strengths"] = (cc_a.get("pdp_strengths") or [])[:3]
    ctx["pdp_weaknesses"] = (cc_a.get("pdp_weaknesses") or [])[:3]
    ctx["benefit_vs_feature_ratio"] = cc_a.get("benefit_vs_feature_ratio")
    rewrites = cc_a.get("pdp_rewrites") or {}
    ctx["rewritten_headline"] = rewrites.get("headline") if isinstance(rewrites, dict) else None
    ctx["content_gaps"] = (cc_a.get("content_gaps") or [])[:3]
    ctx["homepage_score"] = cc_a.get("homepage_score")

    # performance_ads
    pa = results.get("performance_ads") or {}
    if isinstance(pa, str):
        pa = _parse_json(pa) or {}
    pa_a = pa.get("analysis") or {}
    ads_scrape = pa.get("ads_scrape") or {}
    ctx["active_ads_count"] = ads_scrape.get("ads_count")
    ctx["creative_format_breakdown"] = ads_scrape.get("ad_formats")
    ctx["hook_strength_score"] = pa_a.get("hook_strength_score")
    ctx["funnel_coverage"] = pa_a.get("funnel_coverage") or pa_a.get("funnel_stages_covered")
    ctx["retargeting_signals"] = pa_a.get("retargeting_signals") or pa_a.get("has_retargeting")
    ctx["top_ad_hooks"] = (pa_a.get("top_performing_hooks") or ads_scrape.get("sample_headlines") or [])[:3]
    ctx["ad_spend_signals"] = pa_a.get("ad_spend_signals") or pa_a.get("spend_signals")
    ctx["landing_page_match"] = pa_a.get("landing_page_match_score")
    ctx["estimated_active_ads"] = pa_a.get("estimated_active_ads")

    # geo_visibility
    gv = results.get("geo_visibility") or {}
    if isinstance(gv, str):
        gv = _parse_json(gv) or {}
    gv_a = gv.get("analysis") or {}
    ctx["geo_score"] = gv_a.get("geo_score")
    ctx["ai_citation_likelihood"] = gv_a.get("ai_citation_likelihood") or gv_a.get("ai_citation_score")
    ctx["chatgpt_mentioned"] = gv_a.get("chatgpt_mentioned")
    ctx["schema_types_found"] = gv_a.get("schema_types") or gv_a.get("schema_types_found")
    ctx["schema_missing"] = (gv_a.get("schema_missing") or gv_a.get("missing_schema") or [])[:3]
    ctx["wikipedia_present"] = gv_a.get("wikipedia_present")
    ctx["top_geo_fixes"] = (gv_a.get("quick_wins") or gv_a.get("top_recommendations") or gv_a.get("recommendations") or [])[:3]
    ctx["geo_weaknesses"] = (gv_a.get("weaknesses") or gv_a.get("gaps") or [])[:3]

    # store_cro
    sc = results.get("store_cro") or {}
    if isinstance(sc, str):
        sc = _parse_json(sc) or {}
    sc_a = sc.get("analysis") or {}
    ps = sc.get("pagespeed") or {}
    cro_sigs = sc.get("cro_signals") or {}
    ctx["mobile_pagespeed"] = ps.get("mobile_score")
    ctx["desktop_pagespeed"] = ps.get("desktop_score")
    ctx["lcp"] = ps.get("lcp")
    ctx["funnel_friction_points"] = (sc_a.get("funnel_friction") or sc_a.get("checkout_friction") or sc_a.get("friction_points") or [])[:3]
    ctx["top_cro_fixes"] = (sc_a.get("quick_wins") or sc_a.get("top_cro_fixes") or sc_a.get("recommendations") or [])[:3]
    ctx["trust_signals"] = cro_sigs.get("trust_signals") or sc_a.get("trust_signals_found")
    ctx["platform"] = sc.get("platform_detected")
    ctx["payment_options"] = cro_sigs.get("payment_options")
    ctx["email_capture"] = cro_sigs.get("email_capture")
    ctx["cro_score"] = sc_a.get("cro_score")

    # research
    rs = results.get("research") or {}
    if isinstance(rs, str):
        rs = _parse_json(rs) or {}
    rs_a = rs.get("analysis") or {}
    ctx["top_competitors"] = (rs_a.get("top_competitors") or [])[:3]
    ctx["where_brand_wins"] = (rs_a.get("where_brand_wins") or [])[:3]
    ctx["where_brand_loses"] = (rs_a.get("where_brand_loses") or [])[:3]
    ctx["whitespace_opportunities"] = (rs_a.get("whitespace_opportunities") or rs_a.get("market_gaps") or [])[:3]
    ctx["market_position"] = rs_a.get("market_position") or rs_a.get("positioning_summary")
    ctx["category"] = rs.get("category_inferred") or rs_a.get("category")
    ctx["brand_reputation_signals"] = rs_a.get("brand_reputation_signals") or rs_a.get("reputation_summary")

    return {k: v for k, v in ctx.items() if v is not None and v != [] and v != {}}


def _assemble_audit_data(audit: AuditRun) -> dict:
    """Reconstruct the audit dict that generate_audit_report expects."""
    from agents.agentic_orchestrator import _brand_name_from_url as _bnfu
    am = _parse_json(audit.agentic_meta_json) or {}
    return {
        "audit_id":           audit.id,
        "url":                audit.url,
        "brand_name":         _bnfu(audit.url),
        "one_thing":          audit.one_thing or "",
        "roadmap_json":       audit.roadmap_json or "",
        "changes_summary":    audit.changes_summary or "",
        "analyst_brief":      _parse_json(audit.analyst_brief_json) or {},
        "cross_findings":     _parse_json(audit.cross_findings_json) or [],
        # WorkingMemory — powers Reasoning Brain panel
        "agentic_meta":       am,
        "reasoning_trace":    am.get("reasoning_trace", []),
        "signals":            am.get("signals", []),
        "cross_insights":     am.get("cross_insights", []),
        "decisions":          am.get("decisions", []),
        "pattern_detected":   am.get("pattern_detected"),
        "strategic_posture":  am.get("strategic_posture"),
        "results": {
            "brand_basics":       _parse_json(audit.brand_basics),
            "content_catalog":    _parse_json(audit.content_catalog),
            "performance_ads":    _parse_json(audit.performance_ads),
            "geo_visibility":     _parse_json(audit.geo_visibility),
            "store_cro":          _parse_json(audit.store_cro),
            "research":           _parse_json(audit.research),
            "social_profile":     _parse_json(audit.social_profile),
            "social_media_audit": _parse_json(audit.social_media_audit),
        },
    }


def _inject_toolbar(html: str, share_url: str, created_at_str: str) -> str:
    """Inject a sticky top toolbar (Share · Print · Re-run) into a report HTML string."""
    share_url_js = json.dumps(share_url)
    toolbar = (
        '<div id="rpt-toolbar" style="position:sticky;top:0;z-index:200;'
        'background:rgba(10,10,10,.95);backdrop-filter:blur(8px);'
        '-webkit-backdrop-filter:blur(8px);border-bottom:1px solid #1e1e1e;'
        'padding:.45rem 1.5rem;display:flex;align-items:center;'
        'justify-content:flex-end;gap:.4rem;flex-wrap:wrap">'

        '<button id="rpt-share-btn" onclick="copyShareLink()" '
        'style="display:inline-flex;align-items:center;gap:.3rem;padding:.3rem .78rem;'
        'border-radius:6px;border:1px solid #2a2a2a;background:#181818;'
        'color:#e8e8e8;font-size:.77rem;font-weight:600;cursor:pointer;'
        'transition:color .15s,border-color .15s">'
        '&#128203; Copy Share Link</button>'

        '<button onclick="window.print()" '
        'style="display:inline-flex;align-items:center;gap:.3rem;padding:.3rem .78rem;'
        'border-radius:6px;border:1px solid #2a2a2a;background:#181818;'
        'color:#e8e8e8;font-size:.77rem;font-weight:600;cursor:pointer">'
        '&#128424;&#65039; Print / Save PDF</button>'

        '<button onclick="(window.top||window).location.href=\'/\'" '
        'style="display:inline-flex;align-items:center;gap:.3rem;padding:.3rem .78rem;'
        'border-radius:6px;border:1px solid #2a2a2a;background:#181818;'
        'color:#e8e8e8;font-size:.77rem;font-weight:600;cursor:pointer">'
        '&#8634; Re-run Audit</button>'

        '<div style="margin-left:.35rem;font-size:.67rem;color:#404040;'
        'display:flex;flex-direction:column;align-items:flex-end;line-height:1.35">'
        '<span>Link expires never</span>'
        f'<span>Data from {created_at_str}</span>'
        '</div>'
        '</div>'

        '<script>'
        'function copyShareLink(){'
        f'var u={share_url_js};'
        'var b=document.getElementById("rpt-share-btn");'
        'var o=b.innerHTML;'
        'if(navigator.clipboard){'
        'navigator.clipboard.writeText(u).then(function(){'
        'b.innerHTML="&#10003; Link copied!";'
        'b.style.color="#22c55e";b.style.borderColor="rgba(34,197,94,.4)";'
        'setTimeout(function(){b.innerHTML=o;b.style.color="";b.style.borderColor="";},2000);'
        '}).catch(function(){prompt("Copy:",u);});'
        '}else{prompt("Copy:",u);}'
        '}'
        '</script>'
    )
    return html.replace("<body>", "<body>\n" + toolbar, 1)


# ── Score history + change detection helpers ─────────────────────────────────

_CHANGES_PROMPT = """\
You are a brand intelligence analyst. Compare two audit snapshots and produce a concise change summary.

Previous audit data (JSON):
{prev_summary}

Current audit data (JSON):
{curr_summary}

Days between audits: {days_between}

Output ONLY valid JSON:
{{
  "summary": "<1-2 sentence plain-English summary of the most important changes>",
  "improvements": ["<item>", "<item>"],
  "regressions": ["<item>", "<item>"],
  "days_ago": {days_between}
}}

Rules:
- improvements: up to 3 specific positive changes (e.g. "GEO score rose from 32 to 48")
- regressions: up to 3 specific negative changes
- If no meaningful change, set summary to "" and both lists to []
- Never invent data not present in the snapshots
"""


def _score_snapshot(audit: AuditRun) -> dict:
    """Minimal score snapshot for change-detection prompt."""
    results = {
        "brand_basics":    _parse_json(audit.brand_basics)    or {},
        "content_catalog": _parse_json(audit.content_catalog) or {},
        "performance_ads": _parse_json(audit.performance_ads) or {},
        "geo_visibility":  _parse_json(audit.geo_visibility)  or {},
        "store_cro":       _parse_json(audit.store_cro)       or {},
        "research":        _parse_json(audit.research)        or {},
    }
    scores = extract_native_scores(results)
    overall = _overall_health(results)
    return {**scores, "overall_score": overall}


async def _generate_audit_changes(llm, prev_audit: AuditRun, curr_audit: AuditRun) -> dict:
    """Ask LLM to describe meaningful changes between two audit snapshots."""
    from datetime import datetime as _dt
    prev_snap = _score_snapshot(prev_audit)
    curr_snap = _score_snapshot(curr_audit)
    try:
        days = max(0, (curr_audit.created_at - prev_audit.created_at).days)
    except Exception:
        days = 0

    prompt = _CHANGES_PROMPT.format(
        prev_summary=json.dumps(prev_snap),
        curr_summary=json.dumps(curr_snap),
        days_between=days,
    )
    try:
        result = await llm.analyze_structured(
            system_prompt=prompt,
            user_content="Generate the change summary JSON.",
        )
        if isinstance(result, dict) and "summary" in result:
            result.setdefault("days_ago", days)
            return result
    except Exception:
        pass
    return {}


async def _maybe_generate_changes(audit_id: int, audit: AuditRun) -> None:
    """Look up the previous audit for this URL and write changes_summary if found."""
    from sqlmodel import Session as S, select as _select
    url = audit.url

    with S(engine) as s:
        prev = s.exec(
            _select(AuditRun)
            .where(AuditRun.url == url)
            .where(AuditRun.id != audit_id)
            .where(AuditRun.status == "complete")
            .order_by(AuditRun.created_at.desc())
            .limit(1)
        ).first()

    if not prev:
        return

    llm = get_client()
    changes = await _generate_audit_changes(llm, prev, audit)
    if not changes:
        return

    with S(engine) as s:
        a = s.get(AuditRun, audit_id)
        if a:
            a.changes_summary = json.dumps(changes)
            s.add(a)
            s.commit()


def _write_score_history(audit_id: int, results: dict, url: str) -> None:
    """Write one ScoreHistory row for this completed audit."""
    from sqlmodel import Session as S, select as _select
    scores = extract_native_scores(results)
    overall = _overall_health(results)

    with S(engine) as s:
        row = ScoreHistory(
            brand_url=url,
            audit_id=audit_id,
            brand_basics_score=scores.get("brand_basics_score"),
            content_score=scores.get("content_score"),
            ads_score=scores.get("ads_score"),
            geo_score=scores.get("geo_score"),
            store_score=scores.get("store_score"),
            research_score=scores.get("research_score"),
            overall_score=float(overall) if overall else None,
        )
        s.add(row)
        s.commit()


# ── Compare helpers ───────────────────────────────────────────────────────────

async def _orchestrate_if_needed(audit_id: int) -> None:
    """Run the orchestrator only when the audit is not already complete."""
    from sqlmodel import Session as S
    with S(engine) as s:
        audit = s.get(AuditRun, audit_id)
        if audit and audit.status == "complete":
            return
    await _orchestrate(audit_id)


async def _generate_compare_findings(llm, audit_a: dict, audit_b: dict) -> dict:
    """Call LLM to generate rich competitive comparison findings."""
    brand_a = audit_a.get("brand_name", "Brand A")
    brand_b = audit_b.get("brand_name", "Brand B")
    results_a = audit_a.get("results") or audit_a
    results_b = audit_b.get("results") or audit_b

    ctx_a = _build_rich_context(brand_a, results_a)
    ctx_b = _build_rich_context(brand_b, results_b)

    ctx_a_json = json.dumps(ctx_a, indent=2)
    ctx_b_json = json.dumps(ctx_b, indent=2)
    if len(ctx_a_json) < 200:
        print(f"  [compare_findings] Warning: context for {brand_a} is thin ({len(ctx_a_json)} chars) — audit may be empty or failed", flush=True)
    if len(ctx_b_json) < 200:
        print(f"  [compare_findings] Warning: context for {brand_b} is thin ({len(ctx_b_json)} chars) — audit may be empty or failed", flush=True)

    prompt = _COMPARE_FINDINGS_PROMPT.format(
        brand_a=brand_a,
        brand_b=brand_b,
        context_a=ctx_a_json,
        context_b=ctx_b_json,
    )

    try:
        result = await llm.analyze_structured(
            system_prompt=prompt,
            user_content="Generate the competitive comparison now.",
            max_tokens=2500,
        )
        if isinstance(result, dict) and not result.get("_parse_error"):
            return result
    except Exception as exc:
        print(f"  [compare_findings] skipped — {exc}", flush=True)

    return {
        "dimension_verdicts": [],
        "steal_this": [],
        "customer_journey_battleground": {},
        "head_to_head_verdict": "",
        "the_underdog_opportunity": "",
        "shared_blindspot": "",
    }


def _make_audit_run_from_cache(url: str, cached_data: dict, session) -> AuditRun:
    """Create a completed AuditRun from cache data (same logic as start_audit cache path)."""
    results = cached_data.get("results") or {}
    audit = AuditRun(
        url=url,
        status="complete",
        progress_pct=100,
        current_agent="__cached__",
        share_token=secrets.token_urlsafe(8),
        brand_basics=json.dumps(results.get("brand_basics")),
        content_catalog=json.dumps(results.get("content_catalog")),
        performance_ads=json.dumps(results.get("performance_ads")),
        geo_visibility=json.dumps(results.get("geo_visibility")),
        store_cro=json.dumps(results.get("store_cro")),
        research=json.dumps(results.get("research")),
        social_profile=json.dumps(results.get("social_profile")),
        social_media_audit=json.dumps(results.get("social_media_audit")),
        one_thing=cached_data.get("one_thing", ""),
        roadmap_json=(
            json.dumps(cached_data["roadmap"])
            if cached_data.get("roadmap") else None
        ),
        analyst_brief_json=(
            json.dumps(cached_data["analyst_brief"])
            if cached_data.get("analyst_brief") else None
        ),
        cross_findings_json=(
            json.dumps(cached_data["cross_findings"])
            if cached_data.get("cross_findings") else None
        ),
    )
    session.add(audit)
    session.commit()
    session.refresh(audit)
    return audit


async def _run_compare_bg(
    compare_id: int,
    audit_id_a: int,
    audit_id_b: int,
    url_a: str,
    url_b: str,
) -> None:
    """Background: run both audits in parallel, then generate compare report."""
    from sqlmodel import Session as S

    with S(engine) as s:
        cr = s.get(CompareRun, compare_id)
        if cr:
            cr.status = "running"
            s.add(cr)
            s.commit()

    try:
        # Pre-check: skip audits that are already complete (reuse cached results)
        with S(engine) as s:
            a_obj = s.get(AuditRun, audit_id_a)
            b_obj = s.get(AuditRun, audit_id_b)
            a_complete = a_obj is not None and a_obj.status == "complete"
            b_complete = b_obj is not None and b_obj.status == "complete"

        print(
            f"[compare {compare_id}] "
            f"Brand A #{audit_id_a}: {'complete — reusing' if a_complete else 'needs audit'}  "
            f"Brand B #{audit_id_b}: {'complete — reusing' if b_complete else 'needs audit'}",
            flush=True,
        )

        # Sequential when both need running (parallel exhausts Groq free-tier TPM)
        if not a_complete:
            await _orchestrate(audit_id_a)
        if not b_complete:
            await _orchestrate(audit_id_b)
    except Exception as exc:
        with S(engine) as s:
            cr = s.get(CompareRun, compare_id)
            if cr:
                cr.status = "failed"
                cr.error = str(exc)
                s.add(cr)
                s.commit()
        return

    # Fetch completed audit data (audits may be "failed" if their site was unreachable)
    with S(engine) as s:
        audit_a = s.get(AuditRun, audit_id_a)
        audit_b = s.get(AuditRun, audit_id_b)
        cr = s.get(CompareRun, compare_id)

    if not audit_a or not audit_b or not cr:
        return

    brand_a_failed = audit_a.status == "failed"
    brand_b_failed = audit_b.status == "failed"

    audit_data_a = _assemble_audit_data(audit_a)
    audit_data_b = _assemble_audit_data(audit_b)

    # LLM findings
    llm = get_client()
    findings = await _generate_compare_findings(llm, audit_data_a, audit_data_b)

    # Generate HTML report
    try:
        html = generate_compare_report(
            audit_data_a,
            audit_data_b,
            findings,
            url_a,
            url_b,
            cache_hit_a=cr.cache_hit_a,
            cache_hit_b=cr.cache_hit_b,
            compare_id=compare_id,
            audit_id_a=cr.audit_id_a,
            audit_id_b=cr.audit_id_b,
            share_token=cr.compare_share_token,
            brand_a_failed=brand_a_failed,
            brand_b_failed=brand_b_failed,
        )
    except Exception as exc:
        with S(engine) as s:
            cr2 = s.get(CompareRun, compare_id)
            if cr2:
                cr2.status = "failed"
                cr2.error = f"Report generation failed: {exc}"
                s.add(cr2)
                s.commit()
        return

    with S(engine) as s:
        cr2 = s.get(CompareRun, compare_id)
        if cr2:
            cr2.status = "complete"
            cr2.compare_html = html
            cr2.findings_json = json.dumps(findings)
            s.add(cr2)
            s.commit()

    print(f"  Compare #{compare_id} complete.", flush=True)


# ── Compare SSE generator ──────────────────────────────────────────────────────

async def _compare_sse_gen(
    compare_id: int,
    audit_id_a: int,
    audit_id_b: int,
) -> AsyncGenerator[str, None]:
    from sqlmodel import Session as S

    # Fast path — both audits cached, compare already done
    with S(engine) as session:
        cr = session.get(CompareRun, compare_id)
        if cr and cr.status == "complete":
            yield f"data: {json.dumps({'status': 'complete', 'compare_url': f'/compare/{compare_id}'})}\n\n"
            return

    # Sentinel: "__INIT__" means we haven't seen the first DB value yet
    last_a: Optional[str] = "__INIT__"
    last_b: Optional[str] = "__INIT__"
    agent_t: dict[str, float] = {}
    deadline = time.monotonic() + 720  # 12 min
    ka_at = time.monotonic()

    while time.monotonic() < deadline:
        await asyncio.sleep(0.6)

        if time.monotonic() - ka_at > 20:
            yield ": ka\n\n"
            ka_at = time.monotonic()

        with S(engine) as session:
            audit_a = session.get(AuditRun, audit_id_a)
            audit_b = session.get(AuditRun, audit_id_b)
            cr = session.get(CompareRun, compare_id)

        if not cr:
            yield f"data: {json.dumps({'status': 'error', 'error': 'Not found'})}\n\n"
            return

        def _emit_side(cur, last, side, audit) -> tuple[str, list[str]]:
            """Return (new_last, [sse_lines])."""
            events = []
            if cur == last:
                return last, events
            # Emit "done" for the agent that just finished
            if last not in ("__INIT__", "__cached__") and last is not None:
                elapsed = round(time.monotonic() - agent_t.get(f"{side}:{last}", time.monotonic()), 1)
                pct = audit.progress_pct if audit else 0
                events.append(json.dumps({
                    "side": side, "key": last, "status": "done",
                    "elapsed": elapsed, "progress_pct": pct,
                }))
            # Emit event for new current_agent
            if cur == "__cached__":
                events.append(json.dumps({"side": side, "status": "cache_hit", "progress_pct": 100}))
            elif cur is not None:
                agent_t[f"{side}:{cur}"] = time.monotonic()
                events.append(json.dumps({"side": side, "key": cur, "status": "running"}))
            return cur, events

        new_last_a, evts_a = _emit_side(audit_a.current_agent if audit_a else None, last_a, "a", audit_a)
        new_last_b, evts_b = _emit_side(audit_b.current_agent if audit_b else None, last_b, "b", audit_b)
        last_a = new_last_a
        last_b = new_last_b

        for evt in evts_a + evts_b:
            yield f"data: {evt}\n\n"

        if cr.status == "complete":
            yield f"data: {json.dumps({'status': 'complete', 'compare_url': f'/compare/{compare_id}'})}\n\n"
            return
        if cr.status == "failed":
            yield f"data: {json.dumps({'status': 'failed', 'error': cr.error or 'Failed'})}\n\n"
            return

    yield f"data: {json.dumps({'status': 'timeout'})}\n\n"


# ── Background task ───────────────────────────────────────────────────────────

async def _run_tribe_background(audit_id: int) -> None:
    """Run TRIBE v2 brain analysis as a background task after the main audit.

    Reads the posts snapshot saved by social_media_audit, runs _process_reels_tribe,
    merges the brain-map results back into the social_media_audit JSON in the DB,
    then regenerates the HTML report so the Reels Neural section shows real fMRI data.
    """
    from sqlmodel import Session as S
    from agents.social_media_audit import _process_reels_tribe

    def _set_tribe_status(status: str, error: str | None = None) -> None:
        with S(engine) as sess:
            a = sess.get(AuditRun, audit_id)
            if a and a.social_media_audit:
                blob = _parse_json(a.social_media_audit) or {}
                blob["tribe_status"] = status
                if error:
                    blob["tribe_error"] = error
                a.social_media_audit = json.dumps(blob)
                sess.add(a)
                sess.commit()

    try:
        with S(engine) as sess:
            audit = sess.get(AuditRun, audit_id)
            if not audit or not audit.social_media_audit:
                return
            sma = _parse_json(audit.social_media_audit) or {}

        posts = sma.get("tribe_posts_snapshot", [])
        if not posts:
            _set_tribe_status("failed", "No Reels snapshot found")
            return

        print(f"  [tribe_bg] Starting TRIBE v2 for audit {audit_id} — {len(posts)} Reel(s)", flush=True)
        _set_tribe_status("processing")

        tribe_data = await _process_reels_tribe(posts)

        with S(engine) as sess:
            audit = sess.get(AuditRun, audit_id)
            if not audit or not audit.social_media_audit:
                return
            blob = _parse_json(audit.social_media_audit) or {}
            blob["reels_tribe"]        = tribe_data["reels_tribe"]
            blob["brand_brain_map"]    = tribe_data["brand_brain_map"]
            blob["tribe_available"]    = tribe_data["tribe_available"]
            blob["tribe_status"]       = "complete"
            blob["tribe_error"]        = tribe_data.get("tribe_error")
            blob.pop("tribe_posts_snapshot", None)  # no longer needed
            audit.social_media_audit = json.dumps(blob)
            sess.add(audit)
            sess.commit()

        # Regenerate the HTML report with the new brain-map data
        try:
            from reports.generator import generate_audit_report
            with S(engine) as sess:
                audit = sess.get(AuditRun, audit_id)
                if audit:
                    report_html = generate_audit_report(_assemble_audit_data(audit))
                    audit.report_html = report_html
                    sess.add(audit)
                    sess.commit()
        except Exception as rep_exc:
            print(f"  [tribe_bg] Report regen failed: {rep_exc}", flush=True)

        reels_ok = len([r for r in tribe_data["reels_tribe"] if not r.get("error")])
        print(f"  [tribe_bg] Done — {reels_ok} Reel(s) processed for audit {audit_id}", flush=True)

    except Exception as exc:
        print(f"  [tribe_bg] Failed for audit {audit_id}: {exc}", flush=True)
        _set_tribe_status("failed", str(exc))


async def _run_audit_bg(audit_id: int, deep_visual: bool = False) -> None:
    from sqlmodel import Session as S

    try:
        # Main audit always runs without blocking on TRIBE v2.
        # deep_visual flag is forwarded so social_media_audit saves the posts
        # snapshot and sets tribe_status="pending"; TRIBE v2 runs below.
        await _orchestrate(audit_id, deep_visual=deep_visual)
    except Exception as exc:
        with S(engine) as session:
            audit = session.get(AuditRun, audit_id)
            if audit:
                audit.status = "failed"
                audit.error = str(exc)
                session.add(audit)
                session.commit()
        return

    # Post-completion: write score history, detect changes, render report
    with S(engine) as session:
        audit = session.get(AuditRun, audit_id)
        if not audit or audit.status != "complete":
            return
        audit_url = audit.url
        results = {
            "brand_basics":    _parse_json(audit.brand_basics)    or {},
            "content_catalog": _parse_json(audit.content_catalog) or {},
            "performance_ads": _parse_json(audit.performance_ads) or {},
            "geo_visibility":  _parse_json(audit.geo_visibility)  or {},
            "store_cro":       _parse_json(audit.store_cro)       or {},
            "research":        _parse_json(audit.research)        or {},
        }

    # Write score history row (sync, short session)
    try:
        _write_score_history(audit_id, results, audit_url)
    except Exception:
        pass

    # Generate LLM change summary vs previous audit (async, no open session)
    try:
        with S(engine) as s:
            fresh_audit = s.get(AuditRun, audit_id)
        if fresh_audit:
            await _maybe_generate_changes(audit_id, fresh_audit)
    except Exception:
        pass

    # Render and cache HTML
    with S(engine) as session:
        audit = session.get(AuditRun, audit_id)
        if audit and audit.status == "complete":
            try:
                audit_data = _assemble_audit_data(audit)
                audit.report_html = generate_audit_report(audit_data)
                session.add(audit)
                session.commit()
                cache_key = _cache.audit_key(audit.url)
                await _cache.set(cache_key, audit_data, TTL["audit"])
            except Exception:
                pass  # best-effort; report endpoint generates on-demand as fallback

    # If deep_visual was requested, spin up TRIBE v2 in the background now
    # that the main audit HTML is already saved and the user can see the report.
    if deep_visual:
        asyncio.create_task(_run_tribe_background(audit_id))


# ── SSE generator ─────────────────────────────────────────────────────────────

_PHASE3_KEYS = ["content_catalog", "performance_ads", "geo_visibility", "store_cro"]


def _extract_preview(agent_key: str, result: dict) -> dict:
    """Extract a one-line preview from an agent result for the live findings feed."""
    analysis = result.get("analysis") or {}

    def _first(lst, fallback=""):
        item = (lst or [None])[0]
        if isinstance(item, dict):
            return item.get("fix") or item.get("action") or fallback
        return str(item) if item else fallback

    previews = {
        "brand_basics": {
            "headline": f"{analysis.get('brand_name', 'Brand')} — {', '.join((analysis.get('core_categories') or [''])[:1])}",
            "stat": f"Founded {analysis.get('founding_year', '?')} · {analysis.get('tone_of_voice', '?')} tone",
            "insight": analysis.get("brand_positioning") or "Positioning detected",
        },
        "content_catalog": {
            "headline": f"PDP Quality: {analysis.get('pdp_quality_score', '?')}/10",
            "stat": f"Homepage: {analysis.get('homepage_score', '?')}/10",
            "insight": _first(analysis.get("pdp_weaknesses"), "Content analysed"),
        },
        "performance_ads": {
            "headline": f"{analysis.get('estimated_active_ads', '?')} active ads detected",
            "stat": f"Hook strength: {analysis.get('hook_strength_score', '?')}/10",
            "insight": _first(analysis.get("top_3_ad_quick_wins"), "Ad strategy analysed"),
        },
        "geo_visibility": {
            "headline": f"GEO Score: {analysis.get('geo_score', '?')}/100",
            "stat": f"AI citation likelihood: {analysis.get('ai_citation_likelihood', '?')}",
            "insight": _first(analysis.get("top_5_content_topics_for_ai_citation"), "GEO analysed"),
        },
        "store_cro": {
            "headline": f"Mobile Speed: {(result.get('pagespeed') or {}).get('mobile_score', '?')}/100",
            "stat": f"Desktop: {(result.get('pagespeed') or {}).get('desktop_score', '?')}/100",
            "insight": _first(analysis.get("top_5_cro_fixes"), "Store analysed"),
        },
        "research": {
            "headline": f"{len(analysis.get('top_competitors') or [])} competitors identified",
            "stat": "Market position mapped",
            "insight": _first(analysis.get("whitespace_opportunities"), "Research complete"),
        },
        "social_profile": {
            "headline": f"Social presence score: {result.get('social_presence_score', '?')}/10",
            "stat": f"Instagram: {(result.get('instagram') or {}).get('followers', 0):,} followers",
            "insight": _first(result.get("top_3_social_improvements"), "Social presence mapped"),
        },
        "social_media_audit": {
            "headline": f"Social audit score: {(result.get('scores') or {}).get('overall', '?')}/10",
            "stat": f"IG: {(result.get('platforms') or {}).get('instagram', {}).get('followers', 0):,} · YT: {(result.get('platforms') or {}).get('youtube', {}).get('subscribers', 0):,}",
            "insight": _first(result.get("top_3_recommendations"), "Multi-platform audit complete"),
        },
    }
    p = previews.get(agent_key, {"headline": "Analysed", "stat": "", "insight": ""})
    p["label"] = _AGENT_LABELS.get(agent_key, agent_key)
    return p


async def _sse_gen(audit_id: int, deep_visual: bool = False) -> AsyncGenerator[str, None]:
    """Poll DB every 500ms and emit SSE events.

    Parallel agent fix (issues 4+5+6):
      Old design watched current_agent (single string) → 5 parallel agents thrash
      it at ~0ms intervals; SSE at 500ms misses 4 of 5 completions.

      New design watches which DB columns transition null → populated.
      Each column appearing = one done event, regardless of concurrency.
      current_agent is still used for sequential agents (brand_basics,
      social_media_audit) and the __parallel__ sentinel for phase3_start.

    TRIBE v2 SSE (issue 9):
      Watches tribe_status inside social_media_audit JSON blob.
      Emits tribe_started / tribe_complete events once each.
    """
    from sqlmodel import Session as S

    # ── Instant cache-hit ──────────────────────────────────────────────────────
    with S(engine) as session:
        _check = session.get(AuditRun, audit_id)
        if _check and _check.current_agent == "__cached__" and _check.status == "complete":
            yield f"data: {json.dumps({'status': 'cache_hit'})}\n\n"
            yield f"data: {json.dumps({'status': 'complete', 'report_url': f'/report/{audit_id}', 'progress_pct': 100, 'from_cache': True})}\n\n"
            return

    # 6 parallel Phase-2 agents — done events driven by field population, not current_agent
    _PARALLEL_KEYS: set[str] = {
        "content_catalog", "performance_ads", "geo_visibility",
        "store_cro", "research", "social_profile",
    }

    agent_t: dict[str, float] = {}     # agent_key → time we first saw it "running"
    _seen_done: set[str] = set()       # agents whose done event already fired
    _phase2_started   = False
    _phase2_done      = False
    _tribe_announced  = False          # prevent duplicate tribe_started events
    last_current: Optional[str] = None

    deadline = time.monotonic() + (7200 if deep_visual else 1800)
    ka_at    = time.monotonic()

    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)

        if time.monotonic() - ka_at > 20:
            yield ": ka\n\n"
            ka_at = time.monotonic()

        with S(engine) as session:
            audit = session.get(AuditRun, audit_id)
            if not audit:
                yield f"data: {json.dumps({'status': 'error', 'error': 'Audit not found'})}\n\n"
                return
            current   = audit.current_agent
            status    = audit.status
            pct       = audit.progress_pct
            audit_err = getattr(audit, "error", None)
            _agent_data: dict[str, Optional[str]] = {
                "brand_basics":       audit.brand_basics,
                "content_catalog":    audit.content_catalog,
                "performance_ads":    audit.performance_ads,
                "geo_visibility":     audit.geo_visibility,
                "store_cro":          audit.store_cro,
                "research":           audit.research,
                "social_profile":     audit.social_profile,
                "social_media_audit": audit.social_media_audit,
            }

        # ── Sequential agent: brand_basics or social_media_audit ──────────────
        if current != last_current:
            if current not in _PARALLEL_KEYS and current not in (None, "__parallel__"):
                agent_t[current] = time.monotonic()
                step = (AGENT_SEQUENCE.index(current) + 1) if current in AGENT_SEQUENCE else 0
                yield f"data: {json.dumps({'key': current, 'agent': _AGENT_LABELS.get(current, current), 'status': 'running', 'step': step, 'of': len(AGENT_SEQUENCE)})}\n\n"

            # __parallel__ sentinel → Phase 2 is starting
            if current == "__parallel__" and not _phase2_started:
                _phase2_started = True
                for pk in _PARALLEL_KEYS:
                    agent_t[pk] = time.monotonic()
                yield f"data: {json.dumps({'status': 'phase3_start'})}\n\n"

            last_current = current

        # ── Watch DB columns for done events (works for all agents, parallel or not) ──
        for key in AGENT_SEQUENCE:
            if key in _seen_done:
                continue
            raw = _agent_data.get(key)
            if not raw:
                continue
            # Column just got populated → fire done event
            _seen_done.add(key)
            elapsed = round(time.monotonic() - agent_t.get(key, time.monotonic()), 1)
            try:
                r = json.loads(raw)
            except Exception:
                r = {}
            yield f"data: {json.dumps({'key': key, 'agent': _AGENT_LABELS.get(key, key), 'status': 'done', 'elapsed': elapsed, 'progress_pct': pct, 'preview': _extract_preview(key, r)})}\n\n"

            # phase3_done — fire once all 6 parallel agents are in _seen_done
            if not _phase2_done and _PARALLEL_KEYS.issubset(_seen_done):
                _phase2_done = True
                agents_payload = []
                for pk in _PARALLEL_KEYS:
                    try:
                        pr = json.loads(_agent_data[pk]) if _agent_data.get(pk) else {}
                    except Exception:
                        pr = {}
                    agents_payload.append({
                        "key":     pk,
                        "agent":   _AGENT_LABELS.get(pk, pk),
                        "preview": _extract_preview(pk, pr),
                    })
                yield f"data: {json.dumps({'status': 'phase3_done', 'progress_pct': pct, 'agents': agents_payload})}\n\n"

        # ── TRIBE v2 status (issue 9) ──────────────────────────────────────────
        if not _tribe_announced and _agent_data.get("social_media_audit"):
            try:
                sma = json.loads(_agent_data["social_media_audit"])
                tribe_status = sma.get("tribe_status", "none")
                if tribe_status == "processing":
                    _tribe_announced = True
                    yield f"data: {json.dumps({'status': 'tribe_started'})}\n\n"
                elif tribe_status == "complete":
                    _tribe_announced = True
                    reels = sma.get("reels_tribe") or []
                    ok = len([r for r in reels if not r.get("error")])
                    yield f"data: {json.dumps({'status': 'tribe_complete', 'reels_processed': ok})}\n\n"
            except Exception:
                pass

        if status == "complete":
            yield f"data: {json.dumps({'status': 'complete', 'report_url': f'/report/{audit_id}', 'progress_pct': 100})}\n\n"
            return

        if status == "failed":
            yield f"data: {json.dumps({'status': 'failed', 'error': audit_err or 'Audit failed'})}\n\n"
            return

    limit_label = "2-hour" if deep_visual else "30-minute"
    yield f"data: {json.dumps({'status': 'timeout', 'error': f'Exceeded {limit_label} limit'})}\n\n"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def ui():
    return HTMLResponse(UI_HTML)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.3.0"}


@app.get("/status")
async def system_status():
    """System health check — returns api, database, cache, groq, playwright, mastra, forecasting."""
    result: dict[str, str] = {"api": "ok"}

    result["database"] = db_backend()
    result["cache"] = "redis" if _cache.backend == "upstash" else "in-memory"

    groq_key = _os.environ.get("GROQ_API_KEY", "")
    result["groq"] = "ok" if groq_key else "error: no GROQ_API_KEY"

    gemini_key = _os.environ.get("GEMINI_API_KEY", "")
    result["gemini"] = "ok (fallback ready)" if gemini_key else "not configured"

    try:
        from playwright.async_api import async_playwright  # noqa: F401
        result["playwright"] = "ok"
    except ImportError:
        result["playwright"] = "not installed"

    mastra_url = _os.environ.get("MASTRA_URL", "").strip()
    result["mastra"] = "connected" if mastra_url else "not configured"

    try:
        import sys
        import importlib
        tribe_repo = Path(__file__).parent.parent / "tribeV2"
        if tribe_repo.exists() and str(tribe_repo) not in sys.path:
            sys.path.insert(0, str(tribe_repo))
        if importlib.util.find_spec("tribev2"):
            ckpt_dir = _os.getenv("TRIBE_CHECKPOINT_DIR", "facebook/tribev2")
            ckpt_local = Path(ckpt_dir)
            # Check local dir first, then HuggingFace hub cache
            local_ok = ckpt_local.exists() and (ckpt_local / "best.ckpt").exists()
            hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
            hf_ok = any(
                (p / "best.ckpt").exists()
                for p in hf_cache.glob("models--facebook--tribev2/snapshots/*")
            ) if hf_cache.exists() else False
            result["tribe_v2"] = "loaded" if (local_ok or hf_ok) else "checkpoint needed"
        else:
            result["tribe_v2"] = "not installed"
    except Exception:
        result["tribe_v2"] = "not installed"

    return result


@app.post("/audit")
async def start_audit(
    request: AuditRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    url = request.url.strip()

    # ── Cache check ───────────────────────────────────────────────────────────
    cached_data = await _cache.get(_cache.audit_key(url))
    if cached_data:
        # Build a completed AuditRun instantly from cache (no LLM calls)
        results = cached_data.get("results") or {}
        audit = AuditRun(
            url=url,
            status="complete",
            progress_pct=100,
            current_agent="__cached__",   # sentinel for SSE generator
            share_token=secrets.token_urlsafe(8),
            brand_basics=json.dumps(results.get("brand_basics")),
            content_catalog=json.dumps(results.get("content_catalog")),
            performance_ads=json.dumps(results.get("performance_ads")),
            geo_visibility=json.dumps(results.get("geo_visibility")),
            store_cro=json.dumps(results.get("store_cro")),
            research=json.dumps(results.get("research")),
            social_profile=json.dumps(results.get("social_profile")),
            social_media_audit=json.dumps(results.get("social_media_audit")),
        )
        session.add(audit)
        session.commit()
        session.refresh(audit)
        # Pre-render and cache the HTML too
        try:
            audit.report_html = generate_audit_report(cached_data)
            session.add(audit)
            session.commit()
        except Exception:
            pass
        return {
            "audit_id":   audit.id,
            "report_url": f"/report/{audit.id}",
            "stream_url": f"/audit/stream/{audit.id}",
            "status":     "cached",
            "from_cache": True,
        }

    # ── Live pipeline ─────────────────────────────────────────────────────────
    from agents.agentic_orchestrator import _brand_name_from_url
    brand_name = _brand_name_from_url(url)
    audit = AuditRun(
        url=url, status="pending",
        share_token=secrets.token_urlsafe(8),
        monitoring=request.scheduled,
    )
    session.add(audit)
    session.commit()
    session.refresh(audit)

    # Try Mastra first; fall back to Python orchestrator if unavailable
    mastra_started = await _try_mastra_audit(audit.id, url, brand_name)
    if not mastra_started:
        background_tasks.add_task(_run_audit_bg, audit.id, request.deep_visual)

    return {
        "audit_id":     audit.id,
        "report_url":   f"/report/{audit.id}",
        "stream_url":   f"/audit/stream/{audit.id}",
        "status":       "queued",
        "from_cache":   False,
        "orchestrator": "mastra" if mastra_started else "python",
    }


@app.get("/audit/stream/{audit_id}")
async def audit_stream(audit_id: int, deep_visual: int = 0, session: Session = Depends(get_session)):
    audit = session.get(AuditRun, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    return StreamingResponse(
        _sse_gen(audit_id, deep_visual=bool(deep_visual)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/audit/{audit_id}/tribe-status")
async def tribe_status(audit_id: int, session: Session = Depends(get_session)):
    """Poll TRIBE v2 background processing status.

    Returns tribe_status: none | pending | processing | complete | failed
    When complete, also returns reels_count so the frontend knows how many brain maps to expect.
    """
    audit = session.get(AuditRun, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    sma = _parse_json(audit.social_media_audit) or {}
    status = sma.get("tribe_status", "none")
    return {
        "audit_id":       audit_id,
        "status":         status,
        "tribe_available": sma.get("tribe_available", False),
        "reels_count":    len(sma.get("reels_tribe", [])),
        "error":          sma.get("tribe_error"),
    }


@app.get("/audit/{audit_id}/tribe-video/{reel_idx}/{kind}")
async def tribe_video(
    audit_id: int,
    reel_idx: int,
    kind: str,
    session: Session = Depends(get_session),
):
    """Stream the reel video or brain simulation MP4 for a processed Reel.

    kind: 'reel' | 'brain'
    Returns 200 with video/mp4 content, 404 if not ready yet.
    """
    from fastapi.responses import FileResponse

    if kind not in ("reel", "brain"):
        raise HTTPException(status_code=400, detail="kind must be 'reel' or 'brain'")

    audit = session.get(AuditRun, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")

    sma = _parse_json(audit.social_media_audit) or {}
    reels_tribe = sma.get("reels_tribe", [])
    if reel_idx >= len(reels_tribe):
        raise HTTPException(status_code=404, detail=f"Reel index {reel_idx} not found")

    reel = reels_tribe[reel_idx]
    path_key = "reel_video_path" if kind == "reel" else "sim_video_path"
    video_path = reel.get(path_key)

    if not video_path or not _os.path.exists(video_path):
        raise HTTPException(status_code=404, detail=f"{kind} video not yet generated")

    return FileResponse(
        video_path,
        media_type="video/mp4",
        headers={"Content-Disposition": f"inline; filename=\"tribe_{kind}_{reel_idx}.mp4\""},
    )


@app.get("/tribe-video/{filename}")
async def serve_tribe_video(filename: str):
    """Serve pre-computed brain simulation MP4s from cache/tribe_videos/ for the virality predictor."""
    from fastapi.responses import FileResponse

    # Security: only allow safe filenames (hex hash + _brain/_reel + .mp4)
    import re as _re_tv
    if not _re_tv.fullmatch(r"[0-9a-f]{10,16}_(brain|reel)\.mp4", filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    video_path = _os.path.join("cache", "tribe_videos", filename)
    if not _os.path.exists(video_path):
        raise HTTPException(status_code=404, detail="Video not found")

    return FileResponse(
        video_path,
        media_type="video/mp4",
        headers={"Content-Disposition": f"inline; filename=\"{filename}\""},
    )


@app.get("/report/section/{audit_id}/{agent_name}", response_class=HTMLResponse)
async def get_section_html(audit_id: int, agent_name: str, session: Session = Depends(get_session)):
    """Return the HTML fragment for a single audit section — used by progressive reveal."""
    from reports.generator import generate_section as _gen_section

    _valid_keys = {"brand_basics", "content_catalog", "performance_ads",
                   "geo_visibility", "store_cro", "research",
                   "social_profile", "social_media_audit"}
    if agent_name not in _valid_keys:
        raise HTTPException(status_code=400, detail=f"Unknown agent: {agent_name}")

    audit = session.get(AuditRun, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")

    audit_data = _assemble_audit_data(audit)
    html = _gen_section(agent_name, audit_data)
    return HTMLResponse(html)


@app.get("/report/{audit_id}", response_class=HTMLResponse)
async def get_report(audit_id: int, request: Request, session: Session = Depends(get_session)):
    audit = session.get(AuditRun, audit_id)
    if not audit:
        return HTMLResponse(
            _html_error_page(
                "Report Not Found",
                f"No audit report exists for ID #{audit_id}. "
                "It may have expired, been deleted, or the audit never completed. "
                "Run a new audit to generate a fresh report.",
                help_url="/",
            ),
            status_code=404,
        )

    if audit.status == "failed":
        return HTMLResponse(
            _html_error_page(
                "Audit Failed",
                f"The audit for <strong>{audit.url}</strong> encountered errors and could not complete. "
                "This usually happens when the site is down or blocking automated access.",
                help_url="/",
                help_label="Run New Audit",
            ),
            status_code=200,
        )

    if audit.status != "complete":
        return HTMLResponse(_LOADING_PAGE, status_code=202)

    if audit.report_html:
        html = audit.report_html
    else:
        try:
            html = generate_audit_report(_assemble_audit_data(audit))
            from sqlmodel import Session as S
            with S(engine) as s2:
                a2 = s2.get(AuditRun, audit_id)
                if a2:
                    a2.report_html = html
                    s2.add(a2)
                    s2.commit()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")

    if audit.share_token:
        base = str(request.base_url).rstrip("/")
        share_url = f"{base}/share/{audit.share_token}"
        created = _fmt_ist(audit.created_at)
        html = _inject_toolbar(html, share_url, created)

    return HTMLResponse(html)


@app.get("/share/{token}", response_class=HTMLResponse)
async def share_report(token: str, request: Request):
    """Public share link — returns the full report HTML without authentication."""
    from sqlmodel import Session as S, select
    with S(engine) as session:
        stmt = select(AuditRun).where(AuditRun.share_token == token)
        audit = session.exec(stmt).first()

    if not audit:
        return HTMLResponse(
            _html_error_page(
                "Report Not Found",
                "This share link is invalid or has expired. "
                "Ask the sender to regenerate the report link.",
                help_url="/",
            ),
            status_code=404,
        )
    if audit.status != "complete":
        return HTMLResponse(_LOADING_PAGE, status_code=202)

    if audit.report_html:
        html = audit.report_html
    else:
        try:
            html = generate_audit_report(_assemble_audit_data(audit))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")

    base = str(request.base_url).rstrip("/")
    share_url = f"{base}/share/{token}"
    created = _fmt_ist(audit.created_at)
    html = _inject_toolbar(html, share_url, created)

    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/share/compare/{token}", response_class=HTMLResponse)
async def share_compare(token: str, request: Request):
    """Public share link for a comparison report."""
    from sqlmodel import Session as S, select
    with S(engine) as session:
        stmt = select(CompareRun).where(CompareRun.compare_share_token == token)
        cr = session.exec(stmt).first()

    if not cr:
        return HTMLResponse(
            _html_error_page(
                "Comparison Not Found",
                "This comparison share link is invalid or has expired. "
                "Ask the sender to reshare the comparison.",
                help_url="/",
            ),
            status_code=404,
        )
    if cr.status != "complete":
        return HTMLResponse(_LOADING_PAGE, status_code=202)
    if not cr.compare_html:
        raise HTTPException(status_code=500, detail="Report not generated")
    html = cr.compare_html
    created = _fmt_ist(cr.created_at) if hasattr(cr, "created_at") and cr.created_at else ""
    html = _inject_toolbar(html, request.url._url, created)
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=3600"})


def _swot_fallback(brand_a: str, brand_b: str) -> dict:
    """Structured placeholder returned when LLM cannot generate a parseable SWOT."""
    placeholder = [
        {"point": "Analysis could not be generated — audit data may be incomplete", "evidence": "Retry after both audits fully complete"},
        {"point": "Please retry in a few moments", "evidence": ""},
        {"point": "Contact support if this persists", "evidence": ""},
    ]
    return {
        "brand_a_swot": {"strengths": placeholder, "weaknesses": placeholder, "opportunities": placeholder, "threats": placeholder},
        "brand_b_swot": {"strengths": placeholder, "weaknesses": placeholder, "opportunities": placeholder, "threats": placeholder},
        "overall_winner": brand_a,
        "winning_margin": "narrow",
        "match_summary": f"SWOT analysis could not be generated for {brand_a} vs {brand_b}. Please retry.",
        "head_to_head_verdict": "SWOT generation failed — LLM response could not be parsed. Try again.",
        "_fallback": True,
    }


@app.post("/compare/{compare_id}/swot")
async def generate_swot(compare_id: int, session: Session = Depends(get_session)):
    """Generate (and cache) a SWOT analysis for a completed comparison."""
    cr = session.get(CompareRun, compare_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Comparison not found")
    if cr.status != "complete":
        raise HTTPException(status_code=409, detail="Comparison not yet complete")

    # Return cached SWOT if available
    if cr.swot_json:
        return json.loads(cr.swot_json)

    # Reconstruct audit data
    from sqlmodel import Session as S
    with S(engine) as s:
        audit_a = s.get(AuditRun, cr.audit_id_a)
        audit_b = s.get(AuditRun, cr.audit_id_b)

    if not audit_a or not audit_b:
        raise HTTPException(status_code=404, detail="Audit data not found")

    audit_data_a = _assemble_audit_data(audit_a)
    audit_data_b = _assemble_audit_data(audit_b)
    results_a = audit_data_a.get("results") or audit_data_a
    results_b = audit_data_b.get("results") or audit_data_b

    brand_a = audit_data_a.get("brand_name") or cr.url_a
    brand_b = audit_data_b.get("brand_name") or cr.url_b

    ctx_a = _build_rich_context(brand_a, results_a)
    ctx_b = _build_rich_context(brand_b, results_b)

    prompt = _SWOT_PROMPT.format(
        brand_a=brand_a,
        brand_b=brand_b,
        context_a=json.dumps(ctx_a, indent=2),
        context_b=json.dumps(ctx_b, indent=2),
    )

    try:
        llm = get_client()
        swot = await llm.analyze_structured(
            system_prompt=prompt,
            user_content="Generate the SWOT analysis now.",
            max_tokens=3000,
        )

        print(
            f"[swot {compare_id}] LLM result: "
            f"keys={list(swot.keys()) if isinstance(swot, dict) else type(swot).__name__}",
            flush=True,
        )

        if isinstance(swot, dict) and swot.get("_parse_error"):
            raw_text = swot.get("_raw", "")
            print(
                f"[swot {compare_id}] Parse error — raw response ({len(raw_text)} chars):\n{raw_text[:800]}",
                flush=True,
            )
            return _swot_fallback(brand_a, brand_b)

        if isinstance(swot, dict) and "brand_a_swot" in swot:
            with S(engine) as s:
                cr2 = s.get(CompareRun, compare_id)
                if cr2:
                    cr2.swot_json = json.dumps(swot)
                    s.add(cr2)
                    s.commit()
            return swot

        print(f"[swot {compare_id}] Unexpected structure: {swot}", flush=True)
        return _swot_fallback(brand_a, brand_b)

    except Exception as exc:
        print(f"[swot {compare_id}] Exception: {exc}", flush=True)
        raise HTTPException(status_code=500, detail=f"SWOT generation failed: {exc}")


class StrategyRequest(BaseModel):
    brand: str          # "a" or "b"
    compare_id: int
    goal: str = "outperform the competitor"


@app.post("/strategy")
async def generate_strategy(req: StrategyRequest):
    """Generate (and cache) a 90-day strategy for brand A or B in a comparison."""
    if req.brand not in ("a", "b"):
        raise HTTPException(status_code=400, detail="brand must be 'a' or 'b'")

    from sqlmodel import Session as S
    with S(engine) as session:
        cr = session.get(CompareRun, req.compare_id)

    if not cr:
        raise HTTPException(status_code=404, detail="Comparison not found")
    if cr.status != "complete":
        raise HTTPException(status_code=409, detail="Comparison not yet complete")

    # Return cached strategy
    cached_field = "strategy_json_a" if req.brand == "a" else "strategy_json_b"
    cached_val = getattr(cr, cached_field)
    if cached_val:
        return json.loads(cached_val)

    with S(engine) as session:
        audit_a = session.get(AuditRun, cr.audit_id_a)
        audit_b = session.get(AuditRun, cr.audit_id_b)

    if not audit_a or not audit_b:
        raise HTTPException(status_code=404, detail="Audit data not found")

    audit_data_a = _assemble_audit_data(audit_a)
    audit_data_b = _assemble_audit_data(audit_b)
    results_a = audit_data_a.get("results") or audit_data_a
    results_b = audit_data_b.get("results") or audit_data_b

    brand_name_a = audit_data_a.get("brand_name") or cr.url_a
    brand_name_b = audit_data_b.get("brand_name") or cr.url_b

    if req.brand == "a":
        brand_name, competitor_name = brand_name_a, brand_name_b
        ctx_brand = _build_rich_context(brand_name_a, results_a)
        ctx_competitor = _build_rich_context(brand_name_b, results_b)
    else:
        brand_name, competitor_name = brand_name_b, brand_name_a
        ctx_brand = _build_rich_context(brand_name_b, results_b)
        ctx_competitor = _build_rich_context(brand_name_a, results_a)

    prompt = _STRATEGY_PROMPT.format(
        brand_name=brand_name,
        competitor_name=competitor_name,
        goal=req.goal,
        context_brand=json.dumps(ctx_brand, indent=2),
        context_competitor=json.dumps(ctx_competitor, indent=2),
    )

    try:
        llm = get_client()
        strategy = await llm.analyze_structured(
            system_prompt=prompt,
            user_content="Generate the 90-day strategy now.",
            max_tokens=2500,
        )
        if isinstance(strategy, dict) and not strategy.get("_parse_error"):
            with S(engine) as session:
                cr2 = session.get(CompareRun, req.compare_id)
                if cr2:
                    setattr(cr2, cached_field, json.dumps(strategy))
                    session.add(cr2)
                    session.commit()
            return strategy
        raise ValueError("LLM returned parse error")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Strategy generation failed: {exc}")


@app.post("/action-plan")
async def action_plan(req: ActionPlanRequest):
    """Generate a step-by-step implementation plan for a specific finding. Cached 24h."""
    cache_key = "ap:" + hashlib.sha256(
        f"{req.finding.strip()}|{req.brand_name.strip()}|{req.platform.strip()}".encode()
    ).hexdigest()[:16]

    cached = await _cache.get(cache_key)
    if cached:
        return cached

    prompt = _build_action_plan_prompt(
        brand_name=req.brand_name,
        platform=req.platform or "shopify",
        finding=req.finding,
    )
    try:
        llm = get_client()
        plan = await llm.analyze_structured(
            system_prompt=prompt,
            user_content="Generate the implementation plan now.",
            max_tokens=1500,
        )
        await _cache.set(cache_key, plan, TTL["audit"])
        return plan
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/compare")
async def start_compare(
    request: CompareRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    """Start a parallel dual-brand comparison. Returns compare_id and stream_url."""
    url_a, url_b = request.url_a, request.url_b

    # ── Cache check for Brand A ────────────────────────────────────────────────
    cached_a = await _cache.get(_cache.audit_key(url_a))
    if cached_a:
        audit_a = _make_audit_run_from_cache(url_a, cached_a, session)
        cache_hit_a = True
    else:
        audit_a = AuditRun(url=url_a, status="pending", share_token=secrets.token_urlsafe(8))
        session.add(audit_a)
        session.commit()
        session.refresh(audit_a)
        cache_hit_a = False

    # ── Cache check for Brand B ────────────────────────────────────────────────
    cached_b = await _cache.get(_cache.audit_key(url_b))
    if cached_b:
        audit_b = _make_audit_run_from_cache(url_b, cached_b, session)
        cache_hit_b = True
    else:
        audit_b = AuditRun(url=url_b, status="pending", share_token=secrets.token_urlsafe(8))
        session.add(audit_b)
        session.commit()
        session.refresh(audit_b)
        cache_hit_b = False

    # ── Create CompareRun row ──────────────────────────────────────────────────
    compare = CompareRun(
        url_a=url_a,
        url_b=url_b,
        audit_id_a=audit_a.id,
        audit_id_b=audit_b.id,
        cache_hit_a=cache_hit_a,
        cache_hit_b=cache_hit_b,
        status="pending",
        compare_share_token=secrets.token_urlsafe(8),
    )
    session.add(compare)
    session.commit()
    session.refresh(compare)

    background_tasks.add_task(
        _run_compare_bg, compare.id, audit_a.id, audit_b.id, url_a, url_b
    )

    return {
        "compare_id": compare.id,
        "stream_url": f"/compare/stream/{compare.id}",
        "cache_hit_a": cache_hit_a,
        "cache_hit_b": cache_hit_b,
    }


@app.get("/compare/stream/{compare_id}")
async def compare_stream(compare_id: int, session: Session = Depends(get_session)):
    cr = session.get(CompareRun, compare_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Comparison not found")
    return StreamingResponse(
        _compare_sse_gen(compare_id, cr.audit_id_a, cr.audit_id_b),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/compare/{compare_id}", response_class=HTMLResponse)
async def get_compare(compare_id: int, request: Request, session: Session = Depends(get_session)):
    cr = session.get(CompareRun, compare_id)
    if not cr:
        raise HTTPException(status_code=404, detail="Comparison not found")
    if cr.status != "complete":
        return HTMLResponse(_LOADING_PAGE, status_code=202)
    if not cr.compare_html:
        raise HTTPException(status_code=500, detail="Report not yet generated")
    html = cr.compare_html
    if cr.share_token:
        base = str(request.base_url).rstrip("/")
        share_url = f"{base}/share/compare/{cr.share_token}"
        created = _fmt_ist(cr.created_at) if hasattr(cr, "created_at") and cr.created_at else ""
        html = _inject_toolbar(html, share_url, created)
    return HTMLResponse(html)


@app.post("/virality")
async def run_virality(
    request: ViralityRequest,
    session: Session = Depends(get_session),
):
    """Run the virality predictor synchronously and return the full result JSON."""
    run = ViralityRun(
        url=request.url or "",
        product_name=request.product_name or "",
        description=request.description or "",
        status="running",
    )
    session.add(run)
    session.commit()
    run_id = run.id  # capture before session scope changes

    from sqlmodel import Session as S
    import logging as _logging
    _vlog = _logging.getLogger("virality_endpoint")
    try:
        predictor = ViralityPredictor(get_client(), WebScraper(), SearchAgent())
        _vlog.warning("[virality] starting predict()")
        result = await predictor.predict(
            url=request.url,
            product_name=request.product_name,
            description=request.description,
            category=request.category,
        )
        _vlog.warning("[virality] predict() done, error=%s", result.get("error"))
        _vlog.warning("[virality] json.dumps(result)...")
        result_json = json.dumps(result, default=str)
        _vlog.warning("[virality] json.dumps OK")
        with S(engine) as s2:
            r2 = s2.get(ViralityRun, run_id)
            r2.status = "complete"
            r2.result = result_json
            r2.score  = result.get("score")
            s2.add(r2)
            s2.commit()
        return {
            "run_id":           run_id,
            "virality_card_url": f"/virality/{run_id}/report",
            **result,
        }
    except Exception as exc:
        import traceback as _tb
        _vlog.error("[virality] FAILED: %s\n%s", exc, _tb.format_exc())
        with S(engine) as s2:
            r2 = s2.get(ViralityRun, run_id)
            if r2:
                r2.status = "failed"
                r2.error  = str(exc)
                s2.add(r2)
                s2.commit()
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/virality/{run_id}/report", response_class=HTMLResponse)
async def get_virality_report(run_id: int, session: Session = Depends(get_session)):
    run = session.get(ViralityRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Virality run not found")
    if run.status != "complete" or not run.result:
        raise HTTPException(status_code=409, detail=f"Run not complete (status: {run.status})")
    try:
        data = _parse_json(run.result)
        data.setdefault("product_name", run.product_name)
        data.setdefault("url", run.url)

        # Inject tribe video URLs if missing (old runs saved before this was wired)
        if not data.get("tribe_sim_video_url") and data.get("brain_map_source") == "tribe_v2":
            # Check demo cache first for a matching run_id
            _demo_path = _os.path.join("demo", "virality_scores.json")
            if _os.path.exists(_demo_path):
                try:
                    import json as _json2
                    with open(_demo_path) as _df:
                        _demo = _json2.load(_df)
                    for _de in _demo:
                        if _de.get("run_id") == run_id and _de.get("tribe_sim_video_url"):
                            data["tribe_sim_video_url"]  = _de["tribe_sim_video_url"]
                            data["tribe_reel_video_url"] = _de.get("tribe_reel_video_url", "")
                            break
                except Exception:
                    pass
            # Fallback: scan cache/tribe_videos/ for any brain.mp4 that exists
            if not data.get("tribe_sim_video_url"):
                _tv_dir = _os.path.join("cache", "tribe_videos")
                if _os.path.isdir(_tv_dir):
                    import glob as _glob
                    _brain_files = sorted(_glob.glob(_os.path.join(_tv_dir, "*_brain.mp4")), key=_os.path.getmtime, reverse=True)
                    if _brain_files:
                        _bn = _os.path.basename(_brain_files[0])
                        _h  = _bn.replace("_brain.mp4", "")
                        data["tribe_sim_video_url"]  = f"/tribe-video/{_h}_brain.mp4"
                        _reel = _os.path.join(_tv_dir, f"{_h}_reel.mp4")
                        if _os.path.exists(_reel):
                            data["tribe_reel_video_url"] = f"/tribe-video/{_h}_reel.mp4"

        return HTMLResponse(generate_virality_card(data))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}")


# ── Mastra integration helpers ────────────────────────────────────────────────

async def _try_mastra_audit(audit_id: int, url: str, brand_name: str) -> bool:
    """Fire the Mastra auditWorkflow. Returns True if accepted, False if Mastra is unavailable.

    Skips immediately if MASTRA_URL env var is not set — no connection attempt.
    """
    if not _MASTRA_ENABLED:
        return False
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=4.0) as client:
            r = await client.post(
                f"{_MASTRA_URL}/api/workflows/auditWorkflow/execute",
                json={"inputData": {"audit_id": audit_id, "url": url, "brand_name": brand_name}},
            )
            return r.status_code < 400
    except Exception:
        return False


# ── Internal endpoints — called by Mastra, not exposed to end users ────────────

class _InternalAgentReq(BaseModel):
    url: str
    brand_name: str


class _InternalProgressReq(BaseModel):
    agent_key: str
    status: str          # running | done | error
    result: Optional[dict] = None
    error: Optional[str] = None


class _InternalCompleteReq(BaseModel):
    results: dict


@app.post("/internal/scrape/homepage", dependencies=[Depends(_require_internal)])
async def internal_scrape_homepage(req: _InternalAgentReq):
    """Scrape a homepage and return raw structured content (no LLM)."""
    from scrapers.web_scraper import WebScraper
    scraper = WebScraper()
    try:
        data = await scraper.scrape_page(req.url)
        return data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _make_internal_agent_endpoint(agent_key: str):
    """Factory — creates a POST endpoint that runs one agent and returns its result dict."""
    from agents.brand_basics    import BrandBasicsAgent
    from agents.content_catalog import ContentCatalogAgent
    from agents.performance_ads import PerformanceAdsAgent
    from agents.geo_visibility  import GEOVisibilityAgent
    from agents.store_cro       import StoreCROAgent
    from agents.research        import ResearchAgent

    _agent_classes = {
        "brand_basics":    BrandBasicsAgent,
        "content_catalog": ContentCatalogAgent,
        "performance_ads": PerformanceAdsAgent,
        "geo_visibility":  GEOVisibilityAgent,
        "store_cro":       StoreCROAgent,
        "research":        ResearchAgent,
    }

    async def _endpoint(req: _InternalAgentReq, _: None = Depends(_require_internal)):
        klass = _agent_classes[agent_key]
        agent = klass(get_client(), WebScraper(), SearchAgent())
        try:
            return await agent.run(req.url, req.brand_name)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    _endpoint.__name__ = f"internal_agent_{agent_key}"
    return _endpoint


app.add_api_route(
    "/internal/agent/brand_basics", _make_internal_agent_endpoint("brand_basics"),
    methods=["POST"], response_model=None, tags=["internal"],
)
app.add_api_route(
    "/internal/agent/content", _make_internal_agent_endpoint("content_catalog"),
    methods=["POST"], response_model=None, tags=["internal"],
)
app.add_api_route(
    "/internal/agent/ads", _make_internal_agent_endpoint("performance_ads"),
    methods=["POST"], response_model=None, tags=["internal"],
)
app.add_api_route(
    "/internal/agent/geo", _make_internal_agent_endpoint("geo_visibility"),
    methods=["POST"], response_model=None, tags=["internal"],
)
app.add_api_route(
    "/internal/agent/store", _make_internal_agent_endpoint("store_cro"),
    methods=["POST"], response_model=None, tags=["internal"],
)
app.add_api_route(
    "/internal/agent/research", _make_internal_agent_endpoint("research"),
    methods=["POST"], response_model=None, tags=["internal"],
)


@app.put("/internal/audit/{audit_id}/progress", dependencies=[Depends(_require_internal)])
async def internal_audit_progress(audit_id: int, req: _InternalProgressReq):
    """Mastra calls this after each step to update the DB so our SSE stream sees it."""
    from sqlmodel import Session as S
    with S(engine) as session:
        audit = session.get(AuditRun, audit_id)
        if not audit:
            raise HTTPException(status_code=404, detail="Audit not found")

        audit.current_agent = req.agent_key
        if req.status == "done" and req.result is not None:
            setattr(audit, req.agent_key, json.dumps(req.result))
            done_count = sum(
                1 for k in AGENT_SEQUENCE if getattr(audit, k) is not None
            )
            audit.progress_pct = round(done_count / len(AGENT_SEQUENCE) * 100)
        elif req.status == "error":
            audit.error = req.error

        session.add(audit)
        session.commit()
    return {"ok": True}


@app.put("/internal/audit/{audit_id}/complete", dependencies=[Depends(_require_internal)])
async def internal_audit_complete(audit_id: int, req: _InternalCompleteReq):
    """Mastra calls this when all 6 steps finish to mark the audit complete."""
    from sqlmodel import Session as S
    with S(engine) as session:
        audit = session.get(AuditRun, audit_id)
        if not audit:
            raise HTTPException(status_code=404, detail="Audit not found")

        for key, val in req.results.items():
            if hasattr(audit, key) and val is not None:
                setattr(audit, key, json.dumps(val))

        audit.status       = "complete"
        audit.progress_pct = 100
        audit.current_agent = None
        session.add(audit)
        session.commit()
        session.refresh(audit)

        # Generate and cache the HTML report
        try:
            audit_data = _assemble_audit_data(audit)
            audit.report_html = generate_audit_report(audit_data)
            session.add(audit)
            session.commit()
            cache_key = _cache.audit_key(audit.url)
            await _cache.set(cache_key, audit_data, TTL["audit"])
        except Exception:
            pass

    return {"ok": True}


class _MemoryNoteReq(BaseModel):
    resource: str
    geo_score: Optional[float] = None
    brand_name: str


@app.post("/internal/audit/{audit_id}/memory-note", dependencies=[Depends(_require_internal)])
async def internal_memory_note(audit_id: int, req: _MemoryNoteReq):
    """Return a trend note by comparing current geo_score to the most recent past audit for this URL."""
    from sqlmodel import Session as S, select
    note: Optional[str] = None
    if req.geo_score is not None:
        with S(engine) as session:
            # Find the latest *previous* completed audit for the same domain pattern
            stmt = (
                select(AuditRun)
                .where(AuditRun.status == "complete")
                .where(AuditRun.id != audit_id)
                .order_by(AuditRun.id.desc())  # type: ignore[attr-defined]
                .limit(5)
            )
            rows = session.exec(stmt).all()
            for row in rows:
                # Match by domain (crude but effective without a separate column)
                if req.resource.split(":")[-1] in (row.url or ""):
                    prev_geo = _parse_json(row.geo_visibility)
                    if prev_geo:
                        prev_score = (prev_geo.get("analysis") or {}).get("geo_score")
                        if prev_score is not None:
                            try:
                                delta = req.geo_score - float(prev_score)
                                sign  = "+" if delta >= 0 else ""
                                note  = (
                                    f"GEO score changed from {int(prev_score)} → "
                                    f"{int(req.geo_score)} ({sign}{int(delta)}) since last audit"
                                )
                            except (TypeError, ValueError):
                                pass
                    break
    return {"note": note}


@app.post("/internal/virality/score", dependencies=[Depends(_require_internal)])
async def internal_virality_score(req: ViralityRequest):
    """Run virality predictor — called by Mastra viralityWorkflow."""
    try:
        predictor = ViralityPredictor(get_client(), WebScraper(), SearchAgent())
        return await predictor.predict(
            url=req.url,
            product_name=req.product_name,
            description=req.description,
            category=req.category,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Cache management ──────────────────────────────────────────────────────────

@app.delete("/cache/clear/{audit_id}")
async def cache_clear(audit_id: int, session: Session = Depends(get_session)):
    """Admin: invalidate the cache entry for a given audit's URL."""
    audit = session.get(AuditRun, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    key = _cache.audit_key(audit.url)
    await _cache.invalidate(key)
    return {"invalidated": key, "url": audit.url}


@app.get("/cache/status")
async def cache_status():
    """Return which cache backend is active."""
    return {"backend": _cache.backend}


# ── Brands / monitoring endpoints ────────────────────────────────────────────

@app.get("/brands")
async def list_brands(session: Session = Depends(get_session)):
    """Return all unique brands ever audited, with latest audit info."""
    from sqlmodel import select as _sel, func as _func
    from db.models import ScoreHistory

    # Get all complete audits, one per URL (latest)
    all_audits = session.exec(
        _sel(AuditRun)
        .where(AuditRun.status == "complete")
        .order_by(AuditRun.created_at.desc())
    ).all()

    seen: set[str] = set()
    brands = []
    for audit in all_audits:
        url = audit.url
        if url in seen:
            continue
        seen.add(url)

        # Get latest ScoreHistory for this URL
        sh = session.exec(
            _sel(ScoreHistory)
            .where(ScoreHistory.brand_url == url)
            .order_by(ScoreHistory.timestamp.desc())
            .limit(1)
        ).first()

        brands.append({
            "url":          url,
            "audit_id":     audit.id,
            "last_audited": audit.created_at.isoformat(),
            "monitoring":   audit.monitoring,
            "overall_score": sh.overall_score if sh else None,
            "content_score": sh.content_score if sh else None,
            "geo_score":     sh.geo_score     if sh else None,
            "store_score":   sh.store_score   if sh else None,
            "share_token":  audit.share_token,
        })

    return {"brands": brands}


@app.patch("/audit/{audit_id}/monitoring")
async def toggle_monitoring(
    audit_id: int,
    session: Session = Depends(get_session),
):
    """Toggle monitoring flag on an audit. Returns new monitoring state."""
    audit = session.get(AuditRun, audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
    audit.monitoring = not audit.monitoring
    session.add(audit)
    session.commit()
    return {"audit_id": audit_id, "monitoring": audit.monitoring}


# ── Video Neural Analysis (TRIBE v2) ─────────────────────────────────────────

class VideoAnalyzeRequest(BaseModel):
    video_url: str
    label: str = ""

    @field_validator("video_url")
    @classmethod
    def must_be_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("Must be a full URL starting with http:// or https://")
        return v


@app.post("/analyze-video")
async def analyze_video(req: VideoAnalyzeRequest):
    """Run Meta TRIBE v2 fMRI neural engagement analysis on any video URL.

    Supports: YouTube, Instagram Reels, TikTok, Vimeo, Twitter/X, Facebook,
    direct .mp4/.webm links, and any yt-dlp-supported platform.

    Returns neural engagement score (0-100), brain activation heatmap SVG,
    network scores, and interpretation. Processing takes ~10-30 min on CPU.
    """
    try:
        from agents.neural_engagement import NeuralEngagementAnalyzer
        from agents.brain_map import (
            generate_activation_heatmap,
            tribe_preds_to_network_scores,
            virality_dims_to_network_scores,
        )

        analyzer = NeuralEngagementAnalyzer()
        loop = asyncio.get_event_loop()

        # Run TRIBE v2 in executor (blocking, CPU-intensive)
        score_dict, preds, _reel_path, _sim_path = await asyncio.wait_for(
            loop.run_in_executor(None, analyzer._run_sync_full, req.video_url),
            timeout=3600.0,  # 1-hour hard cap
        )

        label = req.label or req.video_url.split("/")[-1][:50] or "video"

        if preds is not None:
            import numpy as _np
            preds_arr = _np.array(preds)
            network_scores = tribe_preds_to_network_scores(preds_arr)
            brain_map_svg = generate_activation_heatmap(
                network_scores, is_real_tribe=True, ad_label=label
            )
            brain_map_source = "tribe_v2"
        else:
            # No preds (error path) — use empty scores so we still return a map
            network_scores = {k: 0.0 for k in ["visual","motor","attention","limbic","default","control","reward"]}
            brain_map_svg = generate_activation_heatmap(
                network_scores, is_real_tribe=False, ad_label=label
            )
            brain_map_source = "error"

        return {
            **score_dict,
            "video_url": req.video_url,
            "label": label,
            "brain_map_svg": brain_map_svg,
            "brain_map_source": brain_map_source,
            "brain_network_scores": network_scores,
        }

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="TRIBE v2 inference timed out (>60 min)")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Demo endpoints (pre-cached, no API calls) ─────────────────────────────────

_DEMO_DIR = Path(__file__).parent / "demo"


@app.post("/admin/backfill-roadmaps")
async def backfill_roadmaps(session: Session = Depends(get_session)):
    """Regenerate roadmap_json for all complete audits that are missing it.

    Idempotent — safe to run multiple times. Skips audits that already have
    a roadmap or don't have enough agent data to build one.
    """
    from agents.orchestrator import _generate_roadmap
    from sqlmodel import select as _select

    audits = session.exec(
        _select(AuditRun)
        .where(AuditRun.status == "complete")
        .where(AuditRun.roadmap_json == None)  # noqa: E711
        .order_by(AuditRun.id.desc())
    ).all()

    if not audits:
        return {"message": "All complete audits already have roadmaps.", "updated": 0}

    llm = get_client()
    updated = 0
    skipped = 0
    errors: list[str] = []

    for audit in audits:
        try:
            results = {
                "brand_basics":       _parse_json(audit.brand_basics),
                "content_catalog":    _parse_json(audit.content_catalog),
                "performance_ads":    _parse_json(audit.performance_ads),
                "geo_visibility":     _parse_json(audit.geo_visibility),
                "store_cro":          _parse_json(audit.store_cro),
                "research":           _parse_json(audit.research),
                "social_profile":     _parse_json(audit.social_profile),
                "social_media_audit": _parse_json(audit.social_media_audit),
            }
            # Skip if we don't have enough data
            filled = sum(1 for v in results.values() if v)
            if filled < 3:
                skipped += 1
                continue

            roadmap = await _generate_roadmap(llm, results)
            if roadmap:
                audit.roadmap_json = json.dumps(roadmap)
                session.add(audit)
                session.commit()
                updated += 1
                print(f"  [backfill] audit {audit.id} — roadmap generated", flush=True)
            else:
                skipped += 1
        except Exception as exc:
            errors.append(f"audit {audit.id}: {exc}")
            continue

    return {
        "message": f"Backfill complete. {updated} updated, {skipped} skipped.",
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "total_found": len(audits),
    }


@app.post("/admin/cache/flush")
async def flush_cache():
    """Wipe the entire cache. All subsequent audits will run fresh with no cached data."""
    count = await _cache.flush_all()
    return {
        "flushed": True,
        "backend": _cache.backend,
        "entries_cleared": count if count >= 0 else "all (redis flushdb)",
    }


# ── Connector endpoints ───────────────────────────────────────────────────────

class ShopifyConnectRequest(BaseModel):
    brand_url: str
    store_url: str
    access_token: str


class MetaConnectRequest(BaseModel):
    brand_url: str
    access_token: str
    ad_account_id: str


@app.post("/connect/shopify")
async def connect_shopify(req: ShopifyConnectRequest, db: Session = Depends(get_session)):
    """Save Shopify Admin API token for a brand. Verifies the token before saving."""
    from scrapers.shopify_private import verify_token
    ok = await verify_token(req.store_url, req.access_token)
    if not ok:
        raise HTTPException(status_code=400, detail="Token verification failed. Check store URL and access token.")

    norm_url = req.brand_url.rstrip("/").lower()
    connector = db.exec(
        _sql_select(BrandConnector).where(BrandConnector.brand_url == norm_url)
    ).first()
    if connector:
        connector.shopify_token = req.access_token
        connector.shopify_store_url = req.store_url.rstrip("/")
        connector.updated_at = _datetime.utcnow()
    else:
        connector = BrandConnector(
            brand_url=norm_url,
            shopify_token=req.access_token,
            shopify_store_url=req.store_url.rstrip("/"),
        )
        db.add(connector)
    db.commit()
    return {"connected": True, "provider": "shopify", "brand_url": norm_url}


@app.post("/connect/meta")
async def connect_meta(req: MetaConnectRequest, db: Session = Depends(get_session)):
    """Save Meta Marketing API token for a brand. Verifies the token before saving."""
    from scrapers.meta_ads_api import verify_token
    ok = await verify_token(req.access_token, req.ad_account_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Token verification failed. Check access token and ad account ID.")

    norm_url = req.brand_url.rstrip("/").lower()
    connector = db.exec(
        _sql_select(BrandConnector).where(BrandConnector.brand_url == norm_url)
    ).first()
    if connector:
        connector.meta_token = req.access_token
        connector.meta_account_id = req.ad_account_id
        connector.updated_at = _datetime.utcnow()
    else:
        connector = BrandConnector(
            brand_url=norm_url,
            meta_token=req.access_token,
            meta_account_id=req.ad_account_id,
        )
        db.add(connector)
    db.commit()
    return {"connected": True, "provider": "meta", "brand_url": norm_url}


@app.get("/connect/status/{brand_url:path}")
async def connector_status(brand_url: str, db: Session = Depends(get_session)):
    """Get connector status for a brand URL."""
    norm_url = brand_url.rstrip("/").lower()
    connector = db.exec(
        _sql_select(BrandConnector).where(BrandConnector.brand_url == norm_url)
    ).first()
    if not connector:
        return {"brand_url": norm_url, "shopify": False, "meta": False}
    return {
        "brand_url": norm_url,
        "shopify": bool(connector.shopify_token),
        "shopify_store_url": connector.shopify_store_url,
        "meta": bool(connector.meta_token),
        "meta_account_id": connector.meta_account_id,
        "updated_at": connector.updated_at.isoformat() if connector.updated_at else None,
    }


@app.delete("/connect/{brand_url:path}/{provider}")
async def disconnect_connector(brand_url: str, provider: str, db: Session = Depends(get_session)):
    """Remove a specific connector (shopify or meta) for a brand."""
    if provider not in ("shopify", "meta"):
        raise HTTPException(status_code=400, detail="provider must be 'shopify' or 'meta'")
    norm_url = brand_url.rstrip("/").lower()
    connector = db.exec(
        _sql_select(BrandConnector).where(BrandConnector.brand_url == norm_url)
    ).first()
    if not connector:
        raise HTTPException(status_code=404, detail="No connector found for this brand")
    if provider == "shopify":
        connector.shopify_token = None
        connector.shopify_store_url = None
    else:
        connector.meta_token = None
        connector.meta_account_id = None
    connector.updated_at = _datetime.utcnow()
    db.commit()
    return {"disconnected": True, "provider": provider, "brand_url": norm_url}


@app.get("/connect/list")
async def list_connectors(db: Session = Depends(get_session)):
    """List all brands with at least one connector configured."""
    connectors = db.exec(
        _sql_select(BrandConnector)
    ).all()
    return [
        {
            "brand_url": c.brand_url,
            "shopify": bool(c.shopify_token),
            "shopify_store_url": c.shopify_store_url,
            "meta": bool(c.meta_token),
            "meta_account_id": c.meta_account_id,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
        for c in connectors
    ]


@app.get("/demo", response_class=HTMLResponse)
async def demo_report():
    """Serve pre-cached Rare Rabbit audit report — instant, no API calls."""
    demo_path = _DEMO_DIR / "rare_rabbit_audit.json"
    if not demo_path.exists():
        raise HTTPException(status_code=503, detail="Demo data not found. Run: python run_audit.py --url https://rarerabbit.in")
    try:
        audit_data = json.loads(demo_path.read_text(encoding="utf-8"))
        html = generate_audit_report(audit_data)
        return HTMLResponse(html)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Demo report error: {exc}")


@app.get("/demo/virality")
async def demo_virality_scores():
    """Return pre-scored virality examples for the UI example buttons."""
    demo_path = _DEMO_DIR / "virality_scores.json"
    if not demo_path.exists():
        raise HTTPException(status_code=503, detail="Demo virality data not found")
    return json.loads(demo_path.read_text(encoding="utf-8"))


# ── Brain Activation Heatmap demo ─────────────────────────────────────────────

# Real TRIBE v2 scores from confirmed mamaearth reel inference (21min CPU run)
_TRIBE_SCORES_REAL = {
    "control":   0.143,
    "default":   0.147,
    "attention": 0.147,
    "limbic":    0.162,
    "motor":     0.133,
    "visual":    0.143,
    "reward":    0.173,
}

# Estimated scores from a high-performing D2C beauty ad (virality dims → network)
_TRIBE_SCORES_EST = virality_dims_to_network_scores({
    "visual_stopping_power": 8.5,
    "transformation_clarity": 7.0,
    "hook_strength": 9.0,
    "emotional_trigger": 8.0,
    "trend_alignment": 7.5,
    "social_currency": 8.0,
    "share_trigger": 7.5,
})


@app.get("/brain-map", response_class=HTMLResponse)
async def brain_map_demo():
    """Standalone brain activation heatmap demo page."""
    svg_real = generate_activation_heatmap(
        _TRIBE_SCORES_REAL,
        is_real_tribe=True,
        ad_label="mamaearth reel (confirmed fMRI)",
    )
    svg_est = generate_activation_heatmap(
        _TRIBE_SCORES_EST,
        is_real_tribe=False,
        ad_label="high-performing D2C beauty ad (estimated)",
    )

    network_descriptions = {
        "visual":    ("Visual Cortex", "imagery · scroll-stop · motion", "ef4444",
                      "Fires when content has strong visual contrast, motion, or lifestyle imagery. High = content stops the scroll."),
        "motor":     ("Motor / CTA",   "action urge · buy impulse",       "f97316",
                      "The brain region that drives physical action. High activation = viewer has a strong urge to tap/click/buy."),
        "attention": ("Attention",     "hook · salience · 3-sec hold",    "facc15",
                      "Controls the 3-second hold. High = the opening hook is strong enough to prevent thumb-up scrolling."),
        "limbic":    ("Limbic / Emotion", "desire · fear · brand feeling", "ec4899",
                      "The emotional core — desire, aspiration, fear of missing out. High = deep emotional resonance with the brand."),
        "default":   ("Default Mode",  "identity · trend · storytelling",  "a855f7",
                      "Active during self-referential thinking. High = content aligns with viewer identity and feels personally relevant."),
        "control":   ("Prefrontal",    "trust · price eval · logic",       "3b82f6",
                      "The analytical network — evaluates price, credibility, and logic. High = viewer is seriously considering the purchase."),
        "reward":    ("Reward Circuit", "dopamine · FOMO · social proof",   "22c55e",
                      "Dopamine-driven response to social proof, scarcity, and sharing potential. High = strong FOMO and share trigger."),
    }

    network_rows = ""
    for net_id, (label, sub, color, desc) in network_descriptions.items():
        real_pct = int(_TRIBE_SCORES_REAL.get(net_id, 0) * 100)
        est_pct  = int(_TRIBE_SCORES_EST.get(net_id, 0) * 100)
        network_rows += f"""
        <tr>
          <td><span style="color:#{color};font-weight:700">{label}</span>
              <br><span style="color:#475569;font-size:.75rem">{sub}</span></td>
          <td style="text-align:center">
            <span style="color:#{color};font-weight:700;font-size:1.05rem">{real_pct}%</span></td>
          <td style="text-align:center">
            <span style="color:#f59e0b;font-weight:700;font-size:1.05rem">{est_pct}%</span></td>
          <td style="color:#94a3b8;font-size:.8rem">{desc}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Neural Brain Activation Heatmap — SHOPOS Agent</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#060d1a;--surface:#0d1829;--surface2:#111d33;--border:#1e3a5f;
  --text:#e2e8f0;--muted:#475569;--r:12px;
  --amber:#f59e0b;--green:#22c55e;--blue:#3b82f6;--red:#ef4444;
}}
html{{font-size:15px;background:var(--bg);color:var(--text)}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh;padding:2rem 1.5rem}}
a{{color:var(--amber);text-decoration:none}}
h1{{font-size:1.6rem;font-weight:800;letter-spacing:-.4px;margin-bottom:.3rem}}
h2{{font-size:1.05rem;font-weight:700;letter-spacing:-.2px;margin-bottom:1rem;color:#94a3b8}}
h3{{font-size:.9rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin-bottom:.75rem}}
p{{color:#94a3b8;font-size:.875rem;line-height:1.65;margin-bottom:.75rem}}
.wrap{{max-width:1100px;margin:0 auto}}
.hdr{{display:flex;align-items:center;justify-content:space-between;
  padding-bottom:1.25rem;border-bottom:1px solid var(--border);margin-bottom:2rem}}
.badge{{display:inline-flex;align-items:center;gap:.35rem;padding:.25rem .7rem;
  border-radius:999px;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em}}
.badge-green{{background:#052e16;color:#22c55e;border:1px solid #14532d}}
.badge-amber{{background:#1c1400;color:#f59e0b;border:1px solid #78350f}}
.grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:2rem;margin-bottom:2rem}}
@media(max-width:700px){{.grid-2{{grid-template-columns:1fr}}}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:1.5rem}}
.card-title{{font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
  color:#64748b;margin-bottom:1rem;display:flex;align-items:center;gap:.5rem}}
table{{width:100%;border-collapse:collapse;font-size:.84rem}}
th{{text-align:left;padding:.5rem .75rem;color:#64748b;font-size:.7rem;
  text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid var(--border)}}
td{{padding:.65rem .75rem;border-bottom:1px solid #0f172a;vertical-align:top}}
tr:last-child td{{border-bottom:none}}
.pill{{display:inline-flex;align-items:center;gap:.25rem;padding:.15rem .5rem;
  border-radius:4px;font-size:.68rem;font-weight:700}}
.pill-tribe{{background:#052e16;color:#22c55e}}
.pill-est{{background:#1c1400;color:#f59e0b}}
.insight-box{{background:#0d1829;border:1px solid var(--border);border-radius:var(--r);
  padding:1.25rem;margin-top:.75rem}}
.insight-box p{{margin:0}}
.back{{display:inline-flex;align-items:center;gap:.4rem;font-size:.82rem;
  color:#64748b;margin-bottom:1.75rem}}
.back:hover{{color:var(--amber)}}
</style>
</head>
<body>
<div class="wrap">
  <a href="/" class="back">← Back to agent</a>

  <div class="hdr">
    <div>
      <h1>Neural Brain Activation Heatmap</h1>
      <h2>How ad content fires the 7 Yeo functional networks · powered by Meta TRIBE v2 fMRI</h2>
    </div>
    <div style="display:flex;flex-direction:column;gap:.4rem;align-items:flex-end">
      <span class="badge badge-green">Real TRIBE v2 · fMRI</span>
      <span class="badge badge-amber">Estimated · virality</span>
    </div>
  </div>

  <p>
    <strong style="color:var(--text)">Meta TRIBE v2</strong> is an fMRI encoding model trained on
    naturalistic video (Algonauts 2025 dataset — Friends + 4 films). It predicts cortical activation
    across 1,000 Schaefer parcels (7 Yeo networks) from video/audio stimuli — giving us a neuroscience-grounded
    measure of how strongly an ad engages each brain system.
  </p>
  <p>
    Below: two heatmaps for the same creative context. The
    <span class="badge badge-green" style="font-size:.68rem">Real TRIBE v2</span> run used confirmed
    fMRI inference on a mamaearth reel (21 min CPU, shape 9×20484 after Schaefer resampling).
    The <span class="badge badge-amber" style="font-size:.68rem">Estimated</span> map shows what the
    model predicts for a high-performing D2C beauty ad using virality dimension scoring.
  </p>

  <div class="grid-2">
    <div class="card">
      <div class="card-title">
        <span class="pill pill-tribe">Real TRIBE v2</span>
        Real fMRI inference — mamaearth reel
      </div>
      {svg_real}
    </div>
    <div class="card">
      <div class="card-title">
        <span class="pill pill-est">Estimated</span>
        Virality dims → network mapping
      </div>
      {svg_est}
    </div>
  </div>

  <div class="card" style="margin-bottom:2rem">
    <div class="card-title">Network Breakdown — What Each Region Means for Ads</div>
    <table>
      <thead>
        <tr>
          <th>Network</th>
          <th>Real fMRI</th>
          <th>Estimated</th>
          <th>Marketing interpretation</th>
        </tr>
      </thead>
      <tbody>{network_rows}</tbody>
    </table>

    <div class="insight-box" style="margin-top:1.25rem">
      <p>
        <strong style="color:#22c55e">Key insight from real fMRI data:</strong>
        The mamaearth reel shows highest activation in the
        <strong style="color:#22c55e">Reward Circuit (17%)</strong> and
        <strong style="color:#ec4899">Limbic system (16%)</strong> —
        indicating strong dopamine-driven social proof response and emotional resonance.
        The relatively balanced spread across all 7 networks (13–17%) suggests
        the content engages the full brain — a hallmark of high-retention video.
      </p>
    </div>
  </div>

  <div class="card" style="margin-bottom:2rem">
    <div class="card-title">How TRIBE v2 fits into the SHOPOS Brand Audit</div>
    <p>
      When <strong style="color:var(--text)">Deep Visual Analysis</strong> is enabled on an audit,
      Agent 8 (Social Media Deep Audit) downloads Instagram Reels from the brand's profile via yt-dlp,
      runs them through TRIBE v2 locally, and generates a brain activation heatmap for each reel.
    </p>
    <p>
      The heatmap is embedded directly in the audit report alongside the reel's virality score,
      giving you a neuroscience-grounded view of which brain systems the brand's content is activating —
      and which it's leaving on the table.
    </p>
    <p style="margin-bottom:0">
      Without Deep Visual enabled, the agent uses the virality dimension scores (LLM-estimated)
      to generate the <span class="badge badge-amber" style="font-size:.68rem">Estimated</span> version shown above.
    </p>
  </div>

  <p style="color:#334155;font-size:.75rem;text-align:center;padding-top:1rem;border-top:1px solid #0f172a">
    TRIBE v2 · Meta AI Research · CC-BY-NC-4.0 · Algonauts 2025 ·
    Schaefer-1000 atlas · 7 Yeo functional networks<br>
    Scores from confirmed local inference: shape (9, 20484) · score 22 · 21 min CPU
  </p>
</div>
</body>
</html>"""
    return HTMLResponse(html)


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(_os.environ.get("PORT", 8000))
    in_production = _os.environ.get("PORT") is not None
    host = "0.0.0.0" if in_production else "127.0.0.1"

    print(f"\n  Research Agent ready at http://{host}:{port}\n")

    if not in_production:
        async def _open_browser():
            await asyncio.sleep(1.2)
            webbrowser.open(f"http://127.0.0.1:{port}")

        loop = asyncio.new_event_loop()

        async def _main():
            import threading
            t = threading.Thread(
                target=lambda: asyncio.run(_open_browser()), daemon=True
            )
            t.start()
            config = uvicorn.Config(app, host=host, port=port, log_level="warning")
            server = uvicorn.Server(config)
            await server.serve()

        loop.run_until_complete(_main())
    else:
        uvicorn.run(app, host=host, port=port, log_level="info")
