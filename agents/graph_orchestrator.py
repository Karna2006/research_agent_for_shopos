"""LangGraph orchestrator — 4-phase audit pipeline as a StateGraph.

Nodes are thin wrappers; all business logic lives in agentic_orchestrator.py unchanged.

Graph topology:
    START → prefetch → brand_basics ─[abort?]──────────────► abort → END
                                     │
                                     ▼ (no abort)
                                core_parallel ─[has_social?]─► social_depth → synthesis → END
                                              │
                                              ▼ (no social)
                                           synthesis → END
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from agents.working_memory import WorkingMemory
from agents.agentic_orchestrator import (
    _AGENT_LABELS,
    _assemble,
    _brand_name_from_url,
    _build_agents,
    _cross_validate_findings,
    _db_set,
    _is_useful,
    _nested,
    _phase0_prefetch,
    _run_gated,
    _run_with_retry,
    _synthesis,
)
from db.database import engine
from db.models import AuditRun, AGENT_SEQUENCE
from llm.client import get_client
from scrapers.web_scraper import WebScraper
from scrapers.search import SearchAgent
from sqlmodel import Session


# ── State reducers ─────────────────────────────────────────────────────────────

def _merge_dicts(a: dict, b: dict) -> dict:
    return {**a, **b}


def _concat_lists(a: list, b: list) -> list:
    return a + b


# ── AuditState ─────────────────────────────────────────────────────────────────

class AuditState(TypedDict):
    url: str
    brand_name: str
    deep_visual: bool
    agents: dict                                     # {agent_key: instance, "__scraper__": scraper}
    llm: Any
    _start_time: float                               # monotonic — passed to _assemble for total time
    prefetch: dict
    results: Annotated[dict, _merge_dicts]           # accumulated across nodes
    agent_status: Annotated[list, _concat_lists]     # accumulated across nodes
    working_memory: Any                              # WorkingMemory instance
    should_abort: bool
    has_social: bool
    progress_cb: Any                                 # async SSE callback | None
    final_output: dict                               # set by synthesis or abort node


# ── Nodes ──────────────────────────────────────────────────────────────────────

async def node_prefetch(state: AuditState) -> dict:
    print("\n  [phase0] Prefetching homepage + pagespeed…", flush=True)
    if state["progress_cb"]:
        await state["progress_cb"]("__prefetch__", "running", 0)
    prefetch = await _phase0_prefetch(state["url"], state["agents"]["__scraper__"])
    if state["progress_cb"]:
        await state["progress_cb"]("__prefetch__", "done", 10)
    return {"prefetch": prefetch}


async def node_brand_basics(state: AuditState) -> dict:
    print("  [phase1] Brand basics…", flush=True)
    if state["progress_cb"]:
        await state["progress_cb"]("brand_basics", "running", 10)

    wm: WorkingMemory = state["working_memory"]
    t0 = time.monotonic()
    result, error = await _run_with_retry(
        "brand_basics", state["agents"]["brand_basics"],
        state["url"], state["brand_name"],
        progress_cb=state["progress_cb"], progress_pct=10,
    )
    elapsed = time.monotonic() - t0
    wm.add_finding("brand_basics", result)
    wm.log(f"brand_basics {'FAILED' if error else 'done'} in {elapsed:.1f}s" + (f": {error}" if error else ""))

    should_abort = bool(
        error
        and result.get("data_coverage") == "unavailable"
        and state["prefetch"]["homepage"].confidence == "unavailable"
    )
    if should_abort:
        print("  [phase1] ABORT — brand URL unreachable.", flush=True)

    return {
        "results": {"brand_basics": result},
        "agent_status": [{
            "agent": "brand_basics",
            "label": _AGENT_LABELS["brand_basics"],
            "status": "error" if error else "done",
            "elapsed_s": round(elapsed, 2),
            "error": error,
            "data_coverage": result.get("data_coverage", "unknown"),
            "quality_ok": _is_useful("brand_basics", result),
        }],
        "should_abort": should_abort,
    }


async def node_abort(state: AuditState) -> dict:
    """Brand URL completely unreachable — mark all remaining agents as skipped."""
    skipped: dict = {
        key: {
            "agent": key, "status": "skipped",
            "skip_reason": "brand URL unreachable",
            "data_coverage": "skipped", "sources_used": [], "fallbacks_used": [],
        }
        for key in AGENT_SEQUENCE if key != "brand_basics"
    }
    final_output = _assemble(
        {**state["results"], **skipped},
        state["agent_status"],
        state["url"], state["brand_name"],
        state["_start_time"],
    )
    return {"results": skipped, "final_output": final_output}


async def node_core_parallel(state: AuditState) -> dict:
    CORE = ["content_catalog", "performance_ads", "geo_visibility", "store_cro", "research", "social_profile"]
    print(f"\n  [phase2] Launching {len(CORE)} agents in parallel…", flush=True)
    if state["progress_cb"]:
        await state["progress_cb"]("__phase2__", "running", 20)

    wm: WorkingMemory = state["working_memory"]
    new_results: dict = {}
    new_statuses: list = []

    tasks = [
        _run_gated(
            key, state["agents"][key], state["url"], state["brand_name"],
            deep_visual=False,
            progress_cb=state["progress_cb"],
            progress_pct=20 + i * 10,
        )
        for i, key in enumerate(CORE)
    ]
    raw = await asyncio.gather(*tasks, return_exceptions=True)

    for item in raw:
        if isinstance(item, Exception):
            print(f"  [phase2] gather exception: {item}", flush=True)
            continue
        key, result, error, elapsed = item
        new_results[key] = result
        wm.add_finding(key, result)
        wm.log(f"{key} {'FAILED' if error else 'done'} in {elapsed:.1f}s" + (f": {error}" if error else ""))
        new_statuses.append({
            "agent": key,
            "label": _AGENT_LABELS.get(key, key),
            "status": "error" if error else "done",
            "elapsed_s": round(elapsed, 2),
            "error": error,
            "data_coverage": result.get("data_coverage", "unknown"),
            "quality_ok": _is_useful(key, result),
        })

    if state["progress_cb"]:
        await state["progress_cb"]("__phase2__", "done", 80)

    ig_data = _nested(new_results, "social_profile", "instagram") or {}
    has_social = bool(ig_data.get("followers") or ig_data.get("bio"))

    return {"results": new_results, "agent_status": new_statuses, "has_social": has_social}


async def node_social_depth(state: AuditState) -> dict:
    print("\n  [phase3] Social media deep audit…", flush=True)
    if state["progress_cb"]:
        await state["progress_cb"]("social_media_audit", "running", 82)

    wm: WorkingMemory = state["working_memory"]
    t0 = time.monotonic()
    result, error = await _run_with_retry(
        "social_media_audit", state["agents"]["social_media_audit"],
        state["url"], state["brand_name"],
        deep_visual=state["deep_visual"],
        progress_cb=state["progress_cb"], progress_pct=82,
    )
    elapsed = time.monotonic() - t0
    wm.add_finding("social_media_audit", result)
    wm.log(f"social_media_audit {'FAILED' if error else 'done'} in {elapsed:.1f}s" + (f": {error}" if error else ""))

    return {
        "results": {"social_media_audit": result},
        "agent_status": [{
            "agent": "social_media_audit",
            "label": _AGENT_LABELS["social_media_audit"],
            "status": "error" if error else "done",
            "elapsed_s": round(elapsed, 2),
            "error": error,
            "data_coverage": result.get("data_coverage", "unknown"),
            "quality_ok": _is_useful("social_media_audit", result),
        }],
    }


async def node_synthesis(state: AuditState) -> dict:
    results = dict(state["results"])
    wm: WorkingMemory = state["working_memory"]

    if "social_media_audit" not in results:
        results["social_media_audit"] = {
            "agent": "social_media_audit", "status": "skipped",
            "skip_reason": "social_profile returned no IG data",
            "data_coverage": "skipped", "sources_used": [], "fallbacks_used": [],
        }

    # ── Optional: Store Intelligence (runs when connector tokens exist) ────────
    try:
        from sqlmodel import select as _select
        from db.models import BrandConnector
        norm_url = state["url"].rstrip("/").lower()
        with Session(engine) as _s:
            connector = _s.exec(_select(BrandConnector).where(BrandConnector.brand_url == norm_url)).first()
        if connector and (connector.shopify_token or connector.meta_token):
            print("\n  [phase4] Store intelligence (private connectors)…", flush=True)
            from agents.store_intelligence import run as _run_store_intel
            si_result = await _run_store_intel(
                url=state["url"],
                llm=state["llm"],
                shopify_token=connector.shopify_token,
                shopify_store_url=connector.shopify_store_url,
                meta_token=connector.meta_token,
                meta_account_id=connector.meta_account_id,
            )
            results["store_intelligence"] = si_result
            wm.log(f"store_intelligence done, connectors={si_result.get('connectors_used', [])}")
    except Exception as _exc:
        print(f"  [phase4] store_intelligence skipped: {_exc}", flush=True)

    print("\n  [phase4] Cross-validating findings (rule-based)…", flush=True)
    cross_findings = _cross_validate_findings(results)
    if cross_findings:
        print(f"  [phase4] {len(cross_findings)} cross-agent patterns detected", flush=True)
        for f in cross_findings:
            print(f"    [{f['impact'].upper()}] {f['pattern']}", flush=True)

    print("  [phase4] Generating synthesis (3 parallel LLM calls)…", flush=True)
    if state["progress_cb"]:
        await state["progress_cb"]("__synthesis__", "running", 90)

    analyst_brief, one_thing, roadmap = await _synthesis(state["llm"], results, cross_findings)

    if state["progress_cb"]:
        await state["progress_cb"]("__synthesis__", "done", 100)

    results["_analyst"] = {"cross_findings": cross_findings, "brief": analyst_brief}
    failed_count = len([s for s in state["agent_status"] if s["status"] == "error"])
    wm.meta_synthesis = {
        "pattern": cross_findings[0].get("pattern") if cross_findings else None,
        "posture": "triage" if failed_count >= 3 else "optimize",
        "narrative": analyst_brief.get("executive_summary", "") if analyst_brief else "",
    }

    final_output = _assemble(
        results, state["agent_status"],
        state["url"], state["brand_name"],
        state["_start_time"],
        one_thing=one_thing, roadmap=roadmap,
        analyst_brief=analyst_brief, cross_findings=cross_findings,
        agentic_meta=wm.to_report_dict(),
    )
    return {"results": results, "final_output": final_output}


# ── Conditional edges ──────────────────────────────────────────────────────────

def _route_brand_basics(state: AuditState) -> str:
    return "abort" if state["should_abort"] else "core_parallel"


def _route_core_parallel(state: AuditState) -> str:
    return "social_depth" if state["has_social"] else "synthesis"


# ── Graph compilation (done once at import time) ───────────────────────────────

def _build_graph():
    g = StateGraph(AuditState)

    g.add_node("prefetch",      node_prefetch)
    g.add_node("brand_basics",  node_brand_basics)
    g.add_node("abort",         node_abort)
    g.add_node("core_parallel", node_core_parallel)
    g.add_node("social_depth",  node_social_depth)
    g.add_node("synthesis",     node_synthesis)

    g.set_entry_point("prefetch")
    g.add_edge("prefetch", "brand_basics")
    g.add_conditional_edges(
        "brand_basics", _route_brand_basics,
        {"abort": "abort", "core_parallel": "core_parallel"},
    )
    g.add_edge("abort", END)
    g.add_conditional_edges(
        "core_parallel", _route_core_parallel,
        {"social_depth": "social_depth", "synthesis": "synthesis"},
    )
    g.add_edge("social_depth", "synthesis")
    g.add_edge("synthesis", END)

    return g.compile()


_AUDIT_GRAPH = _build_graph()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_initial_state(
    url: str,
    brand_name: str,
    deep_visual: bool,
    progress_cb=None,
) -> AuditState:
    llm     = get_client()
    scraper = WebScraper()
    search  = SearchAgent()
    agents  = _build_agents(llm, scraper, search)
    wm      = WorkingMemory(brand_name=brand_name, url=url)
    return AuditState(
        url=url,
        brand_name=brand_name,
        deep_visual=deep_visual,
        agents=agents,
        llm=llm,
        _start_time=time.monotonic(),
        prefetch={},
        results={},
        agent_status=[],
        working_memory=wm,
        should_abort=False,
        has_social=False,
        progress_cb=progress_cb,
        final_output={},
    )


# ── Public API ─────────────────────────────────────────────────────────────────

async def run_graph(url: str, deep_visual: bool = False) -> dict:
    """Complete audit — no database, no side effects."""
    brand_name = _brand_name_from_url(url)
    print(f"\nSHOPOS Brand Audit — {url}", flush=True)
    print(f"Brand: {brand_name}", flush=True)
    print("=" * 52, flush=True)
    state = _make_initial_state(url, brand_name, deep_visual)
    result = await _AUDIT_GRAPH.ainvoke(state)
    return result["final_output"]


async def run_graph_with_db(audit_id: int, deep_visual: bool = False) -> None:
    """DB-backed audit — writes progress to AuditRun, SSE-compatible."""
    with Session(engine) as session:
        audit = session.get(AuditRun, audit_id)
        if not audit:
            return
        url = audit.url
        _db_set(session, audit, status="running", progress_pct=0)

    brand_name = _brand_name_from_url(url)
    print(f"\nSHOPOS Audit #{audit_id} — {url}", flush=True)
    print(f"Brand: {brand_name}", flush=True)
    print("=" * 52, flush=True)

    _PARALLEL_KEYS = frozenset([
        "content_catalog", "performance_ads", "geo_visibility",
        "store_cro", "research", "social_profile",
    ])

    async def _progress_cb(agent_key: str, status: str, pct: int) -> None:
        with Session(engine) as s:
            a = s.get(AuditRun, audit_id)
            if a:
                if agent_key == "__phase2__" and status == "running":
                    current: str | None = "__parallel__"
                elif agent_key in _PARALLEL_KEYS:
                    current = a.current_agent  # never overwrite __parallel__ sentinel
                elif status == "running" and not agent_key.startswith("__"):
                    current = agent_key
                else:
                    current = None
                _db_set(s, a, current_agent=current, progress_pct=min(pct, 100))

    try:
        state = _make_initial_state(url, brand_name, deep_visual, progress_cb=_progress_cb)
        result = await _AUDIT_GRAPH.ainvoke(state)
        audit_data = result["final_output"]
    except Exception as exc:
        with Session(engine) as s:
            a = s.get(AuditRun, audit_id)
            if a and a.status != "complete":
                _db_set(s, a, status="failed", error=str(exc), current_agent=None)
        raise

    with Session(engine) as s:
        a = s.get(AuditRun, audit_id)
        if a:
            updates: dict = {}
            for key in AGENT_SEQUENCE:
                if key in audit_data["results"]:
                    updates[key] = json.dumps(audit_data["results"][key])
            _db_set(
                s, a,
                **updates,
                status="complete",
                current_agent=None,
                progress_pct=100,
                one_thing=audit_data.get("one_thing", ""),
                roadmap_json=(
                    json.dumps(audit_data["roadmap"]) if audit_data.get("roadmap") else None
                ),
                analyst_brief_json=(
                    json.dumps(audit_data["analyst_brief"]) if audit_data.get("analyst_brief") else None
                ),
                cross_findings_json=(
                    json.dumps(audit_data["cross_findings"]) if audit_data.get("cross_findings") else None
                ),
                agentic_meta_json=(
                    json.dumps(audit_data["agentic_meta"]) if audit_data.get("agentic_meta") else None
                ),
            )

    print(f"  Audit #{audit_id} complete.", flush=True)
    if audit_data.get("one_thing"):
        print(f"  One thing: {audit_data['one_thing']}", flush=True)
