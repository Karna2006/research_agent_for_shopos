# Founder Q&A — Competitive Research Agent
**Prepared for demo | June 2026**

---

## Section A — TRIBE V2 & Tool Decisions

**Q: I mentioned TRIBE v2 in my notes. Did you use it?**

Yes — but in a very specific and honest scope.

TRIBE v2 is Meta's fMRI encoding model trained on naturalistic video/audio stimuli (Friends sitcom + 4 films, Algonauts 2025). It predicts cortical activations across 1,000 brain parcels (Schaefer atlas, Yeo-7 networks) from continuous video/audio streams. It is not a general image model, not a time-series model, and not pip-installable from PyPI.

We integrated it in two contexts:

1. **Virality Predictor (Step 2.7):** Any product video URL (YouTube, Instagram, TikTok) gets downloaded via yt-dlp, run through TRIBE v2 locally, and produces a neural engagement score (0-100) plus per-network activation breakdown. Score = mean absolute z-score activation normalised to 0-100, calibrated at 0.40 z-score = 100.

2. **Agent 8 — Social Media Deep Audit (optional `deep_visual=True`):** Instagram Reels are downloaded via yt-dlp, run through TRIBE v2 (first 9 seconds — configurable via `TRIBE_HOOK_SECONDS`), and the audit report embeds per-reel brain activation heatmaps across the 7 Yeo functional networks.

The 9-second hook window was a deliberate design choice. TRIBE v2's fMRI predictions are most actionable in the first hook window of a reel — the first seconds that either stop the scroll or lose the viewer. Processing the full reel (30-60s) gives marginally more data at 4× the CPU cost.

What we did NOT do: use TRIBE v2 as a time-series forecasting model. That would have been dishonest. For price trajectory and demand forecasting we use Chronos (Amazon, MIT license) — a genuine time-series foundation model designed exactly for sequences. Same feature surface, honest implementation of each model's actual capability.

---

**Q: Why Groq and not OpenAI or Anthropic?**

Two reasons: cost and speed.

OpenAI GPT-4 and Anthropic Claude are paid APIs. A full 8-agent audit makes approximately 8-10 LLM calls. At OpenAI pricing that's ~$0.30-0.50 per audit. At scale (100 audits/day) that's $1,000-1,500/month in LLM costs alone before infrastructure.

In production, we run a three-tier LLM chain: Groq llama-3.3-70b as primary, Groq llama-3.1-8b as fallback when the 70B hits rate limits, and Gemini 2.0 Flash (Google, free tier via `GEMINI_API_KEY`) as a third tier when both Groq tiers are exhausted simultaneously. This three-tier chain means the system self-heals from rate limit events rather than returning empty results. The tier transitions are automatic and transparent to the user.

Groq's free tier gives us Llama 3.1 70B — genuinely capable of structured JSON output — at zero cost for the prototype. At production scale, Groq pricing is approximately $0.05 per full audit. The quality difference for structured data extraction tasks (not creative writing) is negligible.

At the point where we need Claude Sonnet or GPT-4 quality (nuanced strategic analysis, complex reasoning), we add a "premium analysis" mode. The current architecture makes swapping the model a one-line change in `llm/client.py`.

---

**Q: What is Mastra and how does it fit?**

Mastra is an open-source TypeScript agent framework. In this architecture it acts as an optional orchestration layer providing two things the Python backend doesn't have natively: persistent brand memory and workflow visualisation.

When Mastra is running, re-auditing the same brand shows: *"GEO score improved from 45 → 62 since last audit (23 days ago)."* That single sentence turns the tool from a one-shot report into a monitoring system.

When Mastra is not configured, the Python orchestrator handles everything directly. The feature degrades gracefully, not breaks.

---

## Section B — Technical Architecture

**Q: How long does an audit take and why?**

Currently approximately 2-3 minutes for a standard audit. Original sequential design: approximately 5 minutes.

The optimisation is phased parallelism. After Brand Basics validates the brand, 5 independent agents (Content, Ads, GEO, Store, Research) run concurrently. These agents have no cross-dependencies — they only need the brand URL and brand name. Wall-clock time for those 5 agents is ~50s instead of ~2min sequential.

There is one important constraint: we gate concurrent agents at 2 via an `asyncio.Semaphore(2)` at the agent level. Without this gate, we discovered in production that all 5 agents firing LLM calls simultaneously saturated Groq's free-tier RPM limit (30 req/min), causing all agents to receive empty responses and return zero scores. The gate keeps peak throughput at ~27 req/min — safely under the limit. The remaining 3 agents queue and start as slots free up. The total wall-clock cost of the gate is minimal (~5-10s) since agents typically take 15-25s each.

Phase breakdown: ~18s Brand Basics → ~50s 5 parallel agents (gated at 2) → ~40s Social agents → ~20s brain synthesis + roadmap. Total: ~2:30 standard.

---

**Q: What happens when a brand site is blocked by Cloudflare?**

Three-layer fallback, transparent to the user:

1. **Playwright attempt** — headless Chromium with realistic browser headers
2. **httpx fallback** — if Playwright gets a 403 or CF challenge, retry with httpx + BeautifulSoup
3. **Search-only mode** — if both fail, continue using DuckDuckGo search results only

The report section shows a yellow banner: *"⚠️ Homepage scrape blocked — data sourced from search results only."* The Data Sources panel marks that section as `confidence: inferred` instead of `verified`.

The audit never crashes. It always produces a report — the coverage may be partial, but it's clear about what it knows vs. what it couldn't access.

---

**Q: What prevents the LLM from returning empty results under load?**

Three mechanisms working together:

1. **Semaphore gate at the agent level.** `_AGENT_LLM_GATE = asyncio.Semaphore(2)` in the orchestrator ensures at most 2 agents are making LLM calls simultaneously. This caps peak RPM consumption regardless of how many agents are queued in parallel.

2. **Minimum call spacing.** `_MIN_CALL_SPACING = 2.2s` inside the LLM client ensures no two API calls are fired closer than 2.2 seconds apart (even within the allowed 2-concurrent cap). Combined, this gives ~27 req/min sustained throughput versus Groq's 30 req/min limit.

3. **Retry on empty response.** Groq occasionally returns HTTP 200 with an empty response body under heavy quota pressure rather than a 429 error. The LLM client now detects `content == ""` and treats it as a soft rate limit — waits 10s (first retry) or 20s (second retry) then tries again. A higher-level outer retry in `analyze_structured()` adds two more attempts at 25s and 50s spacing. Between these layers, the system self-recovers from transient quota pressure rather than propagating zeros into the report.

We discovered this the hard way: a production audit of orangesugar.in returned all-zeros because all 5 parallel agents got empty LLM responses simultaneously and we had no retry logic for that case.

---

**Q: How do you prevent the LLM from hallucinating metrics?**

Three mechanisms:

1. **Explicit instruction in every prompt:** "Do not hallucinate metrics. If a field is unavailable, mark it as 'Not found publicly.' Never invent a revenue figure, follower count, or score."

2. **Confidence tagging:** Every data point has a `confidence` field — "verified" means scraped directly, "inferred" means LLM interpretation. Users see the difference in the report's Data Sources panel.

3. **Score validation:** `validate_scores()` runs before every report render. Any score outside the valid range (1-10 or 0-100) is clamped and logged. This catches LLMs that occasionally output "7.8/100" or "85/10."

We can't prevent hallucination entirely, but we can make it transparent and bounded.

---

**Q: How is the scoring calibrated? Are a 6.2 and 7.8 meaningfully different?**

Yes, because every LLM scoring prompt includes an explicit rubric:

- 10: Industry-best, benchmark for the category
- 8-9: Strong, clear competitive advantage
- 6-7: Solid, above average for Indian D2C
- 5: Category average — neither strength nor weakness
- 3-4: Below average, noticeable gaps
- 1-2: Critical weakness, urgent fix required

The rubric also includes: *"Score 5 is average. Most brands score 4-7. Only exceptional execution earns 8+. A brand with basic PDPs and no UGC is a 4, not a 7."*

Without this instruction, LLMs trend toward generous scoring (most things end up 7-8). The rubric forces distribution. A 6.2 vs 7.8 in PDP quality is a meaningful gap — the report shows it in context: *"Your score is above the Indian D2C average of 5.8 but below the top 10% threshold of 8.1."*

---

**Q: How does the comparison report work?**

Both brands are audited in parallel (or pulled from cache if recently run). Then a dedicated LLM call receives the full audit context of both brands — not just scores, but actual PDP copy, ad headlines, schema gaps, competitor names, positioning statements.

The comparison output includes:

- **Dimension verdicts:** Who wins each category and specifically why, citing actual data
- **Steal This:** 3 concrete tactics each brand should copy from the other, with implementation steps
- **Customer Journey Battleground:** Who wins at Awareness, Consideration, Conversion, Retention — with specific evidence
- **SWOT:** Data-driven, every item cites an actual score or finding. No generic advice.
- **Strategy:** 90-day battle plan for either brand, prioritised by impact × ease of closing the gap

The earlier version was vague because it passed only 6 numbers to the LLM. The current version passes the full structured audit JSON for both brands — the LLM can reference actual copy, actual ad counts, actual schema status.

---

## Section C — Business & Product

**Q: What does this cost to run?**

| Component | Cost |
|-----------|------|
| Groq LLM (free tier) | $0 for ~100 audits/day |
| Playwright scraping | $0 (self-hosted) |
| PageSpeed API | $0 (free public API) |
| DuckDuckGo search | $0 (no API key needed) |
| NVIDIA NIM vision (MiniMax VL-01) | $0 (free during promo, `build.nvidia.com`) |
| Render hosting | $0 (free tier) |
| SQLite / Neon (free tier) | $0 |
| **Total current cost** | **$0** |

At scale (1,000 audits/day): Groq at $0.05/audit = $50/day = $1,500/month. The main cost is infrastructure and a paid Groq tier. Still an order of magnitude cheaper than OpenAI. (Cost basis: 8 agents per full audit.)

---

**Q: What's the accuracy of the audit compared to a human analyst doing it manually?**

For quantitative data (PageSpeed score, schema presence, ad count, CTA button detection): **more accurate** than manual — it's systematic, not subject to analyst fatigue or inconsistency.

For qualitative analysis (positioning assessment, copywriting quality, strategic recommendations): **strong starting point**, not a replacement. The LLM occasionally misinterprets brand intent, especially for niche categories it wasn't trained heavily on.

The honest positioning: this produces in 2 minutes what would take an analyst 2-4 hours to compile. The analysis quality is comparable to a junior analyst's first pass, with the consistency of a machine. A senior strategist reviewing and annotating the output adds significant value — but the tool eliminates 80% of the data-gathering time.

---

**Q: What breaks at scale?**

Three known failure modes:

1. **Playwright blocks (~15% of sites):** Cloudflare-protected sites reject automated browsers. Handled with search-only fallback. At scale, a rotating proxy service (Brightdata, ~$0.001/request) eliminates most of this.

2. **Groq rate limits at high volume:** Free tier: 30 requests/minute. With 5 parallel agents running concurrently, this was our first production bug — all-zeros results when burst traffic exceeded the RPM limit. Fixed with agent-level semaphore + minimum call spacing + retry-on-empty. At scale, paid Groq tier removes this entirely.

3. **SQLite under concurrent writes:** Fine for the prototype. PostgreSQL (Neon free tier already configured) handles production load without changes to the application code.

---

**Q: What's the path to a commercial product?**

The prototype demonstrates the core value loop. The natural commercial progression:

**V2 — Agency tier:** White-label reports with agency logo. API access (`POST /api/audit` with API key). Charge per audit ($10-50 depending on depth).

**V3 — Brand subscription:** Brands connect their Shopify store (Shopify Admin API). Weekly monitoring with score deltas. Competitive alerts when rivals change pricing or launch SKUs. $200-500/month subscription.

**V4 — Platform:** Multiple brands monitored. Benchmark database grows from our audit history. Brands see how they rank against the category, not just against one competitor. This is the defensible moat.

---

**Q: What's something you decided NOT to build and why?**

Three things:

1. **Real-time Meta Ads data (Tier 2):** The spec mentioned connecting Meta Ads Manager for live performance data. This requires OAuth, user permissions, and Meta App Review. For a prototype, the public Ad Library gives 80% of the insight. Tier 2 is a logical V2 upgrade.

2. **Scheduled weekly cron audits:** The spec mentioned automated weekly monitoring. We implemented the data model and score history tracking, but made re-audits manual (user clicks "Re-audit"). Automated cron requires either a paid background job service or significant infrastructure. The value is proven first; automation follows.

3. **WeasyPrint PDF export:** The spec required PDF download. WeasyPrint had rendering issues with dark-theme CSS and complex layouts. We replaced with browser print-to-PDF (`window.print()` with `@media print` CSS) — same result, zero dependencies.

Each decision prioritised demonstrating value over building infrastructure.

---

## Section D — Demo Questions

**Q: Can I give you a brand right now and you run it?**

Yes. Any public brand URL. Brands with Shopify storefronts give the richest results (Shopify theme and product JSON are structured). Custom platforms work but may have less PDP data.

Suggested live demo brands: any Indian D2C you're familiar with so you can validate the findings. Manyavar, Bombay Shaving Company, WOW Skin Science, Wakefit — all work well.

---

**Q: What's the one thing you'd fix before giving this to real brands?**

Source data quality. Currently, the Research agent's competitive intelligence is sourced from DuckDuckGo search — which gives good coverage but limited depth for niche brands. The meaningful upgrade is connecting to a structured data source: SimilarWeb for traffic signals, or a review aggregator for sentiment velocity over time.

The architecture already has the integration point (`DataResult` wrapper, fallback chain) — it's a data source swap, not an architectural change.

---

**Q: How would you explain the virality predictor to a brand?**

*"Think of it like a pre-flight checklist for your content. Before you spend money shooting a campaign or paying a creator, you paste in your product description. The tool tells you which of the 7 psychological triggers your product hits — emotional resonance, transformation clarity, social currency, and so on — and which it misses. If your product scores a C on 'visual stopping power,' you know the brief needs to address that before anything gets produced. It also generates the exact opening line for a video that would stop the scroll — a 'killer hook' you can hand directly to a creator."*

---

**Q: What's the single most impressive thing in the system technically?**

The progressive disclosure pattern during the audit. As each agent completes — in parallel — it immediately streams a first finding to the user. By the time the full report is ready, the user has already seen 6 individual insights appear one by one. The psychological effect is that the 2-minute wait feels like 30 seconds of progressive revelation rather than a loading screen.

Combined with the "Did you know?" ecommerce insights panel rotating in the background, users are actively reading relevant content during the wait. We measured this instinctively — nobody wants to watch a progress bar for 2 minutes, but everyone will read interesting industry facts.
