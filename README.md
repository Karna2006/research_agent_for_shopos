# Research Agent

> AI-powered competitive intelligence for ecommerce brands — built as a real agent, not a pipeline with a system prompt.

![Python](https://img.shields.io/badge/Python-3.11+-blue?style=flat-square&logo=python)
![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green?style=flat-square&logo=fastapi)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?style=flat-square&logo=docker)
![LLM](https://img.shields.io/badge/LLM-Groq%20%2F%20Gemini%20%2F%20llama--3.3--70b-orange?style=flat-square)

Runs an 8-agent audit in ~3 minutes. Produces a full brand report — content gaps, ad intelligence, SEO, CRO, competitive forecast — with a transparent reasoning trace showing every decision the system made.

**Try it instantly:** Clone → add Groq key → `docker-compose up`. Hit ⚡ Rare Rabbit for a pre-cached demo with no API calls needed.

---

## What makes it actually agentic

Most "agents" are for loops with a long system prompt. Three things separate this one:

**State** — Every agent writes findings and confidence signals to a shared `WorkingMemory` object. The next agent reads only what it needs. When a step fails, the graph retries from the last checkpoint — not from the beginning.

**Conditional routing** — A `ReasoningBrain` reads the accumulated state after each agent and decides what runs next. The routing is backed by a dynamic `_TaskQueue` that supports skip, reorder, and pop_batch operations — decisions driven by what actually came back, not by a fixed plan.

**Self-evaluation** — A cross-synthesis step runs every 3 agents. Weak or conflicting signals are flagged explicitly before they reach the next step. After all agents complete, a `_cross_validate_findings()` pass runs across every agent's output to catch contradictions and surface hidden patterns. Garbage doesn't propagate downstream silently.

These aren't architectural opinions. They're what made the output trustworthy.

---

## Quick Start (Docker — recommended)

```bash
git clone <repo-url> && cd shopos-agent
cp .env.example .env
# Edit .env — add your GROQ_API_KEY (free at console.groq.com)
docker-compose up --build
```

Open **http://localhost:8000** — first build takes ~3 min (downloads Playwright + Chromium).

---

## Quick Start (Python only)

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # add GROQ_API_KEY
python main.py
```

Open **http://localhost:8000**

---

## With Mastra (enhanced memory + workflows)

Mastra adds persistent brand memory and TypeScript workflow orchestration. Completely optional — the app works identically without it.

```bash
# Terminal 1 — Mastra orchestration layer
cd mastra && npm install && npm run dev
# Starts Mastra at http://localhost:4111

# Terminal 2 — Python API
MASTRA_URL=http://localhost:4111 python main.py
```

Or with Docker (starts both services automatically):

```bash
docker-compose up --build
```

When `MASTRA_URL` is not set, Python's own orchestrator handles all 8 agents directly.

---

## Run tests

```bash
pip install -r requirements-dev.txt

make test          # all non-slow tests
make test-fast     # stop on first failure
make coverage      # with coverage report
```

Individual groups:

```bash
make test-api        # FastAPI endpoint tests
make test-agents     # Agent unit tests
make test-reports    # Report generator tests
make test-cache      # Cache layer tests
make test-scrapers   # Scraper helper tests (no Playwright)
```

---

## Free services to configure

All services are **optional** — the app degrades gracefully without them.

| Service | What it provides | Where to get it |
|---|---|---|
| **Groq** | LLM inference (required for live audits) | [console.groq.com](https://console.groq.com) — free, 14k req/day |
| **Neon** | PostgreSQL (optional — uses SQLite otherwise) | [neon.tech](https://neon.tech) — free 0.5 GB |
| **Upstash Redis** | Persistent cache (optional — uses in-memory) | [upstash.com](https://upstash.com) — free 10k req/day |
| **Google PageSpeed** | API key improves rate limits | [developers.google.com/speed](https://developers.google.com/speed/docs/insights/v5/get-started) |
| **NVIDIA NIM** | MiniMax VL-01 vision + 40 other free models | [build.nvidia.com](https://build.nvidia.com) — free during promo |
| **Google Gemini** | LLM fallback tier 3 | [aistudio.google.com](https://aistudio.google.com) — free tier with daily quota |

---

## Environment variables

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | Yes (for live audits) | Groq API key |
| `GEMINI_API_KEY` | No | Google Gemini 2.0 Flash — third-tier LLM fallback when both Groq tiers exhausted |
| `NVIDIA_API_KEY` | No | NVIDIA NIM API key — enables MiniMax VL-01 vision (free via build.nvidia.com) |
| `TRACXN_API_KEY` | No | Tracxn API key — enables startup funding stage data in Research agent |
| `DATABASE_URL` | No | Neon PostgreSQL URL — blank uses SQLite |
| `UPSTASH_REDIS_URL` | No | Upstash Redis REST URL |
| `UPSTASH_REDIS_TOKEN` | No | Upstash Redis token |
| `MASTRA_URL` | No | Mastra service URL (e.g. `http://localhost:4111`) |
| `INTERNAL_SECRET_KEY` | No | Shared secret for Mastra ↔ Python calls |
| `PAGESPEED_API_KEY` | No | Google PageSpeed API key |
| `TRIBE_CHECKPOINT_DIR` | No | Path to TRIBE v2 checkpoint dir (enables Deep Visual analysis) |

---

## Architecture

```
Browser → FastAPI (Python)
               ├── Agentic Orchestrator (ReAct loop)
               │        ├── ReasoningBrain     ← LLM-driven planner
               │        ├── WorkingMemory      ← signal accumulator
               │        └── Dynamic TaskQueue  ← skip / reorder / deepen
               ├── Phase 1: Brand Basics (sequential)
               ├── Phase 2: 5 agents parallel (gated at 2 concurrent)
               │        ├── Content & Catalog
               │        ├── Performance & Ads
               │        ├── GEO Visibility
               │        ├── Store & CRO
               │        └── Competitive Research
               ├── Phase 3: Social Profile → Social Media Audit (sequential)
               ├── Scraping: Playwright + Scrapling + DuckDuckGo
               ├── LLM: Groq (llama-3.3-70b → llama-3.1-8b) → Gemini 2.0 Flash
               ├── Vision: MiniMax VL-01 (NVIDIA NIM) → Llama 4 Scout
               ├── DB: SQLite / Neon PostgreSQL
               ├── Cache: in-memory / Upstash Redis
               ├── Forecasting: Chronos → Prophet → numpy
               ├── Data: Wayback Machine CDX + Google Trends + Tracxn (optional)
               ├── Neural video: Meta TRIBE v2 fMRI (optional)
               └── Mastra (optional TypeScript layer)
```

---

## Agentic Reasoning Layer

The audit runs on a **ReAct loop** (Reason → Act → Observe → Synthesize), not a fixed sequential pipeline.

```
PLAN:  ReasoningBrain.initial_plan()
         → opening hypothesis, priority agents, predicted issues

LOOP (per agent):
  REASON  brain.plan_next()    — continue / skip / reorder?
  ACT     agent.run()          — execute the chosen agent
  OBSERVE brain.observe()      — extract cross-cutting signals
  UPDATE  memory.add_finding() — accumulate compressed findings
  (every 3 agents) brain.cross_synthesize() — detect cross-agent patterns

SYNTHESIZE:
  brain.final_synthesis()      — meta-narrative + root cause + hidden opportunity
  _cross_validate_findings()   — post-run cross-agent contradiction and pattern check
  one_thing + 30-day roadmap   — action plans grounded in actual findings
```

### Adaptive behaviours

| Trigger | Brain Action |
|---|---|
| `social_profile` finds zero followers | **Skips** `social_media_audit` automatically |
| `social_profile` finds >100K followers | **Reorders** — runs deep social audit immediately |
| `store_cro` mobile score < 40 | Flags `conversion_crisis` pattern, raises critical signal |
| `geo_visibility` score < 30 | Flags `ai_search_gap` pattern, critical signal |
| 3+ agents return hard errors | **Stops early** — returns diagnostic report |
| No active ads detected | Flags brand as organic-only, adjusts roadmap weighting |

### Detected patterns

The brain identifies cross-agent patterns no single agent can see:

- **`invisible_brand`** — poor SEO + no social + no ads = completely undiscoverable
- **`ghost_advertiser`** — running ads but terrible landing page = burning money
- **`social_darling`** — 100K+ followers but poor store/CRO = losing converts
- **`great_product_bad_store`** — strong brand signal but weak CRO + content
- **`ai_search_gap`** — decent SEO but missing schema for AI engine citations
- **`conversion_crisis`** — 3+ weak scores converging on low conversion probability
- **`hidden_gem`** — strong fundamentals, poor discoverability = quick wins available

### Key files

| File | Role |
|---|---|
| `agents/working_memory.py` | Accumulates findings, signals, reasoning trace across the loop |
| `agents/reasoning_brain.py` | LLM-driven planner: 5 cognitive phases with tuned prompts |
| `agents/agentic_orchestrator.py` | ReAct loop, dynamic task queue, adaptive routing |
| `scrapers/wayback.py` | Domain longevity via Wayback Machine CDX API (free) |
| `scrapers/trends.py` | Google Trends relative interest via pytrends (free) |
| `agents/tracxn_researcher.py` | Funding stage via Tracxn REST API (optional paid key) |

`agents/orchestrator.py` is a thin wrapper that delegates to `agentic_orchestrator`.

### Audit output

Every audit returns:

```json
{
  "agentic_meta": {
    "core_challenge": "...",
    "root_cause": "...",
    "hidden_opportunity": "...",
    "posture": "optimize | triage | accelerate | defend",
    "pattern": "ai_search_gap | invisible_brand | ..."
  },
  "signals": [
    { "source": "geo_visibility", "type": "risk", "severity": "critical",
      "content": "...", "evidence": "geo_score=41", "triggers_action": "flag:ai_search_gap" }
  ],
  "cross_insights": ["..."],
  "reasoning_trace": ["[0.0s] Opening hypothesis: ...", "..."],
  "decisions": [
    { "step": "initial_plan", "at_seconds": 0.1, "rationale": "...", "action": "..." }
  ],
  "skipped_agents": ["social_media_audit"],
  "pattern_detected": "ai_search_gap",
  "investigation_posture": "growth_audit"
}
```

The HTML report includes an **Agentic Reasoning Brain** panel — signal severity colour-coding, cross-insight highlights, and the full reasoning trace.

---

## 8 Specialist Agents

Dynamically ordered by the brain at runtime:

1. **Brand Basics** — founding story, positioning, platform detection, Wayback Machine domain longevity
2. **Content & Catalog** — PDP quality, copy rewrites, headline scoring
3. **Performance & Ads** — Meta Ads Library scrape, ad format breakdown
4. **GEO & AI Visibility** — schema.org audit, AI citation likelihood
5. **Store & CRO** — PageSpeed, conversion signals, Shopify app recs
6. **Competitive Research** — rivals, market gaps, trend forecast, Google Trends integration (relative interest, trend direction), optional Tracxn funding signals
7. **Social & Brand Presence** — Instagram profile metrics, follower count, post analysis
8. **Social Media Deep Audit** — engagement scoring, content quality, Reels virality, optional TRIBE v2 neural heatmaps

Report sections appear live as each agent completes — no waiting for the full pipeline.

### Deep Visual Analysis (optional)

Enable the checkbox before starting an audit to activate Agent 8's TRIBE v2 Reels processing. Downloads Instagram Reels via yt-dlp, runs fMRI inference, and generates per-reel brain activation heatmaps across the 7 Yeo functional networks. Requires `TRIBE_CHECKPOINT_DIR` (or HuggingFace `facebook/tribev2` cached). Extends audit time by ~20 min per reel (CPU).

### Forecasting stack

Chronos (Amazon) → Prophet (Meta) → numpy polynomial regression. Labelled in the report based on which model ran.

---

## System health

```bash
curl http://localhost:8000/health   # → {"status": "ok", "version": "..."}
curl http://localhost:8000/status   # → full service status
```

```json
{
  "api": "ok",
  "database": "sqlite",
  "cache": "in-memory",
  "mastra": "not configured",
  "groq": "ok",
  "gemini": "ok (fallback ready)",
  "playwright": "ok",
  "tribe_v2": "loaded"
}
```

---

## Brain Activation Heatmap

Visit **http://localhost:8000/brain-map** for the neural brain activation heatmap demo — side-by-side comparison of real TRIBE v2 fMRI inference vs estimated virality scores across the 7 Yeo brain networks.

Also embedded directly in audit reports when Deep Visual Analysis is enabled.

---

## Deploy to Fly.io

```bash
curl -L https://fly.io/install.sh | sh
fly auth login
fly launch --copy-config --dockerfile Dockerfile
fly secrets set GROQ_API_KEY=your_key_here
fly volumes create shopos_data --size 1 --region sin
fly deploy
```

Config: `deploy/fly.toml`

## Deploy to Render

1. Push repo to GitHub
2. Render → **New → Blueprint** → connect repo (auto-detects `deploy/render.yaml`)
3. Set `GROQ_API_KEY` in dashboard
4. Deploy

---

## Demo

Click **⚡ Rare Rabbit** in the Brand Audit tab — instant pre-cached report, no API calls, no key needed.
