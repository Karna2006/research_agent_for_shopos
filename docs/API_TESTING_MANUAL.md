# SHOPOS API Testing Manual

**Base URL:** `http://localhost:8000`  
**Swagger UI:** `http://localhost:8000/api/docs`  
**Start server:** `source .venv/bin/activate && uvicorn main:app --host 0.0.0.0 --port 8000 --reload`

**Internal API header** (required for `/internal/*` endpoints):  
`X-Internal-Secret: dev-internal-key-change-in-prod`

---

## Testing Order

Run in this sequence — later endpoints depend on IDs from earlier ones.

```
1. health / status          → confirm server alive
2. POST /audit              → get audit_id
3. GET /audit/stream/{id}   → watch live progress
4. GET /report/{id}         → verify full report
5. GET /brands              → confirm brand saved
6. POST /compare            → get compare_id
7. POST /virality           → get run_id
8. POST /action-plan        → quick LLM test
9. Internal endpoints       → individual agent tests
10. Admin / cache endpoints  → maintenance
```

---

## 1. Health & Status

### GET `/health`
No input.

**Expected:**
```json
{
  "status": "ok",
  "version": "0.3.0"
}
```

---

### GET `/status`
No input.

**Expected:**
```json
{
  "api": "ok",
  "database": "sqlite",
  "cache": "in-memory",
  "groq": "ok",
  "gemini": "ok (fallback ready)",
  "playwright": "ok",
  "mastra": "not configured",
  "tribe_v2": "loaded"
}
```

**Red flags:**
- `groq: "error: no GROQ_API_KEY"` → `.env` not loaded
- `playwright: "not installed"` → run `playwright install chromium`
- `tribe_v2: "checkpoint needed"` → TRIBE weights not downloaded (non-blocking)

---

## 2. Core Audit Flow

### POST `/audit`

**Input:**
```json
{
  "brand_url": "https://rarerabbit.in",
  "deep_visual": false,
  "scheduled": false
}
```

> `deep_visual: true` enables TRIBE v2 Reels fMRI — slow, skip for quick tests.

**Expected:**
```json
{
  "audit_id": 1,
  "report_url": "/report/1",
  "stream_url": "/audit/stream/1",
  "status": "queued",
  "from_cache": false,
  "orchestrator": "python"
}
```

**Save `audit_id` — used in every subsequent endpoint.**

**If `from_cache: true`:** status will be `"cached"`, no `orchestrator` field. Report is instant.

---

### GET `/audit/stream/{audit_id}`

**Path param:** `audit_id = 1`

**Expected:** Server-Sent Events stream. In Swagger, click Execute and watch the response body fill in real time:

```
data: {"type":"progress","agent":"brand_basics","status":"running","pct":10}
data: {"type":"progress","agent":"__parallel__","status":"running","pct":20}
data: {"type":"progress","agent":"content_catalog","status":"done","pct":30}
data: {"type":"progress","agent":"performance_ads","status":"done","pct":40}
data: {"type":"progress","agent":"geo_visibility","status":"done","pct":50}
data: {"type":"progress","agent":"store_cro","status":"done","pct":60}
data: {"type":"progress","agent":"research","status":"done","pct":70}
data: {"type":"progress","agent":"social_profile","status":"done","pct":80}
data: {"type":"progress","agent":"social_media_audit","status":"done","pct":90}
data: {"type":"progress","agent":"__synthesis__","status":"done","pct":100}
data: {"type":"complete","audit_id":1}
```

**LangGraph node order to confirm:**
1. `brand_basics` (phase 1)
2. `__parallel__` sentinel (phase 2 starts)
3. 6 core agents in any order (phase 2 parallel)
4. `social_media_audit` (phase 3, only if IG detected)
5. `__synthesis__` (phase 4)

**Red flags:**
- Stream stops mid-way → agent crashed, check server logs
- `social_media_audit` missing → no IG presence found (not a bug)

---

### GET `/audit/{audit_id}/tribe-status`

**Path param:** `audit_id = 1`

**Expected:**
```json
{
  "tribe_status": "none",
  "reels_count": 0
}
```

If `deep_visual=true` was used:
```json
{
  "tribe_status": "complete",
  "reels_count": 3
}
```

---

### GET `/report/{audit_id}`

**Path param:** `audit_id = 1`

**Expected:** Full HTML report rendered in browser. In Swagger, response body is raw HTML.

Check for presence of all 8 sections:
- Brand Basics
- Content & Catalog
- Performance & Ads
- GEO & AI Visibility
- Store & CRO
- Competitive Research
- Social & Brand Presence
- Social Media Deep Audit

---

### GET `/report/section/{audit_id}/{agent_name}`

**Test each agent section individually.**

| `agent_name` | What it returns |
|---|---|
| `brand_basics` | Platform, pricing tier, founding year HTML card |
| `content_catalog` | PDP quality score, product list HTML |
| `performance_ads` | Ad intelligence, hook strength HTML |
| `geo_visibility` | GEO score, schema gaps HTML |
| `store_cro` | PageSpeed scores, CRO fixes HTML |
| `research` | Competitor landscape, whitespace HTML |
| `social_profile` | IG/YT/Twitter followers HTML |
| `social_media_audit` | Engagement rate, content themes HTML |

**Expected:** HTML fragment for that agent's section.  
**If agent failed/skipped:** returns a greyed-out "unavailable" card (not a 500).

---

## 3. Share Links

### GET `/share/{token}`

**Path param:** use `share_token` from `/brands` response.

**Expected:** Standalone shareable HTML report (no sidebar, no auth).

---

### GET `/share/compare/{token}`

**Path param:** use `compare_share_token` from compare run.

**Expected:** Standalone comparison HTML.

---

## 4. Compare Flow

### POST `/compare`

**Input:**
```json
{
  "url_a": "https://rarerabbit.in",
  "url_b": "https://bewakoof.com"
}
```

**Expected:**
```json
{
  "compare_id": 1,
  "stream_url": "/compare/stream/1",
  "cache_hit_a": false,
  "cache_hit_b": false
}
```

**Save `compare_id`.**

---

### GET `/compare/stream/{compare_id}`

**Path param:** `compare_id = 1`

**Expected:** SSE stream showing both audits running in parallel, then comparison synthesis.

---

### GET `/compare/{compare_id}`

**Path param:** `compare_id = 1`

**Expected:** Full side-by-side HTML comparison report.  
**If not complete yet:** returns `202` with a loading page.

---

### POST `/compare/{compare_id}/swot`

**Path param:** `compare_id = 1` (must be complete)  
No request body.

**Expected:**
```json
{
  "brand_a_swot": {
    "strengths": ["..."],
    "weaknesses": ["..."],
    "opportunities": ["..."],
    "threats": ["..."]
  },
  "brand_b_swot": {
    "strengths": ["..."],
    "weaknesses": ["..."],
    "opportunities": ["..."],
    "threats": ["..."]
  }
}
```

---

### POST `/strategy`

**Input:** (requires completed `compare_id`)
```json
{
  "brand": "a",
  "compare_id": 1,
  "goal": "outperform the competitor"
}
```

`brand` must be `"a"` or `"b"`.

**Expected:**
```json
{
  "strategy": {
    "90_day_plan": "...",
    "quick_wins": ["..."],
    "competitive_edges": ["..."]
  }
}
```

---

## 5. Virality Predictor

### POST `/virality`

**Option A — by URL:**
```json
{
  "url": "https://rarerabbit.in/products/some-tshirt"
}
```

**Option B — by product description:**
```json
{
  "product_name": "Rare Rabbit Classic Polo",
  "description": "Premium cotton polo shirt with embroidered logo, available in 12 colors",
  "category": "menswear"
}
```

At least one of `url`, `product_name`, or `description` required.

**Expected:**
```json
{
  "run_id": 1,
  "virality_card_url": "/virality/1/report",
  "score": 72,
  "tier": "High Potential",
  "dimensions": {
    "visual_appeal": 8.1,
    "emotional_resonance": 7.4,
    "shareability": 6.9,
    "trend_alignment": 7.2,
    "brand_clarity": 8.0,
    "hook_strength": 7.5,
    "platform_fit": 6.8
  },
  "one_liner": "Strong visual brand, needs sharper hook in first 2 seconds",
  "brain_map_svg": "<svg>...</svg>"
}
```

---

### GET `/virality/{run_id}/report`

**Path param:** `run_id = 1`

**Expected:** Full virality HTML card with brain activation heatmap at top.

---

## 6. Action Plan

### POST `/action-plan`

**Input:**
```json
{
  "finding": "Mobile PageSpeed score is 41/100 — hero image is 2.1MB uncompressed",
  "brand_name": "Rare Rabbit",
  "platform": "shopify",
  "audit_id": 1
}
```

`platform` options: `shopify`, `woocommerce`, `custom`

**Expected:**
```json
{
  "title": "Fix: Compress hero image and enable lazy loading",
  "steps": [
    {
      "step": 1,
      "action": "Go to Shopify Admin → Online Store → Themes → Edit code",
      "detail": "Find the hero image section in your theme",
      "time": "5 min"
    },
    {
      "step": 2,
      "action": "Replace hero image with WebP version < 200KB",
      "detail": "Use squoosh.app to compress. Target: < 200KB WebP",
      "time": "15 min"
    }
  ],
  "expected_impact": "+8-12% mobile conversion rate",
  "effort": "30 minutes",
  "tools": ["squoosh.app", "Shopify Theme Editor"]
}
```

---

## 7. Brands & Monitoring

### GET `/brands`

No input.

**Expected:**
```json
{
  "brands": [
    {
      "url": "https://rarerabbit.in",
      "audit_id": 1,
      "last_audited": "2026-06-15T10:00:00+00:00",
      "monitoring": false,
      "overall_score": 68,
      "content_score": 72,
      "geo_score": 45,
      "store_score": 61,
      "share_token": "abc123xyz"
    }
  ]
}
```

---

### PATCH `/audit/{audit_id}/monitoring`

**Path param:** `audit_id = 1`  
No request body.

**Expected (first call — enables monitoring):**
```json
{
  "audit_id": 1,
  "monitoring": true
}
```

**Expected (second call — disables):**
```json
{
  "audit_id": 1,
  "monitoring": false
}
```

---

## 8. Cache Management

### GET `/cache/status`

No input.

**Expected:**
```json
{
  "backend": "in-memory"
}
```

If Redis configured: `"backend": "upstash"`

---

### DELETE `/cache/clear/{audit_id}`

**Path param:** `audit_id = 1`

**Expected:**
```json
{
  "invalidated": "audit:https://rarerabbit.in",
  "url": "https://rarerabbit.in"
}
```

---

## 9. Video Analysis (TRIBE v2)

### POST `/analyze-video`

Requires TRIBE v2 weights downloaded. Slow (~2-5 min on CPU).

**Input:**
```json
{
  "video_url": "https://www.instagram.com/reel/SHORTCODE/",
  "label": "Rare Rabbit Summer Campaign"
}
```

Supported: Instagram Reels, YouTube, TikTok, Twitter/X, direct `.mp4` URLs.

**Expected:**
```json
{
  "label": "Rare Rabbit Summer Campaign",
  "score": 0.74,
  "tier": "High",
  "network_scores": {
    "visual": 0.81,
    "auditory": 0.62,
    "default_mode": 0.55,
    "frontoparietal": 0.70,
    "limbic": 0.68,
    "somatomotor": 0.49,
    "ventral_attention": 0.73
  },
  "brain_map_svg": "<svg>...</svg>",
  "is_real_tribe": true
}
```

---

## 10. Internal Endpoints

> Require header: `X-Internal-Secret: dev-internal-key-change-in-prod`  
> In Swagger: click **Authorize** → add this header.

### POST `/internal/scrape/homepage`

**Input:**
```json
{
  "url": "https://rarerabbit.in",
  "brand_name": "Rare Rabbit"
}
```

**Expected:** Raw scraped homepage data (title, meta, links, text, images).

---

### POST `/internal/agent/brand_basics`

**Input:**
```json
{
  "url": "https://rarerabbit.in",
  "brand_name": "Rare Rabbit"
}
```

**Expected:** Full `brand_basics` agent result JSON (same as stored in DB).

---

### POST `/internal/agent/content`

**Input:**
```json
{
  "url": "https://rarerabbit.in",
  "brand_name": "Rare Rabbit"
}
```

**Expected:** `content_catalog` agent result with PDP scores, product list.

---

### POST `/internal/agent/ads`

**Input:**
```json
{
  "url": "https://rarerabbit.in",
  "brand_name": "Rare Rabbit"
}
```

**Expected:** `performance_ads` agent result with Meta Ad Library data.

---

### POST `/internal/agent/geo`

**Input:**
```json
{
  "url": "https://rarerabbit.in",
  "brand_name": "Rare Rabbit"
}
```

**Expected:** `geo_visibility` agent result with GEO score, schema gaps.

---

### POST `/internal/agent/store`

**Input:**
```json
{
  "url": "https://rarerabbit.in",
  "brand_name": "Rare Rabbit"
}
```

**Expected:** `store_cro` agent result with PageSpeed scores, CRO analysis.

---

### POST `/internal/agent/research`

**Input:**
```json
{
  "url": "https://rarerabbit.in",
  "brand_name": "Rare Rabbit"
}
```

**Expected:** `research` agent result with competitors, whitespace, strategic recs.

---

### PUT `/internal/audit/{audit_id}/progress`

Used by orchestrator to update DB mid-audit. Test only to verify DB write works.

**Path param:** `audit_id = 1`

**Input:**
```json
{
  "agent_key": "brand_basics",
  "status": "done",
  "result": {"test": true},
  "error": null
}
```

**Expected:**
```json
{
  "ok": true
}
```

---

### PUT `/internal/audit/{audit_id}/complete`

**Path param:** `audit_id = 1`

**Input:**
```json
{
  "one_thing": "Fix mobile PageSpeed — add WebP hero image",
  "roadmap": {"week_1": []},
  "analyst_brief": {},
  "cross_findings": [],
  "agentic_meta": {}
}
```

**Expected:**
```json
{
  "ok": true
}
```

---

## 11. Demo & Utilities

### GET `/demo`

No input.

**Expected:** Full HTML report pre-loaded with cached RareRabbit demo data. Instant (no LLM calls).

---

### GET `/demo/virality`

No input.

**Expected:**
```json
{
  "demo": true,
  "score": 74,
  "tier": "High Potential",
  "dimensions": {...},
  "brain_map_svg": "<svg>...</svg>"
}
```

---

### GET `/brain-map`

No input.

**Expected:** Standalone HTML page showing a 7-network fMRI brain activation SVG heatmap.

---

### GET `/`

No input.

**Expected:** Main application UI HTML (the full single-page frontend).

---

## 12. Admin

### POST `/admin/backfill-roadmaps`

Regenerates roadmaps for all complete audits that don't have one. Use if you ran audits before roadmap generation was added.

No input body.

**Expected:**
```json
{
  "processed": 3,
  "skipped": 1,
  "errors": 0
}
```

---

## Common Error Responses

| Status | Meaning | Fix |
|---|---|---|
| `422` | Validation error — bad input | Check required fields, URL must start with `http://` |
| `404` | ID not found | Wrong `audit_id` / `compare_id` / `run_id` |
| `409` | Resource not ready | Audit/compare not complete yet — wait for stream to finish |
| `500` | Agent or LLM error | Check server logs for traceback |
| `403` | Missing internal secret | Add `X-Internal-Secret` header |

---

## Quick Test Sequence (copy-paste)

```bash
# 1. Health
curl http://localhost:8000/health

# 2. Start audit
curl -X POST http://localhost:8000/audit \
  -H "Content-Type: application/json" \
  -d '{"brand_url": "https://rarerabbit.in"}'

# 3. Check status (replace 1 with your audit_id)
curl http://localhost:8000/audit/1/tribe-status

# 4. Get brands list
curl http://localhost:8000/brands

# 5. Virality by description (fast — no scraping)
curl -X POST http://localhost:8000/virality \
  -H "Content-Type: application/json" \
  -d '{"product_name": "Rare Rabbit Polo", "description": "Premium cotton polo, 12 colors"}'

# 6. Action plan
curl -X POST http://localhost:8000/action-plan \
  -H "Content-Type: application/json" \
  -d '{"finding": "Mobile PageSpeed 41/100", "brand_name": "Rare Rabbit", "platform": "shopify"}'
```
