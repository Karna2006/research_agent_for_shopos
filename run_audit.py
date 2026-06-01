#!/usr/bin/env python3
"""
CLI audit runner — no FastAPI, no HTTP, runs all 6 agents directly.

Usage:
    python run_audit.py --url https://rarerabbit.in
    python run_audit.py --url https://rarerabbit.in --save-html
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# Load .env before any other import that reads env vars
from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text
from rich import box

console = Console()

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

AGENT_LABELS = {
    "brand_basics":    "Brand Basics",
    "content_catalog": "Content Audit",
    "performance_ads": "Ad Intelligence",
    "geo_visibility":  "GEO Visibility",
    "store_cro":       "Store & CRO",
    "research":        "Competitive Research",
}


# ── Playwright / httpx fallback scraper ───────────────────────────────────────

def _make_robust_scraper():
    """Return a WebScraper that falls back to httpx+BS4 if Playwright fails."""
    from scrapers.web_scraper import WebScraper
    import scrapers.web_scraper as _ws_mod

    original_scrape_page = WebScraper.scrape_page

    async def _robust_scrape_page(self, url: str) -> dict:
        try:
            result = await original_scrape_page(self, url)
            # If Playwright returned an error (not a block), try httpx fallback
            if result.get("error") and not result.get("blocked"):
                raise RuntimeError(result["error"])
            return result
        except Exception as exc:
            console.print(f"  [yellow]⚠ Playwright failed ({exc.__class__.__name__}), using httpx fallback[/]")
            return await _httpx_scrape(url)

    original_scrape_pdp = WebScraper.scrape_pdp

    async def _robust_scrape_pdp(self, url: str) -> dict:
        try:
            result = await original_scrape_pdp(self, url)
            if result.get("error") and not result.get("blocked"):
                raise RuntimeError(result["error"])
            return result
        except Exception as exc:
            console.print(f"  [yellow]⚠ Playwright PDP failed ({exc.__class__.__name__}), using httpx fallback[/]")
            fallback = await _httpx_scrape(url)
            # Remap generic page fields to PDP fields
            return {
                "url": url, "blocked": False,
                "product_name": fallback.get("title", ""),
                "price": "", "description": fallback.get("body_text", "")[:1000],
                "images": fallback.get("images", []),
                "reviews_count": "", "rating": "", "in_stock": None, "cta_text": "",
            }

    WebScraper.scrape_page = _robust_scrape_page
    WebScraper.scrape_pdp  = _robust_scrape_pdp
    return WebScraper()


async def _httpx_scrape(url: str) -> dict:
    """Lightweight httpx + BeautifulSoup fallback when Playwright fails."""
    import httpx
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return {
            "url": url, "blocked": False, "error": "bs4 not installed",
            "title": "", "meta_description": "", "body_text": "",
            "headings": [], "images": [], "links": [], "schema_json_ld": [],
        }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=20) as client:
            resp = await client.get(url)
        soup = BeautifulSoup(resp.text, "html.parser")
        title = soup.title.string.strip() if soup.title else ""
        meta_el = soup.find("meta", attrs={"name": "description"})
        meta_desc = meta_el.get("content", "").strip() if meta_el else ""
        headings = [el.get_text(strip=True) for el in soup.find_all(["h1", "h2", "h3"])[:20]]
        images = [img.get("src", "") for img in soup.find_all("img", src=True)[:30]]
        links = [a.get("href", "") for a in soup.find_all("a", href=True)[:60]]
        body = soup.get_text(" ", strip=True)[:8000]
        return {
            "url": url, "blocked": False, "title": title,
            "meta_description": meta_desc, "body_text": body,
            "headings": headings, "images": images, "links": links,
            "schema_json_ld": [], "page_html": resp.text[:30000],
        }
    except Exception as exc:
        return {
            "url": url, "blocked": False, "error": str(exc),
            "title": "", "meta_description": "", "body_text": "",
            "headings": [], "images": [], "links": [], "schema_json_ld": [],
        }


# ── Graceful agent runner ──────────────────────────────────────────────────────

async def _run_agent_safe(agent, url: str, brand_name: str, label: str) -> tuple[dict, float, str | None]:
    """Run one agent with a 120s timeout. Never raises — returns (result, elapsed, error)."""
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(agent.run(url, brand_name), timeout=120)
        elapsed = time.monotonic() - t0
        err = result.get("error")
        return result, elapsed, err
    except asyncio.TimeoutError:
        elapsed = time.monotonic() - t0
        err = f"Timed out after {elapsed:.0f}s"
        return {"agent": label, "error": err, "partial": True}, elapsed, err
    except Exception as exc:
        elapsed = time.monotonic() - t0
        return {"agent": label, "error": str(exc), "partial": True}, elapsed, str(exc)


# ── Rich summary table ─────────────────────────────────────────────────────────

def _print_summary(results: dict, total_time: float) -> None:
    table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold gold1")
    table.add_column("Agent", style="bold", min_width=20)
    table.add_column("Status", min_width=10)
    table.add_column("Key output", max_width=50)

    for key, label in AGENT_LABELS.items():
        r = results.get(key, {})
        if not r:
            table.add_row(label, "[dim]skipped[/]", "")
            continue

        err = r.get("error")
        partial = r.get("partial", False)

        if err and partial:
            status = "[red]error[/]"
            detail = err[:60]
        elif err:
            status = "[yellow]partial[/]"
            detail = err[:60]
        else:
            status = "[green]done[/]"
            # Pull 1 interesting value per agent
            a = r.get("analysis") or {}
            if key == "brand_basics":
                detail = f"brand={a.get('brand_name','')} | platform={r.get('platform','')}"
            elif key == "content_catalog":
                detail = f"pdp_score={a.get('pdp_quality_score','')} | pdps={r.get('pdps_scraped','')}"
            elif key == "performance_ads":
                detail = f"ads={r.get('ads_scrape',{}).get('ads_count','?')}"
            elif key == "geo_visibility":
                detail = f"geo_score={a.get('geo_score','')} | ai_vis={r.get('ai_simulation_visibility_pct','')}%"
            elif key == "store_cro":
                ps = r.get("pagespeed", {})
                detail = f"mobile={ps.get('mobile_score','?')} desktop={ps.get('desktop_score','?')}"
            elif key == "research":
                comps = a.get("top_competitors", [])
                names = [c.get("name", "") for c in comps[:3]]
                detail = f"competitors: {', '.join(names)}"
            else:
                detail = ""

        table.add_row(label, status, detail)

    console.print()
    console.print(table)
    console.print(f"  [dim]Total time: {total_time:.1f}s[/]")


# ── Main audit loop ────────────────────────────────────────────────────────────

async def run_audit(url: str, save_html: bool = False) -> dict:
    from llm.client import get_client
    from scrapers.search import SearchAgent
    from agents.brand_basics    import BrandBasicsAgent
    from agents.content_catalog import ContentCatalogAgent
    from agents.performance_ads import PerformanceAdsAgent
    from agents.geo_visibility  import GEOVisibilityAgent
    from agents.store_cro       import StoreCROAgent
    from agents.research        import ResearchAgent

    # Brand name derived from domain
    parsed = urlparse(url)
    netloc = parsed.netloc.lstrip("www.")
    brand_name = " ".join(w.capitalize() for w in netloc.split(".")[0].replace("-", " ").split())

    console.rule(f"[bold gold1]ShopOS Audit[/] — {url}")
    console.print(f"  Brand: [bold]{brand_name}[/]")
    console.print()

    llm    = get_client()
    search = SearchAgent()
    scraper = _make_robust_scraper()

    agents = {
        "brand_basics":    BrandBasicsAgent(llm, scraper, search),
        "content_catalog": ContentCatalogAgent(llm, scraper, search),
        "performance_ads": PerformanceAdsAgent(llm, scraper, search),
        "geo_visibility":  GEOVisibilityAgent(llm, scraper, search),
        "store_cro":       StoreCROAgent(llm, scraper, search),
        "research":        ResearchAgent(llm, scraper, search),
    }

    results: dict = {}
    overall_start = time.monotonic()
    agent_status: list[dict] = []

    progress = Progress(
        SpinnerColumn(spinner_name="dots"),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )

    with progress:
        for idx, (key, label) in enumerate(AGENT_LABELS.items(), start=1):
            task = progress.add_task(
                f"[{idx}/6] [bold]{label}[/]…", total=None
            )
            result, elapsed, err = await _run_agent_safe(agents[key], url, brand_name, key)
            results[key] = result

            if err:
                progress.update(task, description=f"[{idx}/6] [bold]{label}[/] [red]✗ {err[:50]}[/]")
                status = "error"
            else:
                progress.update(task, description=f"[{idx}/6] [bold]{label}[/] [green]✓[/] ({elapsed:.1f}s)")
                status = "done"

            progress.stop_task(task)
            agent_status.append({"agent": key, "label": label, "status": status, "elapsed_s": round(elapsed, 2), "error": err})

    total_time = time.monotonic() - overall_start

    # Assemble the full audit dict
    audit_data = {
        "url":               url,
        "brand_name":        brand_name,
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "total_time_seconds": round(total_time, 2),
        "agent_status":      agent_status,
        "results":           results,
    }

    # Summary table
    _print_summary(results, total_time)

    # Save JSON
    slug = brand_name.lower().replace(" ", "_")
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = OUTPUT_DIR / f"audit_{slug}_{ts}.json"
    json_path.write_text(json.dumps(audit_data, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"\n  [dim]JSON saved →[/] {json_path}")

    # Optionally generate and save HTML report
    if save_html:
        try:
            from reports.generator import generate_audit_report, save_report
            html = generate_audit_report(audit_data)
            html_filename = f"audit_{slug}_{ts}.html"
            html_path = save_report(html, html_filename)
            console.print(f"  [dim]HTML saved →[/] {html_path}")
        except Exception as exc:
            console.print(f"  [yellow]⚠ HTML report generation failed: {exc}[/]")

    console.print(f"\n  [bold green]Audit complete.[/] Report saved to [cyan]{json_path}[/]\n")
    return audit_data


def main() -> None:
    parser = argparse.ArgumentParser(description="ShopOS — run a full brand audit from the command line")
    parser.add_argument("--url", required=True, help="Brand homepage URL, e.g. https://rarerabbit.in")
    parser.add_argument("--save-html", action="store_true", help="Also generate and save the HTML report")
    args = parser.parse_args()

    try:
        asyncio.run(run_audit(args.url, save_html=args.save_html))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
        sys.exit(0)


if __name__ == "__main__":
    main()
