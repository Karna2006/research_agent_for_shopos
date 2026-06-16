# Build Log — Competitive Research Agent
**Project start:** May 31, 2026 | **Demo ready:** June 1, 2026

---

## Phase 0 — Specification Analysis (May 31, AM)

Reviewed three specification documents:

1. **Brand Audit Slash Command** — 6 sequential audit agents producing a 13-page shareable report. Trigger: `/audit [url]`. Output: HTML/PDF artifact. Includes sample Hoka audit as ground truth.

2. **Competitor Analysis Agent** — Continuous competitive monitoring: pricing, review velocity, SKU changes, sentiment. Weekly intelligence brief format.

3. **Competitive Intelligence Scraping** — Data collection methodology: PDP scraping, review tracking, pricing normalisation, meaningful change detection.

**Key decisions made at this stage:**
- Build a single unified system covering all three documents rather than three separate tools
- Add a virality predictor (inspired by Higgsfield's feature) as a bonus capability
- Zero budget constraint → all free tools only

---

## Phase 1 — Project Scaffold (May 31, 09:00–10:00)

**Built:** Full project directory structure

```
shopos-agent/
  main.py          # FastAPI application
  agents/          # 6 audit agents + orchestrator + virality
  scrapers/        # Playwright, search, PageSpeed, Meta Ads
  llm/             # Groq client + all LLM prompts
  reports/         # HTML report generator + templates
  db/              # SQLModel models + database init
  demo/            # Pre-cached demo data
  output/          # Audit JSON outputs
```

**Stack finalised:**
- Backend: Python 3.11 + FastAPI + uvicorn
- Scraping: Playwright (Chromium) + Crawl4AI fallback
- LLM: Groq API (Llama 3.1 70B, free tier)
- Search: duckduckgo-search (no API key)
- Storage: SQLite via SQLModel
- Caching: In-memory TTLCache (Upstash Redis for production)
- Frontend: Inline HTML/CSS/JS served by FastAPI

**`requirements.txt` created** with 20+ dependencies.

---

## Phase 2 — Scraping Layer (May 31, 10:00–11:30)

**Built:**

`scrapers/web_scraper.py` — `WebScraper` class
- `scrape_page(url)` → title, meta, body text, headings, images, links, JSON-LD schema
- `scrape_pdp(url)` → product name, price, description, images, reviews, rating, in-stock, CTA
- `detect_platform(url)` → "shopify" | "woocommerce" | "custom"
- Cloudflare detection via response status + body inspection
- 30-second timeout per page
- Returns `DataResult` wrapper with confidence tagging

`scrapers/search.py` — `SearchAgent` class
- `search(query, max_results=5)` → structured results
- `search_news(query, days=90)` → recent brand news
- `find_competitors(brand_name, category)` → competitor discovery

`scrapers/pagespeed.py` — `PageSpeedScraper` class
- Calls free PageSpeed Insights API (no key required)
- Returns mobile + desktop scores, Core Web Vitals (LCP, CLS, INP)
- 10-second timeout → graceful N/A return with manual check link

`scrapers/meta_ads.py` — `MetaAdsScraper` class
- Playwright scrape of Meta Ad Library public URL
- Returns: ads_count, creative format breakdown, sample headlines
- Handles 4 distinct edge cases (not found, blocked, 0 active, ambiguous name)

`scrapers/result.py` — `DataResult` dataclass
- Wraps every scraper output with: source, source_url, confidence, error, fallback_used, timestamp, manual_check_url

---

## Phase 3 — LLM Client + Prompts (May 31, 11:30–12:30)

**Built:**

`llm/client.py` — `GroqClient` class
- Model: `llama-3.1-70b-versatile`
- `analyze()` → returns string
- `analyze_structured()` → returns parsed JSON dict
- Retry logic: 3 retries, 10s/20s/40s backoff on 429
- JSON parsing: `json.loads()` → regex fallback → single retry with explicit instruction
- Token estimation to prevent context overflow

`llm/prompts.py` — 7 system prompts
- BRAND_BASICS_PROMPT
- CONTENT_AUDIT_PROMPT
- AD_AUDIT_PROMPT
- GEO_VISIBILITY_PROMPT
- STORE_CRO_PROMPT
- COMPETITIVE_RESEARCH_PROMPT
- VIRALITY_PROMPT

Each prompt includes:
- English-only output instruction (prevents regional language output)
- Explicit scoring rubric (prevents score inflation)
- "Not found publicly" instruction (prevents hallucination)

---

## Phase 4 — 6 Audit Agents (May 31, 12:30–15:00)

**Built:** One Python class per agent in `agents/`

Each agent follows the pattern:
```python
class XAgent:
    def __init__(self, llm_client, scraper, search_agent): ...
    async def run(self, url: str, brand_name: str, 
                  prefetched: dict = None, context: dict = None) -> dict: ...
```

**Agent execution — initial version:** Fully sequential.
All 6 agents ran one after another → ~5-6 minutes total.

`agents/orchestrator.py` created with:
- `run_full_audit(url)` → standalone (no DB)
- `run_all(audit_id)` → DB-backed (used by FastAPI background tasks)
- Progress logging: `[1/6] Brand Basics... done (3.2s)`

`agents/virality.py` — `ViralityPredictor`
- 7-dimension scoring model with weighted composite score
- Chronos trend prediction for virality trajectory
- Fallback: Prophet → numpy linear regression

---

## Phase 5 — FastAPI + Web UI (May 31, 15:00–17:00)

**Built:** Complete FastAPI application in `main.py`

**Endpoints registered:**
- `GET /` — web UI
- `GET /health`, `GET /status`
- `POST /audit` → queues background task, returns `audit_id`
- `GET /audit/stream/{id}` → SSE real-time agent progress
- `GET /report/{id}` → HTML report
- `POST /virality` → synchronous prediction
- `GET /virality/{id}/report` → virality score card
- `GET /demo`, `GET /demo/virality` → pre-cached reports
- `GET /brands`, `GET /brands/data` → brand portfolio
- `POST /compare`, `GET /compare/{id}` → brand comparison
- `POST /action-plan` → per-recommendation implementation guide
- `POST /strategy` → 90-day competitive battle plan
- `GET /share/{token}`, `GET /share/compare/{token}` → shareable links

**Web UI:** Single-page, 4 tabs (Brand Audit, Virality, Compare Brands, My Brands).
Pure HTML/CSS/JS — no external frameworks. Inline in `main.py`.

**SSE implementation:** Polls SQLite every 1 second. Emits `running` events on agent transitions. Sends `complete` with report URL. 20-second keep-alive comments prevent proxy drops. `X-Accel-Buffering: no` header for nginx compatibility.

**First working demo:** Rare Rabbit audit completed at 17:23. Duration: 4m 38s.

---

## Phase 6 — Database + Caching (May 31, 17:00–18:00)

**Built:**

`db/models.py` — 5 SQLModel tables:
- `AuditRun` — audit metadata + full JSON result + share_token
- `ScoreHistory` — per-brand score snapshots for trend tracking
- `CompareRun` — comparison results
- `ViralityRun` — virality predictions
- `BrandMemory` — brand intelligence accumulation

`cache/redis_cache.py` — `CacheManager`
- Auto-detects Upstash vs in-memory based on env vars
- Per-data-type TTLs (audit: 24h, pagespeed: 6h, search: 2h, virality: 12h)
- Max 500 entries with LRU eviction to prevent memory growth
- Cache key: `audit:{sha256(url)[:16]}`

**DB auto-detect:** `DATABASE_URL` env var → PostgreSQL (asyncpg). Unset → SQLite (aiosqlite).

---

## Phase 7 — Bug Fixes + API Validation (May 31, 18:00–19:00)

**Fixed:**

1. `/status` endpoint returning 404 — old process holding port after server restart. Fixed port kill procedure.

2. `POST /audit` accepting invalid URLs — added Pydantic validator:
   ```python
   @validator('url')
   def must_be_valid_url(cls, v):
       if not v.startswith(('http://', 'https://')):
           raise ValueError('Invalid URL')
       return v.rstrip('/')
   ```

3. "ShopOS" branding in HTML output — removed from all templates, orchestrator print statements, virality card CSS classes.

4. `PORT` env var not read on Render deployment — updated startup block to read `int(os.environ.get("PORT", 8000))` and use `host="0.0.0.0"` in production.

**Test suite at this stage:** 113 tests passing (pytest, non-slow tests only).

**Brands audited by end of day:** Rare Rabbit, Nike India, Hoka India, boAt — all complete.

---

## Phase 8 — Feature Expansion (June 1, 09:00–12:00)

### 8.1 Comprehensive Edge Case Handling

`scrapers/result.py` — `DataResult` wrapper enforced across all scrapers.

**Meta Ads edge cases:**
- Brand not found → manual check link + "could mean no ads, paused, or name mismatch"
- Cloudflare block → search inference fallback, confidence="inferred"
- 0 active ads → distinguished from "not found" (`status: "found_no_active"`)
- Ambiguous name → picks best match based on domain similarity

**Website scraping edge cases:**
- Cloudflare → httpx fallback → search-only mode
- Site down → continue with search data, yellow banner in report
- SPA (React/Next) → Playwright waits for `networkidle` + 3s extra
- Redirect loops → max 5 redirects then abort

**PageSpeed edge cases:** 10s timeout → N/A with manual PSI link

**LLM edge cases:** Rate limit retry → malformed JSON retry → context truncation at 6000 tokens

**Agent failure handling:** Any exception → error dict with `status: "failed"`. 1-3 failures → partial report. 4-6 failures → error page with manual tool links.

### 8.2 Source Attribution Panel

Every report now includes a "Data Sources" panel with clickable links:
- Meta Ad Library → constructed URL per brand
- PageSpeed Insights → constructed URL per brand URL
- Google Trends → constructed URL per brand name
- Wikipedia → search URL per brand name
- Confidence colour-coding: green=verified, amber=inferred, grey=unavailable

### 8.3 Impact Scoring on Every Recommendation

Every recommendation now includes:
- `impact_metric`: what specifically improves (e.g. "Mobile conversion rate")
- `impact_estimate`: quantified estimate (e.g. "+5-8%")
- `time_to_see_results`: when visible (e.g. "24-48 hours")
- `confidence`: high/medium/low with visual dot indicator

LLM prompt updated with industry benchmark calibration table.

### 8.4 "One Thing" Summary Banner

After all 6 agents complete, a dedicated LLM call identifies the single highest-impact action achievable in 7 days. Displayed as full-width banner at report top. Specific, measurable, low-effort.

### 8.5 Benchmark Context on Scores

Every score displayed with: category average + top 10% benchmark.
Context based on 50+ Indian D2C brand analysis.

Example: `6.2/10 · Category avg: 5.8 · Top 10%: 8.1 ↑ Above average`

---

## Phase 9 — Parallel Execution (June 1, 12:00–13:30)

**Problem:** Sequential execution took 5-6 minutes. Unacceptable for a user-facing product.

**Analysis of dependencies:**
- Agent 1 (Brand Basics): needs homepage data and search → must run after Phase 1 scraping
- Agents 2-5 (Content, Ads, GEO, Store): only need `url` and `brand_name` → can run concurrently
- Agent 6 (Research): benefits from category/positioning from Agent 1 → runs last

**Solution: 5-phase pipeline**

```
Phase 1 (parallel scraping, no LLM):    ~30s
Phase 2 (Brand Basics alone):           ~20s
Phase 3 (4 agents in parallel):         ~90s  ← replaces 200s sequential
Phase 4 (Research alone):               ~30s
Phase 5 (compile):                      ~10s
Total:                                  ~3min  (vs ~5-6min)
```

`asyncio.gather(*tasks, return_exceptions=True)` — one agent failure cannot cancel others.

**Prefetched data:** Phase 1 results stored in `shared_data` dict, passed to Phase 3 agents. Eliminates duplicate scrapes (e.g. PageSpeed doesn't get called twice).

**Efficiency gain:** ~40% reduction in wall-clock time.

---

## Phase 10 — User Engagement During Wait (June 1, 13:30–14:30)

**Problem:** 2-3 minute wait with only a progress bar is poor UX.

**Solution: Progressive Disclosure**

As each agent completes, its first finding is immediately streamed as a card:
```
✅ Brand Basics — done (2.1s)
   "Rare Rabbit · Premium Indian Menswear · Founded 2015"
   
✅ Content Audit — done (8.3s)
   "PDP Quality: 6.8/10 · Main gap: Feature-heavy copy"
   
⏳ Performance Ads — running...
```

Cards fade-in from right as agents complete. Full report replaces feed on completion.

**Secondary engagement:**
- Live data counter: "Analysed 347 data points so far..."
- "Did you know?" rotating ecommerce insights panel (10 facts, 8s rotation)
- Phase labels: "Running 4 analyses simultaneously..." during Phase 3

---

## Phase 11 — Comparative Report Overhaul (June 1, 14:30–16:00)

**Problem:** Comparison report content was generic — "Brand A has strong content" without citing evidence. Root cause: LLM received only 6 numbers per brand.

**Solution: Rich Context Builder**

Created `_build_rich_context()` extracting 40+ fields per brand:
- Actual PDP copy and rewrites
- Ad headlines, hook strength, funnel coverage
- Schema gaps by type, GEO query results
- PageSpeed scores and CRO fixes
- Competitor names and whitespace opportunities

**Comparison prompts rewritten:**
- `_COMPARE_FINDINGS_PROMPT` → now references actual copy, not just scores
- `_SWOT_PROMPT` → every item must cite a specific score or finding
- `_STRATEGY_PROMPT` → every recommendation references a real gap

**New comparison sections:**
- **Steal This:** 3 concrete tactics each brand should copy from the other
- **Customer Journey Battleground:** Who wins at each funnel stage with specific evidence
- **Underdog Opportunity:** The one action that would most close the gap
- **Shared Blindspot:** What both brands are missing

max_tokens increased from 800 → 2500 for all comparison calls.

---

## Phase 12 — Scoring System Overhaul (June 1, 16:00–17:00)

**Problem:** Scores were inconsistent — some exceeded 100, some used mixed scales.

**Solution: Unified scoring framework**

Two scales, never mixed:
- Section scores (agents): 1-10
- Composite scores (overall health, GEO, virality): 0-100

Explicit rubric added to every LLM prompt preventing score inflation.
`validate_scores()` added to report generator — clamps all values before HTML render.
Out-of-range values logged, never shown to users.

---

## Phase 13 — Pre-Demo Polish (June 1, 17:00–18:00)

**Fixed:**
- Kannada text in ad headlines → `normalize_headlines_to_english()` with LLM translation
- All timestamps converted to IST (UTC+5:30) format: "01 Jun 2026, 03:45 PM IST"
- Source links constructed correctly (Meta Ad Library, PSI, Google Trends) — never null
- Non-English links validated before rendering as `<a>` tags
- `+Add Brand` form wired to `/audit` endpoint with proper JS handler
- My Brands table auto-refreshes after audit completion via `GET /brands/data`
- Status bar "undefined" fields (Mastra, Forecasting) → grey "not configured" dots

**Brands audited and verified:** Rare Rabbit, Nike India, Hoka India, boAt, Minimalist, BeMinimalist — all producing complete reports.

---

## Phase 14 — Production Deployment (June 1, 2026, 18:00–18:30)

### Pre-deploy fixes applied
Three code changes made before pushing to GitHub:

1. **SQLite path:** `./shopos.db` → `/tmp/shopos.db`
   Render's filesystem is ephemeral outside `/tmp`. Using `/tmp` ensures the DB
   initialises correctly on container start and doesn't throw permission errors.

2. **CORS origins:** `["http://localhost:3000"]` → `["*"]`
   Prototype allows all origins so the API works from any frontend, Postman,
   or the founder's browser without CORS errors.

3. **PORT env var handling:** Already in place from Phase 5.
   `int(os.environ.get("PORT", 8000))` — Render injects `PORT` at runtime.
   `host="0.0.0.0"` in production (required for Docker), `127.0.0.1` locally.
   `webbrowser.open()` skipped when `PORT` is set.

### Git commit
- Repository initialised, `main` branch set
- 77 files committed in single clean commit
- `.env` confirmed absent from tracked files
- `shopos.db` excluded via `.gitignore`
- `output/` directory excluded (runtime artefacts)

### Render.com setup
- Platform: Render.com free tier (Docker runtime)
- Region: Singapore (lowest latency from India)
- Build: Docker image (~800MB, includes Playwright Chromium)
- First build time: ~8 minutes
- Environment variables set: `GROQ_API_KEY`, `FIRECRAWL_API_KEY`
- Auto-deploy: enabled on push to `main` branch

### Live URL
`https://research-agent.onrender.com` *(update with actual URL)*

### Deployment verification
After deploy, confirmed:
- `GET /health` → 200 `{"status":"ok"}`
- `GET /demo` → instant Rare Rabbit report (no API calls)
- `POST /audit` → queues successfully
- `GET /status` → all components reporting correctly

---

## Phase 15 — Social Profile Agent / Agent 7 (June 2, 2026)

**Added:** `agents/social_profile.py` — `SocialProfileAgent`

Agent 7 enriches audit reports with social & brand presence intelligence:
- **Instagram**: instaloader scrape of public profile (followers, post count, engagement estimate, content mix, top hashtags, latest captions). Runs synchronously in asyncio executor.
- **LinkedIn**: StealthyFetcher stealth scrape (company size, industry, description). Accepts HTTP 999 as LinkedIn's bot-OK response alongside 200.
- **Ad Creative Intelligence**: uses existing Meta Ads headlines; classifies hook type (curiosity/social-proof/discount/fear) via LLM or text-only fallback when image URLs unavailable.
- **Score**: 0-10 social presence score with top-3 improvement recommendations.

**DB:** Added `social_profile: Optional[str]` column to `AuditRun`.
**Orchestrator:** Updated to 7 agents; all `/7` progress fractions.
**Report template:** Section 7 added (Instagram card, LinkedIn card, Ad Creative Intelligence, improvement list).
**Tests:** `_AGENT_RESULTS` updated to include `social_profile`; orchestrator assertions updated from `== 6` to `== 7`.

---

## Phase 16 — DeepGaze IIE Visual Attention Heatmaps (June 2, 2026)

**Added:** `agents/visual_attention.py` — `VisualAttentionAnalyzer`

Predicts human gaze patterns on product images:
- Model: **DeepGaze IIE** — neural saliency model trained on eye-tracking data
- Install: `pip install git+https://github.com/matthias-k/DeepGaze.git` + CLIP dependency
- Input: product image URL → download → 512×512 resize → saliency map (512×512)
- Output: heatmap PNG (base64), attention focus zone (upper/middle/lower × left/center/right), concentration%, interpretation
- API: `centerbias` must be 3D `(1, 512, 512)` — 4D causes silently wrong spatial dims
- Backend: `matplotlib.use("Agg")` set at module level for headless operation
- Inference: CPU, blocking, runs in `asyncio.run_in_executor()` to avoid blocking event loop

**Integration:** Step 2.6 in `agents/virality.py`. Runs on first accessible product image.
**Display:** Heatmap embedded as data URI in virality card; focus chip, distribution chip, interpretation text, powered-by footer.
**macOS note:** Python.org installer lacks CA certs → `SSL: CERTIFICATE_VERIFY_FAILED` on model weight download. Dev fix: `ssl._create_default_https_context = ssl._create_unverified_context` in test scripts only.

---

## Phase 17 — Meta TRIBE v2 Neural Engagement (June 2, 2026)

**Added:** `agents/neural_engagement.py` — `NeuralEngagementAnalyzer`

Predicts neural (fMRI) engagement from video/audio content using Meta's TRIBE v2 model:

**What TRIBE v2 actually does** (honest scope):
- fMRI encoding model trained on Algonauts 2025 challenge data (Friends sitcom + 4 films)
- Inputs: video/audio/text streams → word-level events via transcription pipeline
- Outputs: `(n_TRs, 1000_parcels)` predicted brain activation in z-score units per ~1.49s segment
- NOT an image model — requires `get_events_dataframe(video_path=...)` with actual video file
- Checkpoint: requires `config.yaml` + `best.ckpt` (local or HuggingFace repo `facebook/tribev2`)

**What was built:**
- Smart video URL resolver: handles YouTube/Instagram/TikTok/Vimeo platform URLs, extracts embedded video from scraped page HTML/description, falls back to yt-dlp on product URL
- Download: yt-dlp primary (1000+ platforms), direct HTTP fallback for `.mp4`/`.mkv` URLs
- Score: mean absolute activation normalized to 0-100 (calibrated: 0.40 z-score → 100)
- Metrics: consistency score, early hook ratio (first 25% vs overall), n_TRs analyzed
- 180s timeout; graceful error return when no video found or model unavailable

**Integration:** Step 2.7 in `agents/virality.py`. Fires after attention heatmap, before social search.
**Display:** Score (0-100) + tier badge + bar + chips + interpretation + CC-BY-NC-4.0 footer.
**Setup:** `pip install yt-dlp`, `pip install -e ../tribeV2`, set `TRIBE_CHECKPOINT_DIR` env var.

**Key architectural decision:** TRIBE v2 was initially requested as an "image-to-brain-activation" model. After reading the actual source, confirmed it processes video/audio streams only. Integration was redesigned to use it honestly for its actual capability — neural engagement from video ad content.

---

## Final State — June 2, 2026

**Agents:** 7 (added Social Profile Agent)
**Virality pipeline steps:** 7 (visual signals → Llama vision → DeepGaze IIE → TRIBE v2 → social search → LLM scoring → trajectory)
**Test coverage:** 117 tests passing (0 failures; orchestrator tests updated for 7-agent count)
**New files:** `agents/social_profile.py`, `agents/visual_attention.py`, `agents/neural_engagement.py`
**Status bar:** Shows TRIBE v2 readiness (loaded / checkpoint needed / not installed)
**`esc()` bug:** JS HTML-escape function was called but never defined — fixed
**Total build time:** ~36 hours across 17 phases

---

## Phase 18 — Agentic ReAct Loop (June 3–4, 2026)

**Problem:** The linear orchestrator (`agents/orchestrator.py`) ran agents in a fixed sequence regardless of what earlier agents found. No adaptive routing, no reasoning layer, no cross-agent pattern detection.

**Solution:** Full ReAct (Reason → Act → Observe → Synthesize) agentic redesign in `agents/agentic_orchestrator.py`.

**New files added:**

`agents/reasoning_brain.py` — `ReasoningBrain` class with five cognitive phases:
- `initial_plan()` → opening hypothesis, priority agents, predicted issues, investigation posture
- `plan_next(memory, remaining)` → continue / skip / reorder decision per agent
- `observe(agent_key, result)` → extracts cross-cutting signals with severity + triggered actions
- `cross_synthesize(memory)` → pattern detection every 3 agents
- `final_synthesis(memory)` → meta-narrative, root cause, hidden opportunity

`agents/working_memory.py` — `WorkingMemory` accumulator:
- Stores findings, signals, cross-insights, reasoning trace, decisions with timestamps
- `has_signal(type)` — quick lookup for signal-driven routing
- `add_cross_insight(text)` — records pattern-level insights across agents

`agents/agentic_orchestrator.py` — ReAct loop:
- `_TaskQueue` — dynamic queue with `skip()`, `move_to_front()`, `pop_batch()` operations
- `_run_agentic_loop()` — the core ReAct cycle
- `_cross_validate_findings()` — 6 cross-agent pattern checks (paid-to-site leak, social→CRO mismatch, AI-invisible content, mobile bottleneck, trust deficit, social awareness gap)
- `_generate_analyst_brief()` — McKinsey-grade analyst brief from cross-validated findings
- `run_full_audit()`, `run_all()` — drop-in replacements for original orchestrator

**Adaptive behaviours implemented:**

| Trigger | Brain Action |
|---|---|
| `social_profile` finds zero social presence | Skips `social_media_audit` automatically |
| `social_profile` finds >100K followers | Reorders — runs deep social audit next |
| `store_cro` mobile score < 40 | Raises `conversion_crisis` critical signal |
| `geo_visibility` score < 30 | Raises `ai_search_gap` critical signal |
| 3+ agents fail hard | `stop_early` — returns diagnostic partial report |
| Brand <6 months old + no ads | Skips `performance_ads` agent |

**Detected patterns:**
`invisible_brand`, `ghost_advertiser`, `social_darling`, `great_product_bad_store`, `ai_search_gap`, `conversion_crisis`, `hidden_gem`

**Audit output additions:** `agentic_meta`, `signals`, `cross_insights`, `reasoning_trace`, `decisions`, `skipped_agents`, `pattern_detected`, `investigation_posture`

**Compatibility:** `agents/orchestrator.py` kept as a thin wrapper delegating to `agentic_orchestrator`. No FastAPI changes required.

---

## Phase 19 — Free Data Sources Integration (June 5, 2026)

**Added three new free data scrapers:**

### 19.1 Wayback Machine CDX API (`scrapers/wayback.py`)
- Free CDX API at `https://web.archive.org/cdx/search/cdx` — no auth required
- Input: brand domain → Output: `{first_seen, years_online, total_snapshots, crawl_frequency, longevity_signal}`
- Bug fixed during development: `filter=statuscode:200&collapse=year` returns HTTP 400 on many domains. Fix: fetch all rows with `limit=500`, filter status=200 client-side.
- Wired into `agents/brand_basics.py` via `asyncio.gather` — adds `longevity` to brand basics output
- Signal in LLM prompt: `"WAYBACK MACHINE — First seen: 2018-03 | Years online: 6 | Signal: established"`

### 19.2 Google Trends via pytrends (`scrapers/trends.py`)
- `pytrends` library wrapping Google Trends unofficial API — no API key
- `get_brand_trends(brand_name, geo="IN")` → `{relative_interest, trend_direction, peak_week, recent_average}`
- Runs via `asyncio.to_thread` (pytrends is blocking)
- Wired into `agents/research.py` alongside 4 DDG searches in a 6-way `asyncio.gather`
- Signal in LLM prompt: `"GOOGLE TRENDS (India): relative_interest=78/100 | direction=rising | peak_week=2026-05-12"`

### 19.3 Tracxn Researcher (`agents/tracxn_researcher.py`)
- Optional: requires paid `TRACXN_API_KEY` — silently returns `{"note": "no key"}` without it
- REST API at `https://api.tracxn.com/api/2.1`
- Output: `{company_name, stage, funding_display, investors, founded}`
- Wired into `agents/research.py` via the same 6-way gather as Google Trends

---

## Phase 20 — Parallel Middle Agents (June 5, 2026)

**Problem:** After the ReAct loop was introduced, agents still ran sequentially. With the reasoning brain adding overhead per agent (~5s each for LLM planning calls), total audit time crept back up to 4-5 minutes.

**Solution:** After `brand_basics` validates the brand, pop 5 independent agents from the queue and run them concurrently via `asyncio.gather`.

```python
_PARALLEL_MIDDLE = ["content_catalog", "performance_ads", "geo_visibility", "store_cro", "research"]
```

These 5 have no cross-dependencies on each other — only on `brand_basics` output, which is complete before the batch fires.

**Added to `agentic_orchestrator.py`:**
- `_TaskQueue.pop_batch(keys)` — removes and returns multiple keys at once
- `_run_one(agent_key, ...)` — isolated agent runner that updates memory, brain, and progress callback
- Parallel launch block after `brand_basics` success: `asyncio.gather(*tasks, return_exceptions=True)`
- Cross-synthesis run automatically after the parallel batch completes

**Time savings:** ~90s parallel wall-clock vs ~200s sequential for the same 5 agents.

---

## Phase 21 — LLM Tier 3 + Vision Client (June 5, 2026)

**Added Gemini 2.0 Flash as third-tier LLM fallback in `llm/client.py`:**

Full tier chain:
1. `llama-3.3-70b-versatile` (Groq) — primary
2. `llama-3.1-8b-instant` (Groq) — on 70B rate limit (resets after 90s)
3. `gemini-2.0-flash` (Google, via OpenAI-compatible endpoint) — on both Groq tiers exhausted (resets after 120s)

Gemini called via `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions`. Same message format as Groq — no SDK change needed. Requires `GEMINI_API_KEY` env var.

**Added MiniMax VL-01 for vision via NVIDIA NIM (`llm/client.py` — `MiniMaxClient`):**
- 41 free models on NVIDIA NIM build platform including MiniMax M2.7 (multimodal)
- Endpoint: `https://integrate.api.nvidia.com/v1` with `NVIDIA_API_KEY`
- Used for image analysis in virality pipeline (Step 2.5) — falls back to Llama 4 Scout on error
- Unlimited during NVIDIA NIM promo period

**Status endpoint updated:** `/status` now reports `gemini: ok (fallback ready)`.

---

## Phase 22 — TRIBE v2 Hook Window + Scrapling Fix (June 6, 2026)

### 22.1 TRIBE v2 Hook Window
Changed `TRIBE_HOOK_SECONDS` default from `15` → `9` in `agents/neural_engagement.py`:
```python
hook_secs = int(os.getenv("TRIBE_HOOK_SECONDS", "9"))
```
Processing only the first 9 seconds of a Reel captures the full hook window while cutting CPU inference time by ~40%.

### 22.2 Scrapling Deprecation Fix
Scrapling v0.4.8 changed `StealthyFetcher` to use classmethods for all browser settings. Old instantiation-based API was deprecated and caused `WARNING: This logic is deprecated now, and have no effect` in every scrape.

**Three files fixed:**
- `scrapers/web_scraper.py` line 284, 292: `StealthyFetcher.fetch(url, headless=True, network_idle=True)` and `DynamicFetcher.fetch(url)`
- `scrapers/meta_ads.py` line 309: `_StealthyFetcher.fetch(source_url, headless=True, solve_cloudflare=True)`
- `agents/social_profile.py` line 100: `_StealthyFetcher.fetch(url, headless=True, solve_cloudflare=True)`

---

## Phase 23 — Rate Limit Bug Fix (June 10, 2026)

**Critical production bug:** All-zeros audit results for `orangesugar.in`. Every agent showed `{"_raw": "", "_parse_error": "Could not parse JSON after repair attempt"}`.

**Root cause analysis:**
- Phase 20 introduced 5 parallel agents via `asyncio.gather`
- All 5 fired LLM calls simultaneously: 5 agents × 2-3 calls each = ~15 API calls in the first 10 seconds
- Groq free-tier RPM limit: 30 requests/minute. 15 calls in 10s = 90 req/min burst → immediate throttling
- Groq silently returned empty response bodies (HTTP 200 but `choices[0].message.content = ""`) instead of 429 errors
- `analyze_structured()` received `""` → `json.loads("")` failed → parse error → all scores = `None` → report showed 0/0/0
- `_LLM_SEMAPHORE = asyncio.Semaphore(3)` only limited concurrent HTTP connections, not the rate at which multiple agents consumed the RPM quota

**Three-layer fix in `llm/client.py` + `agentic_orchestrator.py`:**

**1. Reduced `_LLM_SEMAPHORE` from 3 → 2**
Fewer concurrent API calls directly reduces peak RPM.

**2. Minimum call spacing (`_MIN_CALL_SPACING = 2.2s`)**
```python
_LAST_CALL_TS: float = 0.0
_MIN_CALL_SPACING = 2.2  # 2.2s × 2 concurrent = ~27 req/min, safely under 30
```
Inside the semaphore block, checks time since last call and sleeps if needed before firing the next request.

**3. Retry on empty response in `analyze()` and `analyze_structured()`**
```python
# In analyze(): empty body treated as soft rate limit
if not content.strip():
    if attempt < _RETRY_ATTEMPTS - 1:
        wait = _RETRY_BASE_DELAY * (2 ** attempt)  # 10s, 20s
        await asyncio.sleep(wait)
        continue

# In analyze_structured(): outer retry loop for persistent empty responses
for _outer in range(3):
    raw = await self.analyze(...)
    if raw.strip(): break
    if _outer < 2:
        wait = 25 * (2 ** _outer)  # 25s, 50s
        await asyncio.sleep(wait)
```

**4. Agent-level concurrency gate (`_AGENT_LLM_GATE = asyncio.Semaphore(2)` in `agentic_orchestrator.py`)**
Wraps the execution body of `_run_one()` — limits to 2 full agents making LLM calls simultaneously regardless of how many are in the parallel batch. Other agents queue and start as slots free up.

**Result:** Under sustained load, no more all-zeros results. The gate + spacing keeps throughput at ~27 req/min, and the retry logic recovers from any remaining bursts.

---

## Updated Architecture Summary — June 2026

**Agents:** 8 (Brand Basics, Content Catalog, Performance Ads, GEO Visibility, Store CRO, Research, Social Profile, Social Media Deep Audit)
**Orchestration:** ReAct loop with ReasoningBrain + WorkingMemory — not a fixed pipeline
**Execution model:** brand_basics sequential → 5 middle agents parallel (gated at 2 concurrent) → social agents sequential
**LLM tiers:** Groq 70B → Groq 8B → Gemini 2.0 Flash → retry with backoff → graceful degradation
**Vision:** MiniMax VL-01 via NVIDIA NIM → Llama 4 Scout (Groq) fallback
**Data sources:** Playwright scraping, DuckDuckGo, PageSpeed API, Meta Ad Library, Instagram API, Wayback Machine CDX, Google Trends (pytrends), Tracxn (optional)
**Scraping library:** Scrapling v0.4.8 (StealthyFetcher classmethod API)
**Neural video:** Meta TRIBE v2 — first 9s hook window (configurable via `TRIBE_HOOK_SECONDS`)
**Visual attention:** DeepGaze IIE heatmaps in virality pipeline
**Total build time:** ~60 hours across 23 phases

---

## Full Key Architectural Decisions (Updated)

| Decision | Alternative considered | Reason chosen |
|----------|----------------------|---------------|
| Groq / Llama 3.3 70B | OpenAI GPT-4 | Free tier sufficient, 20x cheaper at scale |
| Gemini 2.0 Flash as tier 3 | Single LLM source | Prevents all-zeros audits when Groq quota exhausted |
| MiniMax VL-01 via NVIDIA NIM | OpenAI Vision | Free unlimited via NVIDIA NIM promo |
| Scrapling StealthyFetcher | Raw Playwright | Anti-bot evasion built-in, classmethod API in v0.4.8 |
| Chronos for trend forecasting | TRIBE v2 | TRIBE v2 is a neuroscience model, not time-series |
| TRIBE v2 for video neural engagement | Synthetic "neural" score | Honest use of actual model capability |
| Sequential → phased parallel → ReAct | Full parallel | Research agent needs Brand Basics context; ReAct adds adaptive routing |
| _AGENT_LLM_GATE = Semaphore(2) | No gate | Without it, 5 parallel agents exhaust Groq RPM → all-zeros results |
| Wayback Machine CDX API | Domain WHOIS | Free, no auth, richer longitudinal signal than WHOIS |
| pytrends for Google Trends | Google Trends API | No API key needed, good enough for relative interest signals |
| TRIBE_HOOK_SECONDS = 9 | Full reel duration | First 9s captures the hook; ~40% CPU savings vs 15s default |
| Inline HTML UI | Next.js + shadcn | Fewer moving parts for prototype, ships faster |
| Browser print for PDF | WeasyPrint | WeasyPrint had dark-theme CSS rendering issues |
| Playwright as primary scraper | Apify/BrightData | Zero cost, self-hosted, unlimited |
| SQLite for dev | PostgreSQL everywhere | Simpler local setup; Neon used for production |
