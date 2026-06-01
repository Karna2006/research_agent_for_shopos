#!/usr/bin/env python3
"""
CLI virality predictor — runs ViralityPredictor directly, no FastAPI.

Usage:
    python run_virality.py --url https://rarerabbit.in/products/some-product
    python run_virality.py --name "Rare Rabbit Shirt" --desc "Premium cotton poplin..."
    python run_virality.py --url https://... --name "Product" --desc "..." --category fashion
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import box

console = Console()

OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

_DIM_LABELS = {
    "emotional_trigger":      "Emotional Trigger",
    "visual_stopping_power":  "Visual Stopping Power",
    "transformation_clarity": "Transformation Clarity",
    "social_currency":        "Social Currency",
    "trend_alignment":        "Trend Alignment",
    "share_trigger":          "Share Trigger",
    "hook_strength":          "Hook Strength",
}

_GRADE_COLORS = {
    "S": "bright_yellow",
    "A": "green",
    "B": "blue",
    "C": "yellow",
    "D": "red",
}

_BAR_COLORS = {
    "S": "bright_yellow",
    "A": "green3",
    "B": "steel_blue1",
    "C": "yellow3",
    "D": "red3",
}


def _grade_char(grade: str) -> str:
    return (grade or "D")[0].upper()


def _score_bar(score: float | int, width: int = 20) -> str:
    """Render a text progress bar for a 0-10 score."""
    filled = round((score / 10) * width)
    return "█" * filled + "░" * (width - filled)


def _print_virality_card(result: dict) -> None:
    score   = result.get("score") or 0
    grade   = result.get("grade") or "D"
    g_char  = _grade_char(grade)
    color   = _GRADE_COLORS.get(g_char, "white")
    bcolor  = _BAR_COLORS.get(g_char, "white")
    analysis = result.get("analysis") or result
    name    = result.get("product_name") or analysis.get("product_name") or "Product"

    # ── Score header ──────────────────────────────────────────────────
    header = Text(justify="center")
    header.append(f"\n  {name}\n\n", style="bold")
    header.append(f"  {score}", style=f"bold {color} on default")
    header.append(" / 100\n", style="dim")
    header.append(f"  {grade}\n", style=f"bold {color}")
    console.print(Panel(header, title="[bold]Virality Score[/]", border_style=color, padding=(0, 2)))

    # ── Dimension bars ────────────────────────────────────────────────
    dims = analysis.get("dimensions") or {}
    table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    table.add_column("Dimension", style="bold", min_width=26)
    table.add_column("Bar", no_wrap=True)
    table.add_column("Score", justify="right", min_width=5)

    for key, label in _DIM_LABELS.items():
        raw   = dims.get(key, {})
        s     = raw.get("score", 0) if isinstance(raw, dict) else (raw or 0)
        bar   = _score_bar(s)
        rsn   = raw.get("reasoning", "") if isinstance(raw, dict) else ""
        table.add_row(
            label,
            f"[{bcolor}]{bar}[/]",
            f"[bold {color}]{s}/10[/]",
        )
        if rsn:
            table.add_row("", f"[dim]  {rsn[:70]}[/]", "")

    console.print(table)

    # ── Killer hook ───────────────────────────────────────────────────
    hook = analysis.get("killer_hook", "")
    if hook:
        console.print(Panel(
            f'[bold yellow]"{hook}"[/]',
            title="[bold]✦ Killer Hook[/]",
            border_style="dark_goldenrod",
        ))

    # ── Viral angles ──────────────────────────────────────────────────
    angles = analysis.get("viral_content_angles") or []
    if angles:
        console.print("\n  [bold]Viral Content Angles[/]")
        for i, angle in enumerate(angles, 1):
            console.print(f"  [{bcolor}]{i}.[/] {angle}")

    # ── Best platforms ────────────────────────────────────────────────
    platforms = analysis.get("best_platforms") or []
    if platforms:
        console.print(f"\n  [bold]Best Platforms:[/] [dim]{' · '.join(platforms)}[/]")

    # ── Risk factors ──────────────────────────────────────────────────
    risks = analysis.get("risk_factors") or []
    if risks:
        console.print("\n  [bold]Risk Factors[/]")
        for r in risks:
            console.print(f"  [red]⚠[/]  {r}")

    console.print()


async def run_virality(
    url: str | None,
    product_name: str | None,
    description: str | None,
    category: str | None,
) -> dict:
    from agents.virality import ViralityPredictor
    from llm.client import get_client
    from scrapers.web_scraper import WebScraper
    from scrapers.search import SearchAgent

    if not url and not product_name:
        console.print("[red]Error:[/] provide --url and/or --name")
        sys.exit(1)

    display = product_name or url or "product"
    console.rule(f"[bold gold1]ShopOS Virality Predictor[/]")
    console.print(f"  Scoring: [bold]{display}[/]")
    console.print()

    llm     = get_client()
    scraper = WebScraper()
    search  = SearchAgent()
    predictor = ViralityPredictor(llm, scraper, search)

    with Progress(
        SpinnerColumn(spinner_name="dots"),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Analysing virality potential…", total=None)
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(
                predictor.predict(
                    url=url,
                    product_name=product_name,
                    description=description,
                    category=category,
                ),
                timeout=120,
            )
        except asyncio.TimeoutError:
            console.print("[red]Timed out after 120s[/]")
            sys.exit(1)
        elapsed = time.monotonic() - t0
        progress.stop_task(task)

    console.print(f"  [green]✓[/] Completed in {elapsed:.1f}s\n")
    _print_virality_card(result)

    # Save JSON
    slug = (product_name or "product").lower().replace(" ", "_")[:30]
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"virality_{slug}_{ts}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"  [dim]JSON saved →[/] {out_path}\n")

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="ShopOS — score a product's virality potential")
    parser.add_argument("--url",      help="Product page URL (optional — scrapes it for context)")
    parser.add_argument("--name",     help="Product name")
    parser.add_argument("--desc",     help="Product description")
    parser.add_argument("--category", help="Product category (e.g. skincare, fashion)")
    args = parser.parse_args()

    if not args.url and not args.name:
        parser.error("Provide at least --url or --name")

    try:
        asyncio.run(run_virality(
            url=args.url,
            product_name=args.name,
            description=args.desc,
            category=args.category,
        ))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/]")
        sys.exit(0)


if __name__ == "__main__":
    main()
