# Architecture Report — SHOPOS Brand Audit Agent
**Version:** 5.0 | **Date:** June 2026 | **Stack:** Python / FastAPI / Groq (Kimi K2) / Gemini / Scrapling / Playwright / TRIBE v2 / DeepGaze IIE

---

## 1. System Overview

SHOPOS is an 8-agent D2C brand intelligence platform. A user submits any brand URL and receives
a full-section audit report covering brand identity, content quality, paid advertising, AI search
visibility, store performance, competitive landscape, social presence, and social media engagement
— in approximately 3 minutes.

The pipeline is driven by `agents/agentic_orchestrator.py`: a phased parallel pipeline with
bounded retry loops. A `WorkingMemory` accumulates signals and cross-insights across all agents
and is persisted to DB, powering the **Reasoning Brain** panel in every report.

Data sources span web scraping (Scrapling StealthyFetcher + DynamicFetcher primary, Playwright
fallback), DuckDuckGo search, PageSpeed API, Instagram mobile API + og:description extraction,
**Wayback Machine CDX API** (domain age), **Google Trends via pytrends**, and optional **Tracxn
REST API** (startup funding intelligence).

Sections appear progressively as each agent completes (progressive reveal via SSE). The full
report is available at the end as a shareable HTML artifact.

A secondary virality predictor scores any product's content across 7 psychological dimensions,
runs neural saliency heatmaps (DeepGaze IIE), predicts neural engagement from video content
(Meta TRIBE v2), and estimates social media spread potential.

---

## 2. High-Level Architecture

```
User → FastAPI (port 8000)
         │
         ├── POST /audit → Background Task
         │      │
         │      └── Agentic Orchestrator (agents/agentic_orchestrator.py)
         │             │  [Phased pipeline: Prefetch → Brand → Parallel Core → Social → Synthesis]
         │             │
         │             ├── WorkingMemory     ← signal accumulator + trace (persisted to DB)
         │             │
         │             ├── Phase 0: Prefetch (parallel, zero LLM)
         │             │     └── homepage scrape + pagespeed
         │             │
         │             ├── Phase 1: BrandBasicsAgent        (sequential)
         │             │
         │             ├── Phase 2: [6 agents, fully parallel via asyncio.gather]
         │             │     ├── ContentCatalogAgent
         │             │     ├── PerformanceAdsAgent
         │             │     ├── GEOVisibilityAgent
         │             │     ├── StoreCROAgent
         │             │     ├── ResearchAgent
         │             │     └── SocialProfileAgent
         │             │
         │             ├── Phase 3: SocialMediaAuditAgent   (sequential, skipped if no social)
         │             │     └── [if deep_visual=True]
         │             │           └── TRIBE v2 fMRI → brain_map.py → SVG heatmap
         │             │
         │             └── Phase 4: Synthesis (3 parallel LLM calls)
         │                   ├── analyst_brief
         │                   ├── one_thing
         │                   └── roadmap
         │
         ├── GET /audit/stream/{id}?deep_visual=0|1 → SSE (real-time agent progress)
         ├── GET /report/section/{id}/{key} → Single-agent section HTML (progressive reveal)
         ├── GET /report/{id} → Full HTML report
         ├── GET /brain-map → Neural heatmap demo page
         ├── POST /virality → ViralityPredictor
         ├── POST /compare → Brand comparison
         └── GET /brands → Portfolio view
```

---

## 3. Technology Decisions

### 3.1 LLM — Three-Tier Chain (Groq → Gemini)

**Tier 1:** `moonshotai/kimi-k2-instruct` (Groq) — primary. 131K context, purpose-built for agentic tasks and structured JSON. Significantly better at multi-step reasoning and analysis than llama-3.3-70b.
**Tier 2:** `llama-3.3-70b-versatile` (Groq) — activates on Kimi K2 rate-limit (429), resets after 90 seconds
**Tier 3:** `gemini-2.0-flash` (Google, OpenAI-compatible endpoint `https://generativelanguage.googleapis.com/v1beta/openai`) — activates when both Groq tiers are rate-limited, resets after 120 seconds

**Rate limiting and concurrency:**
- `_LLM_SEMAPHORE = asyncio.Semaphore(2)` — at most 2 LLM calls in flight at any moment
- `_MIN_CALL_SPACING = 2.2s` — enforced minimum gap between calls to stay within RPM limits
- Retry on empty response: 10s / 20s in `analyze()`, 25s / 50s outer loop in `analyze_structured()`
- JSON parsing: regex fallback if model returns malformed JSON, retry once with explicit instruction

**Vision:**
- Primary: MiniMax VL-01 via NVIDIA NIM (`https://integrate.api.nvidia.com/v1`, free tier via `NVIDIA_API_KEY`)
- Fallback: `meta-llama/llama-4-scout-17b-16e-instruct` (Groq) for image analysis in virality pipeline

**Rationale:**
- Kimi K2 on Groq: 131K context (vs 32K for llama-3.3-70b), near-zero JSON formatting failures, stronger structured output — zero latency penalty on Groq's H100s
- Groq's free tier provides ~100 requests/day — sufficient for prototype
- Gemini 2.0 Flash provides a zero-cost third tier on a separate quota

### 3.2 Web Scraping — Scrapling Primary, Playwright Fallback

**Scrapling v0.4.8 (primary):**
- `StealthyFetcher.fetch(url, headless=True, solve_cloudflare=True, network_idle=True, timeout=90_000, wait=1_500)` — Cloudflare-bypassing, stealth headers
- `DynamicFetcher.fetch(url, headless=True, network_idle=True, timeout=90_000, wait=1_500)` — full JS rendering
- Both are **classmethods** — do not instantiate. `timeout` in milliseconds.

**Playwright (fallback):** Chromium headless — handles JavaScript-heavy SPAs when Scrapling is blocked

**Fallback chain for `scrape_pdp()`:**
1. `StealthyFetcher` (90s, Cloudflare bypass)
2. `DynamicFetcher` (90s, full JS)
3. Playwright (60s)
4. Terminal `DataResult(value=None, confidence="unavailable")` — always returns

**Fallback chain for `scrape_page()`:**
1. `StealthyFetcher` (90s)
2. `DynamicFetcher` (90s)
3. Playwright
4. httpx → curl-cffi (Chrome TLS impersonation)

**Quality over latency:** All Scrapling calls use `timeout=90_000` (90s) and `wait=1_500` (1.5s post-idle). Generous timeouts intentional — scraper wall is a bigger quality risk than latency.

**PDP parsing:** `_parse_pdp_from_soup()` extracts JSON-LD Product schema first, then CSS selectors as fallback. Returns `DataResult`.

> **Breaking change in Scrapling v0.4.8:** `StealthyFetcher.fetch()` and `DynamicFetcher.fetch()` are **classmethods**. `PlayWrightFetcher` class does not exist in this version — use `DynamicFetcher` instead.

**Shopify detection:** Checks `X-Shopify-Stage` response header and `/products.json` endpoint availability via fast httpx pre-check before launching Playwright.

### 3.3 Instagram Scraping — Mobile API + Scrapling + og:description Parsing

**Attempt order (`scrapers/instagram_scraper.py`):**
1. Mobile API: `https://i.instagram.com/api/v1/users/web_profile_info/?username={username}`
2. **Scrapling `DynamicFetcher`** (stealth, 90s) — extracts follower count from `og:description` meta tag
3. Playwright browser fallback

**og:description follower extraction:**
Instagram's `og:description` contains: `"500K Followers, 234 Following, 890 Posts – See Instagram photos..."`
`_parse_followers_from_og_desc()` regex-parses this with `_parse_ig_number()` handling `K/M/B` suffixes.

**DDG follower fallback (`agents/social_profile.py`):**
When scraping returns bio but no follower count, the social profile agent runs a DDG search
(`{brand} instagram followers site:instagram.com OR site:socialblade.com`) and extracts
estimated follower count from search snippets. Marked `confidence="inferred"`.

**has_social gate:** `has_social = bool(ig_data.get("followers") or ig_data.get("bio"))` — social
audit runs whenever followers OR bio is available. Bio-only results (Scrapling partial success)
are no longer discarded; they now trigger SocialMediaAuditAgent.

**In-process cache:** 5-minute `_PROFILE_CACHE` prevents double-scraping (Agent 7 + Agent 8 both need the same profile data).

### 3.4 Meta Ads Scraping — `scrapers/meta_ads.py`

**Attempt order:**
1. Scrapling `StealthyFetcher` (`network_idle=True, timeout=90_000, wait=1_500, solve_cloudflare=True`)
2. Playwright browser

`_parse_meta_ads_soup()` handles: login wall detection (return None → fall through to Playwright), no-results, ad card extraction, headline filtering (`_filter_headlines`, `_JUNK_HEADLINE_PATTERNS`), regional script translation via LLM.

### 3.5 Search — DuckDuckGo

**Chosen:** `duckduckgo-search` Python library
**Rationale:** No API key required, no rate limit on free tier within reason.
Used for: brand background research, competitor discovery, news mentions, Reddit sentiment, Instagram username discovery, follower count estimation.

### 3.6 Database — SQLite (dev) / Neon PostgreSQL (prod)

**Dev:** SQLite via SQLModel ORM — zero config, file-based
**Prod:** Neon (free tier: 500MB serverless PostgreSQL)
Auto-detected via `DATABASE_URL` environment variable.

**Schema (`db/models.py`):**
- `AuditRun` — audit metadata, status, progress, full JSON results per agent, share token, synthesis fields
- `ScoreHistory` — per-brand score snapshots for trend tracking
- `CompareRun` — comparison results with both audit IDs
- `ViralityRun` — virality predictions with product data
- `BrandConnector` — third-party API tokens per brand (Shopify, Meta Marketing API)

**`AuditRun` columns (key fields):**
| Column | Type | Contents |
|--------|------|---------|
| `brand_basics` … `social_media_audit` | TEXT | JSON blobs per agent |
| `one_thing` | TEXT | Single top priority recommendation |
| `roadmap_json` | TEXT | 30-day action roadmap |
| `analyst_brief_json` | TEXT | Analyst brief + verdict |
| `cross_findings_json` | TEXT | Cross-agent rule-based patterns |
| `agentic_meta_json` | TEXT | `WorkingMemory.to_report_dict()` — powers Reasoning Brain panel |
| `report_html` | TEXT | Cached rendered report HTML |
| `share_token` | TEXT | Public share link token |

**DB migrations (`db/database.py`):** `_migrate_columns()` runs at startup — adds new columns via `ALTER TABLE` without dropping data. Covers all columns added post-initial schema including `analyst_brief_json`, `cross_findings_json`, `agentic_meta_json`.

### 3.7 PageSpeed — `scrapers/pagespeed.py`

**API:** Google PageSpeed Insights (free, 25K queries/day per key)
**Rate limiting:** `_PSI_LOCK` serializes calls, 4s minimum gap between calls
**Retry on 429:** backoff 5s → 15s → 45s (3 attempts)
**Cache:** 10-minute TTL per (url, strategy) pair
**Both strategies:** mobile + desktop fetched sequentially; partial result (one strategy fails) returns `confidence="inferred"`

### 3.8 Caching — Upstash Redis (prod) / in-memory TTLCache (dev)

**TTL policy:**
| Data type | TTL |
|-----------|-----|
| Full audit result | 24 hours |
| PageSpeed score | 10 minutes (in pagespeed.py) |
| Search results | 2 hours |
| Instagram profile | 5 minutes (in-process, per audit) |

### 3.9 Trend Forecasting — Chronos (Amazon)

**Chosen:** `chronos-forecasting` (Amazon, MIT license)
Fallback chain: Chronos → Prophet → numpy linear regression.
**Used by:** Virality predictor trajectory forecasting.

### 3.10 Neural Engagement — Meta TRIBE v2

**Chosen:** TRIBE v2 (Meta, CC-BY-NC-4.0) — fMRI encoding model trained on naturalistic video/audio.
Predicts cortical activations across 1,000 Schaefer parcels (7 Yeo functional networks).

**Used in two contexts:**
1. **Virality Predictor (Step 2.7):** Scores product video URLs for neural engagement.
2. **SocialMediaAuditAgent (`deep_visual=True`):** Downloads Reels via yt-dlp, runs TRIBE v2 locally, generates per-reel brain activation heatmaps.

**Integration:**
- `get_events_dataframe(video_path=...)` → `predict()` → `(n_TRs, 1000_parcels)` z-score matrix
- Score = mean absolute activation normalized to 0-100
- `num_workers=0` required (avoids CUDA/multiprocessing conflicts on CPU)
- Requires `TRIBE_CHECKPOINT_DIR` env var or HF hub cache at `~/.cache/huggingface/hub/models--facebook--tribev2`

### 3.11 Brain Activation Heatmap — `agents/brain_map.py`

Converts TRIBE v2 predictions into SVG heatmap across the 7 Yeo functional networks.

**Two input modes:**
1. **Real TRIBE v2:** `(n_TRs, 1000)` Schaefer parcel predictions → per-network mean activation
2. **Estimated:** Virality dimension scores → approximate network activation via fixed mapping

### 3.12 Visual Attention — DeepGaze IIE

**Role:** Step 2.6 in virality pipeline — predicts where human eyes look on a product image.
Install: `pip install git+https://github.com/matthias-k/DeepGaze.git` + CLIP dependency.
Runs on CPU via `asyncio.run_in_executor()`.

### 3.13 Wayback Machine — `scrapers/wayback.py`

**API:** Free CDX API — no auth required.
**Used by:** BrandBasicsAgent for domain age + longevity signal.
**Implementation note:** `filter=statuscode:200&collapse=year` returns HTTP 400 on many domains — implementation fetches raw snapshots with `limit=500` and filters client-side.

### 3.14 Google Trends — `scrapers/trends.py`

**Library:** `pytrends` — no API key.
Async integration: `asyncio.to_thread()` to avoid blocking event loop.
**Used by:** ResearchAgent, runs in parallel with DDG queries via `asyncio.gather`.

### 3.15 Tracxn — `agents/tracxn_researcher.py`

**Auth:** Optional — `TRACXN_API_KEY` env var.
Degrades silently when key is absent (`{"note": "no key"}`).
**Used by:** ResearchAgent for funding stage + investor intelligence.

---

## 4. The 8 Audit Agents

### Agent 1 — Brand Basics
**Data sources:** Homepage scrape, DuckDuckGo (Wikipedia, Crunchbase, LinkedIn), Shopify meta tags, Wayback Machine CDX (domain age)
**Output:** Brand name, founding year, founders, HQ, revenue range, target audience, positioning, tone of voice, domain longevity signal

### Agent 2 — Content + Catalog
**Data sources:** Homepage, top 5 product PDPs (auto-discovered via Scrapling StealthyFetcher → DynamicFetcher → Playwright), About page, blog
**Output:** PDP quality score (1-10), headline clarity, benefit vs feature ratio, before/after rewrites, homepage score
**PDP scrape:** 3-tier attempt chain (StealthyFetcher → DynamicFetcher → Playwright). JSON-LD Product schema parsed first; CSS selectors as fallback. Always returns `DataResult` — never raises.

### Agent 3 — Performance Ads
**Data sources:** Meta Ad Library (Scrapling StealthyFetcher primary, Playwright fallback)
**Output:** Active ad count, creative format breakdown, hook strength score, funnel coverage, targeting signals
**Scrapling params:** `network_idle=True, timeout=90_000, wait=1_500, solve_cloudflare=True`
**Edge case:** Login wall detected → falls back to Playwright. Brand not found → `{"ads_count": 0, "status": "not_found"}` + manual check link.

### Agent 4 — GEO Visibility
**Data sources:** Schema.org JSON-LD from homepage HTML, DuckDuckGo simulating AI search
**Output:** GEO score (0-100), schema audit (Product/FAQ/Review/Organization), AI citation likelihood, 90-day roadmap
**GEO score formula:** +20 AI citation, +15 product schema, +15 FAQ schema, +10 review schema, +10 org schema, +15 Wikipedia/authority mention, +15 multi-query AI presence.

### Agent 5 — Store CRO
**Data sources:** PageSpeed Insights API (serialized, rate-limited, cached), storefront scrape
**Output:** Mobile/desktop PageSpeed, Core Web Vitals (LCP/CLS/INP), funnel friction points, top 5 CRO fixes

### Agent 6 — Research
**Data sources:** DuckDuckGo (4 queries), Google Trends, optional Tracxn — all in one `asyncio.gather`
**Output:** Top 3-5 competitors, industry trends, whitespace opportunities, 3 strategic recommendations

### Agent 7 — Social & Brand Presence
**Data sources:** Instagram (mobile API → DynamicFetcher stealth → Playwright), Meta Ads headlines
**Output:** Instagram metrics (followers, posts, content mix), ad creative intelligence, social presence score (0-10)
**Instagram attempt chain:** mobile API → Scrapling DynamicFetcher (90s, extracts followers from og:description) → Playwright
**DDG follower fallback:** When scraping returns bio but no followers, DDG search estimates follower count. Marked `confidence="inferred"`.
**Partial data handling:** Bio-only result now preserved as `confidence="partial"` — triggers Agent 8 instead of skipping.

### Agent 8 — Social Media Deep Audit
**Data sources:** Instagram profile (from Agent 7 cache), Reels via yt-dlp, optional TRIBE v2
**Output:** Overall social score (0-10), engagement rate, content quality, Reels virality, per-reel brain maps (if `deep_visual=True`)
**Skip condition:** `has_social = bool(ig_data.get("followers") or ig_data.get("bio"))` — runs when either is available
**Skipped state:** When skipped, renders a "Social Audit Not Run" card with skip reason — never returns 0 bytes

---

## 5. Progressive Section Reveal

Each agent writes its result to DB as it completes. Frontend polls `/report/section/{audit_id}/{agent_key}` on each SSE `agent_done` event, fetching that section's rendered HTML and injecting it into `#live-sections`.

```
SSE event: agent_done {key: "brand_basics", elapsed: 18}
    → fetch /report/section/{id}/brand_basics
    → inject into #live-sections
    → section auto-opens (details[open])
... repeat for each of 8 agents ...
SSE event: complete → show "↗ Full Report" link
```

`generate_section(agent_key, audit_data)` in `reports/generator.py` renders the full Jinja2 template and extracts the target section via `<!-- SECTION:key -->` / `<!-- /SECTION:key -->` markers.

---

## 6. WorkingMemory + Reasoning Brain Panel

### 6.1 WorkingMemory — `agents/working_memory.py`

Accumulates findings, signals, and reasoning trace for the lifetime of one audit run.

| Field | Contents |
|-------|---------|
| `findings` | Compressed per-agent results (100-200 tokens each via `_compress()`) |
| `raw_results` | Full agent results — NOT sent to LLM |
| `signals` | `Signal` objects: source, type, severity, content, evidence, triggers_action |
| `decisions` | `Decision` objects: timestamp, step, rationale, action_taken |
| `cross_insights` | Cross-agent pattern strings |
| `trace` | Timestamped log entries |
| `meta_synthesis` | pattern, posture, narrative — set after Phase 4 |

**`to_report_dict()`** serializes the above into a flat dict stored as `agentic_meta_json` in DB.

### 6.2 Wiring — Orchestrator → DB → Report

```
_run_pipeline()
  └── wm = WorkingMemory(brand_name, url)
  └── _record(key, result, elapsed, error)
        └── wm.add_finding(key, result)   ← compresses + logs every agent
  └── wm.meta_synthesis = {...}           ← set after cross_findings
  └── _assemble(..., agentic_meta=wm.to_report_dict())

run_all()
  └── saves agentic_meta_json = json.dumps(audit_data["agentic_meta"])

_assemble_audit_data(audit)
  └── am = _parse_json(audit.agentic_meta_json)
  └── returns: agentic_meta, reasoning_trace, signals, cross_insights, decisions, pattern_detected, strategic_posture

generate_audit_report(audit_data)
  └── _render_agentic_brain_section(audit_data)   ← renders if agentic_meta or signals present
  └── injected before </body>
```

### 6.3 Reasoning Brain Panel — `reports/generator.py`

`_render_agentic_brain_section()` renders when `agentic_meta` or `signals` is non-empty:
- Pattern badge (color-coded: `invisible_brand`, `ghost_advertiser`, `social_darling`, etc.)
- Strategic posture badge (`triage` / `optimize` / `accelerate` / `defend`)
- Executive narrative from `meta_synthesis.narrative`
- Signal grid (severity-tagged)
- Cross-agent insights list
- Collapsible reasoning trace + decisions log

---

## 7. Execution Design

```
Phase 0: Prefetch (parallel, zero LLM)       ~5s
  └── homepage scrape + pagespeed

Phase 1: BrandBasicsAgent                    ~18s   sequential

Phase 2: 6 agents, fully parallel:
    ContentCatalogAgent                       ~45s  ┐
    PerformanceAdsAgent                       ~30s  │ wall-clock: ~50s (slowest pair)
    GEOVisibilityAgent                        ~15s  │ Scrapling 90s timeout per scrape
    StoreCROAgent                             ~25s  │
    ResearchAgent                             ~25s  │
    SocialProfileAgent                        ~35s  ┘

Phase 3: SocialMediaAuditAgent               ~25s   sequential (+ 15-20 min/reel if deep_visual)

Phase 4: Synthesis (3 parallel LLM calls)    ~15s
  └── analyst_brief + one_thing + roadmap

Total (standard):                            ~3-4 min
Total (deep_visual, 1 reel):                 ~20 min
```

**Bounded retry:** Each Phase 2 agent has max 2 attempts. Quality gate (`_is_useful()`) is a binary yes/no per agent key — not a score. On fail: wait 3s, retry once, accept whatever result comes back. Never retries forever.

**Abort condition:** If brand URL is completely unreachable (Phase 1 `data_coverage="unavailable"`), all remaining agents are skipped with `status="skipped", skip_reason="brand URL unreachable"`.

---

## 8. Social Audit Skip Logic

```python
ig_data = _nested(results, "social_profile", "instagram") or {}
has_social = bool(ig_data.get("followers") or ig_data.get("bio"))
```

When `has_social=False` (neither followers nor bio available):
- `results["social_media_audit"] = {"status": "skipped", "skip_reason": "social_profile returned no IG data", ...}`
- Report renders a "Social Audit Not Run" card explaining why + prompting re-audit
- Never returns 0 bytes — the template `{% else %}` branch always produces visible HTML

---

## 9. Data Source Attribution

Every data point is tagged with:
- `source`: e.g. `"meta_ad_library"`, `"homepage_scrape"`, `"scrapling"`, `"inferred"`
- `confidence`: `"verified"` (directly scraped) | `"partial"` (some fields missing) | `"inferred"` (LLM/DDG estimate) | `"unavailable"` (blocked/missing)
- `source_url`: clickable link to original source
- `fallback_used`: boolean — whether primary method failed

---

## 10. Scoring Framework

**Two scales, never mixed:**
- Section scores: 1-10 (brand basics, content, ads, GEO, store, research, social)
- Composite scores: 0-100 (overall health, GEO score, virality score)

**Score validation:** `validate_scores()` clamps all values before report generation.

---

## 11. Deep Visual Analysis — `deep_visual` Flag

```
POST /audit body: {"url": "...", "deep_visual": true}
    → run_all(audit_id, deep_visual=True)
    → SocialMediaAuditAgent.run(url, brand_name, deep_visual=True)
    → if deep_visual: _process_reels_tribe(posts)
                        → yt-dlp download → TRIBE v2 inference → brain_map SVG

GET /audit/stream/{id}?deep_visual=1
    → SSE deadline = 7200s (vs 600s default)
```

---

## 12. Virality Predictor

### 12.1 Scoring Dimensions
| Dimension | Weight |
|-----------|--------|
| Emotional trigger | 20% |
| Visual stopping power | 18% |
| Transformation clarity | 17% |
| Social currency | 15% |
| Trend alignment | 12% |
| Share trigger | 10% |
| Hook strength | 8% |

**Grade mapping:** S (85-100), A (70-84), B (55-69), C (40-54), D (0-39)

### 12.2 Pipeline
```
Step 1:  PDP scrape (Scrapling → Playwright)
Step 2:  PIL image analysis
Step 2.5 Kimi K2 vision → emotion, hook strength
Step 2.6 DeepGaze IIE → gaze heatmap
Step 2.7 TRIBE v2 → fMRI parcel activation
Step 3:  DDG social signals
Step 4:  LLM scoring (7 dimensions)
Step 5:  Weighted composite + grade
Step 6:  Chronos trajectory forecast
```

---

## 13. Deployment

- **Platform:** Render.com (free tier, Docker-based)
- **Sleep behaviour:** App sleeps after 15min inactivity, wakes in ~30s
- **DB persistence:** SQLite at `/tmp/shopos.db` — persists within session, reset on redeploy
- **Env vars required:** `GROQ_API_KEY`
- **Env vars optional:** `GEMINI_API_KEY` (tier 3), `NVIDIA_API_KEY` (MiniMax vision), `TRACXN_API_KEY` (funding), `META_AD_LIBRARY_TOKEN` (Graph API), `PAGESPEED_API_KEY` (PSI quota), `TRIBE_CHECKPOINT_DIR` (TRIBE v2 local checkpoint)
- **Docker:** Custom image with Playwright Chromium pre-installed (~800MB)
- **CORS:** Allow-all origins (`*`) for prototype
- **Auto-deploy:** Render rebuilds on every push to main (~8 min build)

---

## 14. Recent Changes (v4 → v5)

| Area | Change |
|------|--------|
| LLM primary | `llama-3.3-70b-versatile` → `moonshotai/kimi-k2-instruct` (131K ctx, better JSON, agentic) |
| LLM fallback | `llama-3.1-8b-instant` → `llama-3.3-70b-versatile` |
| `scrape_pdp()` | Playwright-only → StealthyFetcher → DynamicFetcher → Playwright. Never raises. |
| `scrape_page()` | Added `timeout=90_000, wait=1_500, network_idle=True` to all Scrapling calls |
| `meta_ads.py` | Added `network_idle=True, timeout=90_000, wait=1_500` to `StealthyFetcher.fetch()` |
| `instagram_scraper.py` | `PlayWrightFetcher` (broken) → `DynamicFetcher`. Added `_parse_followers_from_og_desc()`. Scrapling now attempt 2, Playwright attempt 3. |
| `social_profile.py` | DDG follower estimation fallback. `partial` confidence path (bio-only result preserved). |
| `agentic_meta_json` | New `AuditRun` column. `WorkingMemory` now wired into `_run_pipeline` — all agent findings accumulated. Persisted via `run_all()`, read by `_assemble_audit_data()`. |
| DB migration | `_migrate_columns()` now covers `analyst_brief_json`, `cross_findings_json`, `agentic_meta_json`. |
| Skipped SMA | `{% else %}` branch in template — renders "Social Audit Not Run" card instead of 0 bytes. |
| `has_social` gate | Now triggers on `bio` as well as `followers` — partial scrape results no longer silently skip Agent 8. |
