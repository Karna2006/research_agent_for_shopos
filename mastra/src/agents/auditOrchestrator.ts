/**
 * Audit Orchestrator Agent
 *
 * An AI agent that can analyze brand audit results and provide strategic
 * commentary — especially useful for comparing against memory of past audits.
 * Actual data collection is done by workflow steps calling Python endpoints.
 */
import { Agent } from "@mastra/core/agent";
import { createTool } from "@mastra/core/tools";
import { createGroq } from "@ai-sdk/groq";
import { z } from "zod";
import { brandMemory } from "../memory/index.js";

const PYTHON_API = process.env.PYTHON_API_URL ?? "http://localhost:8000";
const INTERNAL_KEY = process.env.INTERNAL_SECRET_KEY ?? "dev-internal-key-change-in-prod";

const iHeaders = {
  "Content-Type": "application/json",
  "X-Internal-Key": INTERNAL_KEY,
};

async function callPython(endpoint: string, body: unknown) {
  const res = await fetch(`${PYTHON_API}${endpoint}`, {
    method: "POST",
    headers: iHeaders,
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${endpoint} → HTTP ${res.status}`);
  return res.json() as Promise<unknown>;
}

// ── Tools (execute receives inputData as first arg) ────────────────────────────

const scrapeHomepageTool = createTool({
  id: "scrape-homepage",
  description: "Scrape a brand homepage and return structured content data",
  inputSchema:  z.object({ url: z.string().url() }),
  outputSchema: z.object({ html: z.string().optional(), meta: z.unknown(), blocked: z.boolean() }),
  execute: async ({ url }: { url: string }) =>
    callPython("/internal/scrape/homepage", { url }) as never,
});

const runBrandBasicsTool = createTool({
  id: "run-brand-basics",
  description: "Brand Basics agent — founding story, positioning, pricing tier",
  inputSchema:  z.object({ url: z.string(), brand_name: z.string() }),
  outputSchema: z.object({ agent: z.string(), analysis: z.unknown() }),
  execute: async ({ url, brand_name }: { url: string; brand_name: string }) =>
    callPython("/internal/agent/brand_basics", { url, brand_name }) as never,
});

const runContentAuditTool = createTool({
  id: "run-content-audit",
  description: "Content Catalog agent — PDP quality, headline clarity, CRO rewrites",
  inputSchema:  z.object({ url: z.string(), brand_name: z.string() }),
  outputSchema: z.object({ agent: z.string(), analysis: z.unknown() }),
  execute: async ({ url, brand_name }: { url: string; brand_name: string }) =>
    callPython("/internal/agent/content", { url, brand_name }) as never,
});

const runAdsAuditTool = createTool({
  id: "run-ads-audit",
  description: "Performance & Ads agent — Meta Ad Library, hooks, CTAs, funnel coverage",
  inputSchema:  z.object({ url: z.string(), brand_name: z.string() }),
  outputSchema: z.object({ agent: z.string(), analysis: z.unknown() }),
  execute: async ({ url, brand_name }: { url: string; brand_name: string }) =>
    callPython("/internal/agent/ads", { url, brand_name }) as never,
});

const runGEOAuditTool = createTool({
  id: "run-geo-audit",
  description: "GEO Visibility agent — schema markup, AI citation likelihood",
  inputSchema:  z.object({ url: z.string(), brand_name: z.string() }),
  outputSchema: z.object({ agent: z.string(), analysis: z.unknown() }),
  execute: async ({ url, brand_name }: { url: string; brand_name: string }) =>
    callPython("/internal/agent/geo", { url, brand_name }) as never,
});

const runStoreAuditTool = createTool({
  id: "run-store-audit",
  description: "Store & CRO agent — PageSpeed, conversion friction, cart abandonment",
  inputSchema:  z.object({ url: z.string(), brand_name: z.string() }),
  outputSchema: z.object({ agent: z.string(), analysis: z.unknown() }),
  execute: async ({ url, brand_name }: { url: string; brand_name: string }) =>
    callPython("/internal/agent/store", { url, brand_name }) as never,
});

const runResearchAuditTool = createTool({
  id: "run-research",
  description: "Competitive Research agent — top competitors, positioning gaps, opportunities",
  inputSchema:  z.object({ url: z.string(), brand_name: z.string() }),
  outputSchema: z.object({ agent: z.string(), analysis: z.unknown() }),
  execute: async ({ url, brand_name }: { url: string; brand_name: string }) =>
    callPython("/internal/agent/research", { url, brand_name }) as never,
});

// ── Agent ─────────────────────────────────────────────────────────────────────

const groq = createGroq({ apiKey: process.env.GROQ_API_KEY });

export const auditOrchestratorAgent = new Agent({
  id:   "audit-orchestrator",
  name: "Audit Orchestrator",
  instructions: `You are an ecommerce brand intelligence analyst.
You have access to tools that run each of the 6 audit modules against a brand.
When asked to audit a brand:
1. Run all 6 tools in sequence: brand basics → content → ads → geo → store → research.
2. When re-auditing a brand you have seen before, compare current scores to past scores
   and highlight improvements or regressions.
3. Your final output should be a concise executive summary (3-5 sentences) highlighting
   the brand's biggest strength and most urgent improvement area.`,
  model: groq("llama-3.3-70b-versatile"),
  tools: {
    scrapeHomepageTool,
    runBrandBasicsTool,
    runContentAuditTool,
    runAdsAuditTool,
    runGEOAuditTool,
    runStoreAuditTool,
    runResearchAuditTool,
  },
  memory: brandMemory,
});
