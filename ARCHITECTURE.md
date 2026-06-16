# SHOPOS Agent Architecture

```mermaid
flowchart TD
    %% ── Entry Points ───────────────────────────────────────
    CLI["🖥 run_audit.py\nCLI entry"]
    API["⚡ FastAPI\n/api/audit"]
    CLI --> OR
    API --> OR
    OR["orchestrator.py\nrun_full_audit / run_all\n(thin delegation wrapper)"]
    OR --> AO

    %% ── Orchestrator ────────────────────────────────────────
    AO["🧠 agentic_orchestrator.py\n_run_pipeline(url, brand_name)"]

    %% ── Phase 0 ─────────────────────────────────────────────
    AO --> P0

    subgraph P0["PHASE 0 — Prefetch  (parallel, zero LLM)"]
        direction LR
        WS1["WebScraper\nscrape_page()"]
        PSP["pagespeed\nget_scores()"]
    end

    P0 --> P1

    %% ── Phase 1 ─────────────────────────────────────────────
    subgraph P1["PHASE 1 — Brand Foundation  (sequential, 1 LLM call)"]
        BB["BrandBasicsAgent\nbrand_basics.run()"]
        BB --> BB1["• WebScraper.scrape_page()"]
        BB --> BB2["• SearchAgent DDG × 2 queries"]
        BB --> BB3["• wayback.get_brand_longevity()"]
        BB --> BB4["• detect_platform() → Shopify?"]
        BB4 -->|"Shopify detected"| BB5["• _fetch_shopify_catalog()\n  /products.json + /collections.json"]
        BB --> BB6["• GroqClient.analyze_structured()"]
    end

    P1 --> ABORT{URL\nreachable?}
    ABORT -->|"No"| DEAD["⛔ All agents skipped\npartial result returned"]
    ABORT -->|"Yes"| P2

    %% ── Phase 2 ─────────────────────────────────────────────
    subgraph P2["PHASE 2 — Core Intelligence  (6 agents fully parallel)"]
        direction TB
        GATE["_AGENT_LLM_GATE\nasyncio.Semaphore(2)\nmax 2 concurrent LLM calls"]

        subgraph RETRY["Bounded Retry per agent  (max 2 attempts)"]
            RUN1["Attempt 1\nagent.run()"]
            QC{"_is_useful()\nbinary check"}
            RUN1 --> QC
            QC -->|"pass"| DONE_A["✅ accept result"]
            QC -->|"fail → sleep 3s"| RUN2["Attempt 2\nagent.run()"]
            RUN2 --> DONE_A
        end

        CC["ContentCatalogAgent\n• WebScraper.scrape_pdp()\n• DDG search\n• GroqClient"]
        PA["PerformanceAdsAgent\n• meta_ads.get_ads()\n• DDG search\n• GroqClient"]
        GEO["GEOVisibilityAgent\n• WebScraper.scrape_page()\n• DDG search × 3\n• GroqClient"]
        CRO["StoreCROAgent\n• WebScraper.scrape_page()\n• pagespeed.get_scores()\n• httpx direct checks\n• GroqClient"]
        RES["ResearchAgent\n• DDG search × 5 queries\n• trends.get_brand_trends()\n• tracxn_researcher\n• GroqClient"]
        SP["SocialProfileAgent\n• instagram_handle_finder\n• instagram_scraper\n• youtube_scraper\n• DDG search\n• GroqClient"]

        GATE --> CC & PA & GEO & CRO & RES & SP
    end

    %% ── Instagram fallback chain ─────────────────────────────
    SP -->|"handle found"| IGF

    subgraph IGF["instagram_scraper.scrape_instagram_profile()  — 3-step fallback"]
        direction LR
        IG1["Step 1\nmobile API\ni.instagram.com\nAndroid UA"]
        IG2["Step 2\nPlaywright\nbrowser render\nog: meta only"]
        IG3["Step 3\nScrapling\nPlayWrightFetcher\nlast resort"]
        IG1 -->|"fail"| IG2
        IG2 -->|"fail"| IG3
    end

    %% ── instagram_handle_finder strategies ──────────────────
    SP --> HF

    subgraph HF["instagram_handle_finder.discover_handle()"]
        direction LR
        S1["_strategy_ig_search\nIG internal search"]
        S2["_strategy_website\nscrape brand site\nog:see_also, bio links"]
        S3["_strategy_ddg\nDDG brand queries"]
        S4["_strategy_linktree\nlinktree.me profile"]
        S1 & S2 & S3 & S4 -->|"candidates"| VAL["_validate_profile()\nIG API check\ntop 8 candidates"]
    end

    %% ── Phase 3 ─────────────────────────────────────────────
    P2 --> CHK{social_profile\nreturned IG data?}
    CHK -->|"No"| SKIP_SMA["⏭ social_media_audit\nskipped"]
    CHK -->|"Yes"| P3

    subgraph P3["PHASE 3 — Social Depth  (conditional, 2 attempts)"]
        SMA["SocialMediaAuditAgent\nsocial_media_audit.run()"]
        SMA --> SMA1["• instagram_scraper\n  posts + reels"]
        SMA --> SMA2["• youtube_scraper\n  scrape_youtube_channel()\n  yt-dlp + yt-api"]
        SMA --> SMA3["• Llama 4 Scout\n  multimodal image analysis\n  up to 8 images"]
        SMA --> SMA4["• TRIBE v2 fMRI\n  NeuralEngagementAnalyzer\n  ≤3 Reels → brain activation\n  heatmaps + brand aggregate"]
        SMA --> SMA5["• GroqClient\n  text analysis"]
    end

    %% ── Phase 4 ─────────────────────────────────────────────
    P3 --> P4
    SKIP_SMA --> P4

    subgraph P4["PHASE 4 — Synthesis  (3 parallel LLM calls, zero per-step LLM)"]
        CVF["_cross_validate_findings()\nrule-based pattern detection\n6 cross-agent patterns\nzero LLM"]
        SYN["_synthesis()\nasyncio.gather × 3"]
        B["analyst_brief\nGroqClient.analyze_structured()\nverdict + top 3 findings"]
        OT["one_thing\nGroqClient.analyze()\nhighest-impact 7-day action"]
        RM["roadmap\nGroqClient.analyze_structured()\n30-day week-by-week plan"]
        CVF --> SYN
        SYN --> B & OT & RM
    end

    %% ── Output ───────────────────────────────────────────────
    P4 --> OUT

    subgraph OUT["📊 Final Report"]
        direction LR
        O1["analyst_brief\nverdict + urgency"]
        O2["one_thing\n25-word action"]
        O3["roadmap\nweek_1/2-3/4"]
        O4["cross_findings[]\n6 patterns"]
        O5["results{}\n8 agent outputs"]
        O6["agent_status[]\nelapsed, quality_ok"]
    end

    %% ── Shared Infrastructure ────────────────────────────────
    subgraph INFRA["Shared Infrastructure"]
        direction LR
        LLM["llm/client.py\nGroqClient\nanalyze()\nanalyze_structured()"]
        SRCH["scrapers/search.py\nSearchAgent\nDuckDuckGo DDG"]
        WS["scrapers/web_scraper.py\nWebScraper\nPlaywright + httpx\nCloudflare detection"]
        DB["db/models.py\nAuditRun\nAGENT_SEQUENCE"]
    end
```

## Agent Summary

| # | Agent | Key Tools | LLM Model | Quality Signal |
|---|-------|-----------|-----------|----------------|
| 1 | BrandBasicsAgent | WebScraper, DDG, Wayback, Shopify API | Groq | `analysis` exists |
| 2 | ContentCatalogAgent | WebScraper (PDPs), DDG | Groq | `analysis` or `pdps_scraped` |
| 3 | PerformanceAdsAgent | meta_ads, DDG | Groq | `analysis` or `ads_scrape` |
| 4 | GEOVisibilityAgent | WebScraper, DDG | Groq | `analysis` |
| 5 | StoreCROAgent | WebScraper, PageSpeed, httpx | Groq | `analysis` or `pagespeed` |
| 6 | ResearchAgent | DDG ×5, Trends, Tracxn | Groq | `analysis` |
| 7 | SocialProfileAgent | IG handle finder, IG scraper, YT scraper, DDG | Groq | `instagram.followers` or `bio` |
| 8 | SocialMediaAuditAgent | IG scraper, YT scraper, Llama 4 Scout, TRIBE v2 | Groq | `engagement_rate` or `content_themes` |

## Scraper Fallback Chains

```
instagram_scraper.scrape_instagram_profile()
  → mobile API (i.instagram.com, Android UA)
  → Playwright render (og: meta, no posts)
  → Scrapling PlayWrightFetcher (last resort)

instagram_handle_finder.discover_handle()
  → IG internal search
  → brand website scrape (og:see_also, bio links)
  → DuckDuckGo brand queries
  → Linktree profile scrape
  → validate top 8 candidates via IG API

WebScraper.scrape_page()
  → Playwright (headless Chrome, Cloudflare detection)
  → httpx fallback (static HTML)
```

## Key Design Principles (Greg Isenberg loop constraints)

- **Bounded retry**: max 2 attempts per agent, binary `_is_useful()` check, not open-ended
- **LLM calls**: 3 total (synthesis only) vs 30+ in old ReAct design
- **Parallel Phase 2**: 6 agents concurrent, gated at `Semaphore(2)` for Groq RPM
- **Hard abort**: brand URL unreachable → skip all 7 downstream agents
- **Conditional Phase 3**: `social_media_audit` skipped if `social_profile` returns no IG data
- **Rule-based cross-validation**: 6 patterns detected without LLM
