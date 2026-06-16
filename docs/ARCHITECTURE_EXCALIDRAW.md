# SHOPOS Brand Audit Agent — Architecture for Excalidraw
**Feed this file to Claude to generate an Excalidraw flow diagram.**

---

## HOW TO READ THIS DOCUMENT

Each section below is a layer of the diagram.
Draw them top to bottom, left to right.
Boxes = components. Arrows = data flow. Swimlanes = phases.

---

## LAYER 1 — ENTRY POINT

```
[ Browser / API Client ]
        |
        | POST /audit { url, deep_visual }
        ↓
[ FastAPI Server (port 8000) ]
        |
        | spawns background task
        ↓
[ Agentic Orchestrator ]        ← this is the brain
        |
        | reads/writes throughout
        ↓
[ SQLite / PostgreSQL DB ]      ← AuditRun row created immediately
```

---

## LAYER 2 — LIVE FEEDBACK LOOP (runs in parallel with pipeline)

```
[ Browser ]
    ↑  ↑  ↑
    |  |  |  SSE events: agent_started, agent_done, complete
    |  |  |
[ GET /audit/stream/{id} ]     ← Server-Sent Events endpoint
    |
    | polls DB every 0.5s
    ↓
[ AuditRun.progress_json ]     ← updated by orchestrator as each agent finishes
```

*This is how the UI shows agents completing in real-time — no websocket needed.*

---

## LAYER 3 — THE 5-PHASE PIPELINE

Draw as a vertical swimlane with 5 horizontal bands.

```
┌─────────────────────────────────────────────────────────────┐
│ PHASE 0 — PREFETCH (parallel, zero LLM calls, ~5s)          │
│                                                             │
│   [ Homepage Scrape ]         [ PageSpeed API ]             │
│   ★ SCRAPLING StealthyFetcher  Google PSI mobile+desktop    │
│     Cloudflare bypass          → cached 10min               │
│     → cached for all agents                                 │
└─────────────────────────────────────────────────────────────┘
         |
         ↓
┌─────────────────────────────────────────────────────────────┐
│ PHASE 1 — BRAND BASICS (sequential, ~18s)                   │
│                                                             │
│   [ Agent 1: BrandBasicsAgent ]                             │
│   ★ SCRAPLING StealthyFetcher → homepage                    │
│   DDG search + Wayback Machine + Shopify /products.json     │
│   Output: brand_name, founding_year, CEO, revenue,          │
│           store_count, category_expansion, domain_variants  │
│                                                             │
│   → result written to DB                                    │
│   → context passed to Phase 2 agents                        │
└─────────────────────────────────────────────────────────────┘
         |
         ↓
┌─────────────────────────────────────────────────────────────┐
│ PHASE 2 — CORE AUDIT (6 agents fully parallel, ~50s)        │
│                                                             │
│  ┌───────────────────┐  ┌───────────────────┐               │
│  │ Agent 2           │  │ Agent 3           │               │
│  │ Content & Catalog │  │ Performance Ads   │               │
│  │                   │  │                   │               │
│  │ ★ SCRAPLING        │  │ ★ SCRAPLING        │               │
│  │   StealthyFetcher  │  │   StealthyFetcher  │               │
│  │   DynamicFetcher   │  │   → Meta Ads Lib  │               │
│  │   → PDP scrape    │  │   Playwright fbk  │               │
│  │   copy quality    │  │   hook score      │               │
│  │   rewrites        │  │   funnel coverage │               │
│  └───────────────────┘  └───────────────────┘               │
│                                                             │
│  ┌───────────────────┐  ┌───────────────────┐               │
│  │ Agent 4           │  │ Agent 5           │               │
│  │ GEO Visibility    │  │ Store & CRO       │               │
│  │                   │  │                   │               │
│  │ ★ SCRAPLING        │  │ ★ SCRAPLING        │               │
│  │   StealthyFetcher  │  │   StealthyFetcher  │               │
│  │   → schema audit  │  │   → storefront    │               │
│  │   AI citation     │  │   PageSpeed API   │               │
│  │   DDG simulation  │  │   UX audit        │               │
│  │                   │  │   WhatsApp/CRM    │               │
│  └───────────────────┘  └───────────────────┘               │
│                                                             │
│  ┌───────────────────┐  ┌───────────────────┐               │
│  │ Agent 6           │  │ Agent 7           │               │
│  │ Competitive Res.  │  │ Social & Brand    │               │
│  │                   │  │ Presence          │               │
│  │ DDG + Trends      │  │ ★ SCRAPLING        │               │
│  │ + Tracxn          │  │   DynamicFetcher  │               │
│  │ competitors       │  │   → Instagram     │               │
│  │ threat level 🔴🟡🟢 │  │   og:description  │               │
│  │ omnichannel gaps  │  │   follower parse  │               │
│  └───────────────────┘  └───────────────────┘               │
│                                                             │
│   all 6 write to DB as they complete → SSE fires per agent  │
└─────────────────────────────────────────────────────────────┘
         |
         ↓ (conditional — skipped if no Instagram found)
┌─────────────────────────────────────────────────────────────┐
│ PHASE 3 — SOCIAL DEEP AUDIT (sequential, ~25s or ~20min)    │
│                                                             │
│   [ Agent 8: SocialMediaAuditAgent ]                        │
│                                                             │
│   ★ SCRAPLING DynamicFetcher → Instagram posts              │
│   yt-dlp → Reel video download                              │
│                                                             │
│   Standard path (~25s):                                     │
│   ★ MINIMAX VL-01 (NVIDIA NIM)                               │
│     multimodal vision → post image analysis                 │
│     style consistency, content type, aesthetic quality      │
│     Fallback: Llama 4 Scout (Groq)                          │
│                                                             │
│   deep_visual=True path (~20min/reel):                      │
│   ★ META TRIBE v2 (fMRI encoding model)                      │
│     local inference on downloaded Reels                     │
│     predicts cortical activation across                     │
│     1,000 Schaefer brain parcels                            │
│     → (n_TRs × 1000) z-score matrix                        │
│     → 7 Yeo network activation scores                       │
│     → brain activation heatmap SVG (brain_map.py)          │
│     → per-reel attention timeline in report                 │
│                                                             │
│   "Which frames trigger the most neural engagement?         │
│    Where do viewers mentally check out?"                    │
└─────────────────────────────────────────────────────────────┘
         |
         ↓
┌─────────────────────────────────────────────────────────────┐
│ PHASE 4 — SYNTHESIS (3 parallel LLM calls, ~15s)            │
│                                                             │
│  [ analyst_brief ]  [ one_thing ]  [ 30-day roadmap ]       │
│  strategic verdict  top priority   week-by-week plan        │
│                                                             │
│  + WorkingMemory cross-agent synthesis runs here:           │
│  → detects patterns across all 8 agents                     │
│  → assigns strategic posture                                │
│  → writes narrative                                         │
└─────────────────────────────────────────────────────────────┘
```

---

## LAYER 4 — WORKING MEMORY (cross-cutting, not a phase)

Draw as a vertical sidebar running alongside all 5 phases.

```
┌──────────────────────────────────────┐
│  WorkingMemory (agents/working_      │
│  memory.py)                          │
│                                      │
│  Accumulates across entire run:      │
│  ─ findings[]     per-agent summary  │
│  ─ signals[]      severity-tagged    │
│  ─ decisions[]    timestamped        │
│  ─ cross_insights pattern strings    │
│  ─ trace[]        step-by-step log   │
│  ─ meta_synthesis pattern + posture  │
│                                      │
│  Every agent calls:                  │
│  wm.add_finding(key, result)         │
│                                      │
│  After Phase 4:                      │
│  wm.meta_synthesis = {               │
│    pattern: "ghost_advertiser",      │
│    posture: "triage",                │
│    narrative: "..."                  │
│  }                                   │
│                                      │
│  Persisted as agentic_meta_json      │
│  in DB → powers Reasoning Brain      │
│  section in report                   │
└──────────────────────────────────────┘
```

---

## LAYER 5 — REPORT GENERATION

```
[ GET /report/{id} ]
        |
        ↓
[ reports/generator.py ]
        |
        | reads AuditRun from DB
        | runs validate_scores()
        | builds _build_audit_context()
        |   → 10-dim scorecard
        |   → priority framework (🔴🟡🟢)
        |   → all agent outputs mapped
        |
        | renders Jinja2 template
        |   audit_report.html
        |
        | injects:
        |   → Agentic Brain section
        |   → Sources panel
        |   → Skip Re-audit callout
        |
        ↓
[ Full HTML Report ]

Sections in report (in order):
  01 Brand Basics
  02 Content & Catalog
  03 Performance & Ads
  04 GEO & AI Visibility
  05 Store & CRO  (+ UX audit + Omnichannel signals)
  06 Competitive Research
  07 Social Profile
  08 Social Media Deep Audit
  ── Reels Neural Engagement (TRIBE v2)
  ── Agentic Reasoning Brain
  ── Financial & Brand Snapshot   ← NEW
  ── Competitive Landscape        ← NEW (threat levels)
  ── Priority Action Framework    ← NEW (🔴🟡🟢)
  ── 10-Dimension Scorecard       ← NEW
  ── 30-Day Roadmap
  ── Data Sources Panel
```

---

## LAYER 6 — DATA SOURCES MAP

Draw as a separate reference panel (bottom or side).
★ marks the three headline technologies — give these boxes a distinct color (purple).

```
DATA SOURCES (no API key required unless marked *)

★★★ HEADLINE TECHNOLOGIES ★★★

  ★ SCRAPLING v0.4.8
    StealthyFetcher  → Cloudflare bypass, stealth TLS headers
                       used by: ALL 8 agents for any web page
    DynamicFetcher   → full JS rendering, network idle wait
                       used by: Instagram scrape, JS-heavy PDPs
    Fallback chain:  Scrapling → Playwright → httpx → curl-cffi
    Why it matters:  Modern ecommerce blocks naive scrapers.
                     Scrapling gets through where requests fails.

  ★ MINIMAX VL-01 (via NVIDIA NIM API, free tier)
    Multimodal vision model — reads images, not just text
    Used by: Agent 8 (Social Media Audit)
             → analyses downloaded Instagram post images
             → scores style consistency, aesthetic quality,
               content type mix, brand alignment
    Fallback: Llama 4 Scout (Groq) if NVIDIA key absent
    Why it matters: Text LLMs can't see images.
                    MiniMax tells us what the feed actually looks like.

  ★ META TRIBE v2  (local inference, CC-BY-NC-4.0)
    fMRI encoding model trained on naturalistic video+audio
    Used by: Agent 8 (deep_visual=True) + Virality Predictor
    Input:   Downloaded Reel (yt-dlp) → video file
    Process: → predict() → (n_TRs × 1000 parcels) z-score matrix
             → 7 Yeo functional network activation scores
             → brain_map.py → SVG heatmap timeline
    Output:  "Frame 0:12 — peak attention. Frame 0:28 — drop-off risk."
    Why it matters: No other brand audit tool tells you which
                    10 seconds of a Reel your audience is actually
                    processing vs. mentally scrolling past.

─────────────────────────────────────────────

Standard data sources:

Web scraping:
  StealthyFetcher (Scrapling) ★  → homepage, PDP, Meta Ads Library
  DynamicFetcher  (Scrapling) ★  → JS pages, Instagram profile
  Playwright                     → final fallback (Chromium headless)
  httpx / curl-cffi              → lightweight last resort

Search & intelligence:
  DuckDuckGo                     → brand research, competitors, Reddit
  Google Trends (pytrends)       → trend direction, peak week
  Wayback Machine CDX API        → domain age + longevity
  Tracxn REST API*               → funding stage, investors

Platform-specific:
  Shopify /products.json         → catalog, price range, collections
  Meta Ads Library               → active ads, formats, headlines
  Instagram mobile API           → followers, posts, bio
  Google PageSpeed Insights API* → mobile/desktop scores, CWV
  yt-dlp                         → Reel downloads for TRIBE v2 ★

AI / ML:
  Kimi K2 (OpenRouter)           → primary LLM (131K ctx)
  llama-3.3-70b (Groq)           → LLM fallback tier 2
  Gemini 2.0 Flash               → LLM fallback tier 3
  MiniMax VL-01 (NVIDIA NIM) ★   → post image vision analysis
  TRIBE v2 (Meta, local)      ★  → fMRI neural Reels engagement
  DeepGaze IIE (local)           → visual saliency heatmap (virality)
  Chronos (Amazon, local)        → trend forecasting
```

---

## LAYER 7 — LLM FALLBACK CHAIN

Draw as a linear chain with failure conditions.

```
[ Request comes in ]
        |
        ↓
[ Kimi K2 via OpenRouter ]  ← primary, 131K context
        |
        | fails / rate limit / empty response
        ↓
[ llama-3.3-70b via Groq ]  ← tier 2, resets after 90s
        |
        | fails / rate limit
        ↓
[ Gemini 2.0 Flash ]        ← tier 3, separate quota, resets after 120s
        |
        | fails
        ↓
[ Return partial result ]   ← never raises, always returns DataResult
```

---

## LAYER 8 — EXTENSIBILITY (THE KEY FRAMING FOR SHOPOS TEAM)

Draw as a callout box / annotation layer over the diagram.

```
┌─────────────────────────────────────────────────────────────┐
│  DESIGNED TO PLUG INTO ANY DATA SOURCE                      │
│                                                             │
│  Want to add a new data source?                             │
│  → Add a scraper in scrapers/                               │
│  → Call it inside the relevant agent                        │
│  → Pass result to LLM via user_content string               │
│  → Template renders it automatically                        │
│  No orchestrator changes. No schema changes.                │
│                                                             │
│  Want to add a new report section?                          │
│  → Add fields to the relevant prompt in llm/prompts.py      │
│  → Map output in generator.py _build_audit_context()        │
│  → Add Jinja2 block in audit_report.html                    │
│  No new agents. No pipeline changes.                        │
│                                                             │
│  Want to swap the LLM?                                      │
│  → Change 3 lines in llm/client.py                          │
│  → All 8 agents pick it up automatically                    │
│  Zero downstream changes.                                   │
│                                                             │
│  Want to add a 9th agent?                                   │
│  → Create agents/new_agent.py                               │
│  → Add to Phase 2 asyncio.gather() in orchestrator          │
│  → WorkingMemory accumulates it automatically               │
│  → SSE streams it automatically                             │
│  → Report section renders it automatically                  │
│                                                             │
│  SHOPOS team already has:                                    │
│  → Shopify store data (→ feed directly to Agent 5)          │
│  → Meta Marketing API access (→ replace Ads Library scrape) │
│  → First-party brand data (→ skip scraping, inject context) │
│  → Customer behaviour signals (→ new agent in Phase 2)      │
│  This architecture absorbs all of it — no redesign.         │
└─────────────────────────────────────────────────────────────┘
```

---

## EXCALIDRAW LAYOUT INSTRUCTIONS

When generating the diagram, use this spatial layout:

```
TOP:       Entry point + SSE feedback loop (2 boxes, horizontal)

MIDDLE:    5-phase pipeline (vertical swimlane, left 70% of canvas)
           WorkingMemory sidebar (right 30%, spans all 5 phases)

BOTTOM LEFT:   Report generation flow (compact, 4 boxes)
BOTTOM MIDDLE: Data sources map (reference panel, grid layout)
BOTTOM RIGHT:  LLM fallback chain (linear, 4 boxes)

OVERLAY:   Extensibility callout — dashed border, amber color,
           positioned top-right or as a floating annotation
```

**Color coding:**
- Blue  → orchestrator, pipeline phases, core flow
- Green → data sources, scrapers
- Purple → LLM / AI components
- Amber → WorkingMemory, synthesis, reasoning brain
- Red   → fallback paths, skip conditions
- Gray  → DB, cache, infrastructure

**Arrow labels to include:**
- "spawns background task"
- "SSE: agent_done"
- "writes to DB"
- "reads prefetched"
- "context passed forward"
- "skipped if no Instagram"
- "persisted as JSON"
- "renders template"
- "injected before </body>"

---

## ONE-LINE SUMMARY FOR EACH COMPONENT

For use as Excalidraw labels:
★ = headline technology — use purple box, larger font

| Component | Label | Color |
|---|---|---|
| FastAPI | API layer — audit trigger + report serve | blue |
| Agentic Orchestrator | Pipeline coordinator + phase manager | blue |
| WorkingMemory | Cross-agent signal accumulator | amber |
| Phase 0 Prefetch | Homepage + PageSpeed, cached for all agents | gray |
| Agent 1 Brand Basics | Founding story, CEO, revenue, store count | blue |
| Agent 2 Content | PDP quality, copy rewrites, catalog depth | blue |
| Agent 3 Ads | Meta Ads Library, hook score, funnel coverage | blue |
| Agent 4 GEO | Schema audit, AI search simulation | blue |
| Agent 5 CRO | PageSpeed, UX audit, WhatsApp/CRM detection | blue |
| Agent 6 Research | Competitors with threat level, omnichannel | blue |
| Agent 7 Social Profile | Instagram scrape, ad creative intelligence | blue |
| Agent 8 Social Audit | Engagement, content quality → feeds MiniMax + TRIBE | blue |
| ★ SCRAPLING | Cloudflare-bypassing stealth scraper — used by ALL agents | purple |
| ★ MINIMAX VL-01 | Multimodal vision — reads Instagram post images | purple |
| ★ META TRIBE v2 | fMRI neural encoding — frame-by-frame Reel engagement | purple |
| brain_map.py | TRIBE output → 7 Yeo network SVG heatmap | purple |
| DeepGaze IIE | Visual saliency — where eyes look on product images | purple |
| Generator | Jinja2 render, 10-dim scorecard, priority framework | green |
| SSE Stream | Real-time agent progress to browser | gray |
| DB (SQLite/Postgres) | AuditRun, ScoreHistory, share tokens | gray |
| LLM Chain | Kimi K2 → llama-3.3-70b → Gemini 2.0 Flash | amber |
| Priority Framework | All recs → 🔴 Fix now / 🟡 Q3 / 🟢 Long-term | green |
| 10-Dim Scorecard | All agents → 10 dimensions → one composite | green |

---

## EXCALIDRAW ANNOTATION CALLOUTS

Add these as floating sticky-note annotations near the relevant components:

Near SCRAPLING box:
> "Not just scraping — stealth fingerprinting.
>  Bypasses Cloudflare, mimics real Chrome.
>  Without this: Instagram returns 403.
>  Meta Ads Library returns login wall.
>  Modern D2C sites return empty pages."

Near MINIMAX VL-01 box:
> "The only component that can actually
>  SEE the brand's visual identity.
>  Text LLMs describe what we scraped.
>  MiniMax tells us what it looks like."

Near META TRIBE v2 box:
> "Research-grade neuroscience model
>  applied to brand content for the first time.
>  Trained on fMRI data from real humans
>  watching real video.
>  Output: which 10 seconds of your Reel
>  the brain actually processes."
