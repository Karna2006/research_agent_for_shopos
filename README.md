# Research Agent

AI-powered competitive intelligence for ecommerce brands. Runs a 6-agent pipeline across scraping, LLM analysis, and time-series forecasting to produce a full brand audit in ~90 seconds.

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

When `MASTRA_URL` is not set, Python's own orchestrator handles all 6 agents directly.

---

## Run tests

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run all non-slow tests (no Playwright required)
make test

# Fast mode — stop on first failure
make test-fast

# With coverage report
make coverage
```

Individual test groups:

```bash
make test-api        # FastAPI endpoint tests only
make test-agents     # Agent unit tests only
make test-reports    # Report generator tests only
make test-cache      # Cache layer tests only
make test-scrapers   # Scraper helper tests (no Playwright)
```

---

## Free services to configure

All services are **optional** — the app works without them and degrades gracefully.

| Service | What it provides | Where to get it |
|---|---|---|
| **Groq** | LLM inference (required for live audits) | [console.groq.com](https://console.groq.com) — free, 14k req/day |
| **Neon** | PostgreSQL (optional — uses SQLite otherwise) | [neon.tech](https://neon.tech) — free 0.5 GB |
| **Upstash Redis** | Persistent cache (optional — uses in-memory) | [upstash.com](https://upstash.com) — free 10k req/day |
| **Google PageSpeed** | API key improves rate limits | [developers.google.com/speed](https://developers.google.com/speed/docs/insights/v5/get-started) |

---

## Environment variables

Copy `.env.example` and fill in what you need:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | Yes (for live audits) | Groq API key |
| `DATABASE_URL` | No | Neon PostgreSQL URL — blank uses SQLite |
| `UPSTASH_REDIS_URL` | No | Upstash Redis REST URL |
| `UPSTASH_REDIS_TOKEN` | No | Upstash Redis token |
| `MASTRA_URL` | No | Mastra service URL (e.g. `http://localhost:4111`) |
| `INTERNAL_SECRET_KEY` | No | Shared secret for Mastra ↔ Python calls |
| `PAGESPEED_API_KEY` | No | Google PageSpeed API key |

---

## System health

```bash
curl http://localhost:8000/health   # → {"status": "ok", "version": "..."}
curl http://localhost:8000/status   # → full service status
```

`/status` returns:

```json
{
  "api": "ok",
  "database": "sqlite",
  "cache": "in-memory",
  "mastra": "not configured",
  "groq": "ok",
  "playwright": "ok",
  "tribe_v2": "loaded"
}
```

The status bar is also shown at the bottom of every page in the UI.

---

## Architecture

```
Browser → FastAPI (Python)
               ├── Scraping: Playwright + DuckDuckGo
               ├── LLM: Groq (llama-3.3-70b)
               ├── DB: SQLite / Neon PostgreSQL
               ├── Cache: in-memory / Upstash Redis
               ├── Forecasting: Chronos → Prophet → numpy
               └── Mastra (optional TypeScript layer)
                        ├── Workflow orchestration
                        └── Brand memory (LibSQL)
```

**6 agents run in sequence:**

1. **Brand Basics** — founding story, positioning, platform detection
2. **Content & Catalog** — PDP quality, copy rewrites, headline scoring
3. **Performance & Ads** — Meta Ads Library scrape, ad format breakdown
4. **GEO & AI Visibility** — schema.org audit, AI citation likelihood
5. **Store & CRO** — PageSpeed, conversion signals, Shopify app recs
6. **Competitive Research** — rivals, market gaps, trend forecast

**Forecasting stack (each falls back to the next):**
- Chronos (Amazon, foundation model) → Prophet (Meta) → numpy polynomial regression
- When Chronos is loaded: labelled "AI-Powered Forecast (Chronos)"
- When only numpy: labelled "Statistical Projection"

---

## Deploy to Fly.io (free tier)

```bash
curl -L https://fly.io/install.sh | sh
fly auth login
fly launch --copy-config --dockerfile Dockerfile
fly secrets set GROQ_API_KEY=your_key_here
fly volumes create shopos_data --size 1 --region sin
fly deploy
```

Config: `deploy/fly.toml`

## Deploy to Render (easier)

1. Push repo to GitHub
2. Render → **New → Blueprint** → connect repo (auto-detects `deploy/render.yaml`)
3. Set `GROQ_API_KEY` in dashboard
4. Deploy

---

## Demo

Click **⚡ Rare Rabbit** in the Brand Audit tab — instant pre-cached report, no API calls, no key needed.
