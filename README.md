# SHOPOS — Brand Intelligence Agent

An 8-agent AI pipeline that audits any D2C ecommerce brand in ~3 minutes. Paste a URL, get a full competitive intelligence report: ad strategy, SEO gaps, social performance, CRO issues, and a 30-day action roadmap.

**Built for**: Founders and brand teams who want answers, not dashboards.

---

## Quick Demo (< 5 minutes)

```bash
git clone <repo-url>
cd shopos-agent

# Create your env file
cp .env.example .env
# Add your GROQ_API_KEY — free at https://console.groq.com (takes 30 seconds)

# Install dependencies
pip install -r requirements.txt
playwright install chromium   # one-time browser install (~200MB)

# Start
uvicorn main:app --port 8000
```

Open **http://localhost:8000**

Click **⚡ Rare Rabbit** → instant pre-cached report. No API calls, no waiting.

To run a live audit: paste any brand URL and hit **Start Audit**.

---

## What You See

The audit runs live — each section appears as its agent completes. No waiting for the full pipeline.

**8 agents run in parallel where possible:**

| Agent | What It Finds |
|---|---|
| Brand Basics | Founding story, category, domain longevity |
| Content & Catalog | PDP quality, headline copy, benefit-vs-feature ratio |
| Performance Ads | Active Meta ads, creative formats, hook angles |
| GEO & AI Visibility | Schema.org audit, Wikipedia presence, AI citation likelihood |
| Store & CRO | PageSpeed scores, mobile vs desktop, conversion blockers |
| Competitive Research | Top competitors, market positioning, Google Trends, funding data |
| Social Profile | Instagram handle discovery, follower count |
| Social Media Audit | Engagement scoring, Reels analysis, TRIBE v2 neural engagement |

At the bottom: a **Reasoning Brain** panel showing the agent's cross-agent synthesis — what patterns it detected, what the root cause is, and what to fix first.

---

## What Makes It Actually Agentic

Most "AI audit tools" are a for-loop with a long system prompt.

This one has:

**State** — Every agent writes findings to a shared `WorkingMemory`. The next agent reads only what it needs. When a step fails, the graph retries from that checkpoint.

**Conditional routing** — A `ReasoningBrain` reads accumulated signals after each agent and decides what runs next. It can skip agents, reorder them, or deepen analysis based on what came back.

**Self-evaluation** — A cross-synthesis step runs every 3 agents. Weak or conflicting signals are flagged before they reach the next step. After all agents complete, a `_cross_validate_findings()` pass catches contradictions across every output.

**Transparent failures** — When a scraper is blocked (Cloudflare, Instagram rate limit, Meta login wall), the report shows exactly why that section has limited data — not a generic error.

---

## Scraping Stack

Data quality is the whole point. When one method fails, the agent tries the next:

| Target | Method chain |
|---|---|
| Any website | Scrapling StealthyFetcher → Playwright → HTML parser |
| Shopify stores | `/products.json` API (bypasses Cloudflare entirely) |
| Meta Ads Library | Graph API → Scrapling StealthyFetcher → Playwright |
| Instagram profile | Mobile API → Playwright (with network interception) → og:meta tags |
| Competitive intel | DuckDuckGo search → Entrackr → Yourstory |
| Market trends | Google Trends (pytrends) |
| Funding data | Tracxn API (optional) |

---

## Setup

### Required

```bash
# Only one key needed to run live audits
GROQ_API_KEY=gsk_...   # https://console.groq.com — free, 14k req/day
```

### Optional (all improve data quality, none are required)

```bash
# Refresh if you get "Token expired" in Meta Ads section
# Get at: developers.facebook.com → Tools → Graph API Explorer → Generate Token
META_AD_LIBRARY_TOKEN=EAA...

# Google Gemini — third-tier LLM fallback when Groq is exhausted
GEMINI_API_KEY=AIza...   # https://aistudio.google.com

# Startup funding data in the Research section
TRACXN_API_KEY=...

# Improves PageSpeed rate limits (unauthenticated endpoint still works)
PAGESPEED_API_KEY=...

# Meta TRIBE v2 neural engagement (for Deep Visual Analysis)
# Install: cd ../tribeV2 && pip install -e .
TRIBE_CHECKPOINT_DIR=/path/to/tribe/checkpoints
```

### Database

Default: SQLite (`./shopos.db`) — works out of the box, no configuration needed.

Optional PostgreSQL (for production):
```bash
DATABASE_URL=postgresql://user:pass@host/dbname
```

---

## Installing Dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

**Note on heavy packages**: `torch`, `chronos-forecasting`, and `prophet` are ~2GB total. They enable the market forecast section. If you're on a slow connection or want a lighter install:

```bash
# Minimal install (no forecasting, no neural engagement)
pip install fastapi uvicorn sqlmodel aiosqlite httpx playwright scrapling \
    curl_cffi patchright msgspec browserforge beautifulsoup4 ddgs \
    instaloader yt-dlp pytrends vaderSentiment langgraph langchain-core \
    jinja2 python-dotenv python-jose[cryptography] groq google-generativeai \
    rich scipy matplotlib numpy
playwright install chromium
```

The market forecast section will show a graceful fallback message if torch/chronos aren't installed.

---

## Known Data Limitations

These are real scraping constraints, not bugs. The report tells you exactly what's missing and why.

| Situation | What you'll see |
|---|---|
| Meta Ads token expired | "Token invalid — Playwright fallback active. Refresh token at developers.facebook.com" |
| Instagram rate limited | "Instagram API rate limited — post data unavailable. Try again in ~1 hour." |
| Cloudflare-blocked store | "Product pages protected by Cloudflare — scraped homepage only. Product quality scoring estimated." |
| Brand not in Meta Ads | "No active Meta ads found — brand may be organic-only." |
| Brand not appearing in search | "Brand not cited in any AI-simulation search queries for its category." |

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
               ├── DB: SQLite (default) / PostgreSQL
               ├── Forecasting: Chronos → Prophet → numpy
               └── Neural video: Meta TRIBE v2 fMRI (optional)
```

---

## Adaptive Behaviour

The agent doesn't just run all 8 steps blindly:

| Trigger | What the brain does |
|---|---|
| Social profile finds 0 followers | Skips Social Media Audit automatically |
| Social profile finds >100K followers | Promotes deep audit to run immediately |
| Store CRO mobile score < 40 | Flags `conversion_crisis`, raises critical signal |
| GEO score < 30 | Flags `ai_search_gap`, critical signal |
| 3+ agents return hard errors | Stops early, returns diagnostic report |

**Patterns it detects** (no single agent can see these alone):
- `invisible_brand` — poor SEO + no social + no ads = completely undiscoverable
- `ghost_advertiser` — running ads but terrible landing page = burning money  
- `social_darling` — 100K+ followers but poor store/CRO = losing converts
- `ai_search_gap` — decent SEO but missing schema for AI engine citations
- `conversion_crisis` — 3+ weak scores converging on low conversion probability

---

## System Health

```bash
curl http://localhost:8000/health   # → {"status": "ok"}
curl http://localhost:8000/status   # → full service status including LLM, DB, scraper health
```

---

## File Structure

```
agents/          — 8 specialist agents + orchestrator + reasoning brain
scrapers/        — web scraper, search, Instagram, Meta Ads, trends
llm/             — Groq/Gemini client with fallback chain
reports/         — HTML report generator + Jinja2 templates
demo/            — pre-cached demo data (Rare Rabbit, boAt)
main.py          — FastAPI app, SSE streaming, all endpoints
shopos.db        — SQLite database (auto-created on first run)
```

---

## Docker (optional)

```bash
cp .env.example .env  # add GROQ_API_KEY
docker-compose up --build
```

First build takes ~5 min (downloads Chromium + model weights).

---

## Virality Predictor

Separate tab in the UI. Scores any product for viral potential across 7 dimensions using Meta TRIBE v2 fMRI encoding model.

Powered by: engagement prediction, killer hook generation, platform fit, creator archetype matching, comparable viral products.

Access at **http://localhost:8000** → Virality tab, or directly at `/virality/{run_id}`.
