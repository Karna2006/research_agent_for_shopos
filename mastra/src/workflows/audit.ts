/**
 * Audit Workflow — Mastra dashboard observability layer.
 *
 * Python LangGraph is the real execution engine. This workflow:
 *   step 1 (observe): mirrors what Python is running — calls all 8 agent
 *                     endpoints in parallel (matching LangGraph's parallel phase)
 *                     and reports each result back for dashboard visibility.
 *   step 2 (compile): aggregates results, updates brand memory, signals done.
 *
 * NOTE: This workflow is triggered fire-and-forget by Python when an audit starts.
 * Python never waits on this — it runs independently for the Mastra dashboard UI.
 */
import { createWorkflow, createStep } from "@mastra/core/workflows";
import { z } from "zod";
import { brandResource } from "../memory/index.js";

const PYTHON_API = process.env.PYTHON_API_URL ?? "http://localhost:8000";
const INTERNAL_KEY = process.env.INTERNAL_SECRET_KEY ?? "dev-internal-key-change-in-prod";

const iHeaders = {
  "Content-Type": "application/json",
  "X-Internal-Key": INTERNAL_KEY,
};

// ── Schemas ────────────────────────────────────────────────────────────────────

const TriggerSchema = z.object({
  audit_id:   z.number(),
  url:        z.string(),
  brand_name: z.string(),
});

const AgentResultsSchema = z.object({
  brand_basics:      z.unknown(),
  content_catalog:   z.unknown(),
  performance_ads:   z.unknown(),
  geo_visibility:    z.unknown(),
  store_cro:         z.unknown(),
  research:          z.unknown(),
  social_profile:    z.unknown(),
  social_media_audit: z.unknown(),
});

const CompileOutputSchema = z.object({
  audit_id:    z.number(),
  url:         z.string(),
  brand_name:  z.string(),
  results:     z.record(z.unknown()),
  memory_note: z.string().optional(),
});

type Trigger       = z.infer<typeof TriggerSchema>;
type AgentResults  = z.infer<typeof AgentResultsSchema>;
type CompileOutput = z.infer<typeof CompileOutputSchema>;

// ── Helpers ────────────────────────────────────────────────────────────────────

async function callAgent(endpoint: string, url: string, brand_name: string): Promise<unknown> {
  const res = await fetch(`${PYTHON_API}${endpoint}`, {
    method: "POST",
    headers: iHeaders,
    body: JSON.stringify({ url, brand_name }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${endpoint} → HTTP ${res.status}: ${text}`);
  }
  return res.json();
}

async function reportProgress(
  audit_id: number,
  agent_key: string,
  status: "running" | "done" | "error",
  result?: unknown,
  error?: string,
): Promise<void> {
  await fetch(`${PYTHON_API}/internal/audit/${audit_id}/progress`, {
    method: "PUT",
    headers: iHeaders,
    body: JSON.stringify({ agent_key, status, result, error }),
  }).catch(() => {});
}

// ── Step 1: Observe all 8 agents in parallel ──────────────────────────────────

const stepRunAgents = createStep({
  id:           "run-agents",
  inputSchema:  TriggerSchema,
  outputSchema: AgentResultsSchema,
  execute: async ({
    inputData,
  }: {
    inputData: Trigger;
  }): Promise<AgentResults> => {
    const { audit_id, url, brand_name } = inputData;

    // Phase 1: brand_basics alone (Python does this first for abort routing)
    await reportProgress(audit_id, "brand_basics", "running");
    let brandBasicsResult: unknown;
    try {
      brandBasicsResult = await callAgent("/internal/agent/brand_basics", url, brand_name);
      await reportProgress(audit_id, "brand_basics", "done", brandBasicsResult);
    } catch (e: unknown) {
      const err = e instanceof Error ? e.message : String(e);
      brandBasicsResult = { agent: "brand_basics", error: err };
      await reportProgress(audit_id, "brand_basics", "error", undefined, err);
    }

    // Phase 2: core 5 agents in parallel (mirrors LangGraph node_core_parallel)
    const coreAgents: Array<{ key: keyof AgentResults; endpoint: string }> = [
      { key: "content_catalog", endpoint: "/internal/agent/content"   },
      { key: "performance_ads", endpoint: "/internal/agent/ads"       },
      { key: "geo_visibility",  endpoint: "/internal/agent/geo"       },
      { key: "store_cro",       endpoint: "/internal/agent/store"     },
      { key: "research",        endpoint: "/internal/agent/research"  },
    ];

    // Report all as running before firing
    await Promise.all(coreAgents.map(({ key }) => reportProgress(audit_id, key, "running")));

    const coreResults = await Promise.all(
      coreAgents.map(async ({ key, endpoint }) => {
        try {
          const result = await callAgent(endpoint, url, brand_name);
          await reportProgress(audit_id, key, "done", result);
          return { key, result };
        } catch (e: unknown) {
          const err = e instanceof Error ? e.message : String(e);
          await reportProgress(audit_id, key, "error", undefined, err);
          return { key, result: { agent: key, error: err } };
        }
      }),
    );

    // Phase 3: social agents in parallel (mirrors LangGraph node_social_depth)
    const socialAgents: Array<{ key: keyof AgentResults; endpoint: string }> = [
      { key: "social_profile",    endpoint: "/internal/agent/social_profile"    },
      { key: "social_media_audit", endpoint: "/internal/agent/social_media_audit" },
    ];

    await Promise.all(socialAgents.map(({ key }) => reportProgress(audit_id, key, "running")));

    const socialResults = await Promise.all(
      socialAgents.map(async ({ key, endpoint }) => {
        try {
          const result = await callAgent(endpoint, url, brand_name);
          await reportProgress(audit_id, key, "done", result);
          return { key, result };
        } catch (e: unknown) {
          const err = e instanceof Error ? e.message : String(e);
          await reportProgress(audit_id, key, "error", undefined, err);
          return { key, result: { agent: key, error: err } };
        }
      }),
    );

    // Assemble final results
    const assembled: Partial<AgentResults> = { brand_basics: brandBasicsResult };
    for (const { key, result } of [...coreResults, ...socialResults]) {
      assembled[key] = result;
    }
    return assembled as AgentResults;
  },
});

// ── Step 2: Compile + memory ──────────────────────────────────────────────────

const stepCompile = createStep({
  id:           "compile-report",
  inputSchema:  AgentResultsSchema,
  outputSchema: CompileOutputSchema,
  execute: async ({
    inputData,
    getInitData,
  }: {
    inputData:   AgentResults;
    getInitData: () => Trigger;
  }): Promise<CompileOutput> => {
    const { audit_id, url, brand_name } = getInitData();
    const results = inputData as Record<string, unknown>;

    // Brand memory note — Python has full audit history, ask it for the trend
    let memoryNote: string | undefined;
    try {
      const resource  = brandResource(url);
      const geoResult = results.geo_visibility as Record<string, unknown> | undefined;
      const currentGeoScore =
        (geoResult?.analysis as Record<string, unknown> | undefined)?.geo_score as number | undefined;

      const memRes = await fetch(`${PYTHON_API}/internal/audit/${audit_id}/memory-note`, {
        method:  "POST",
        headers: iHeaders,
        body:    JSON.stringify({ resource, geo_score: currentGeoScore, brand_name }),
      }).catch(() => null);

      if (memRes?.ok) {
        const memData = await memRes.json() as { note?: string };
        memoryNote = memData.note;
      }
    } catch {
      // Memory is best-effort
    }

    return { audit_id, url, brand_name, results, memory_note: memoryNote };
  },
});

// ── Workflow ──────────────────────────────────────────────────────────────────

export const auditWorkflow = createWorkflow({
  id:           "auditWorkflow",
  inputSchema:  TriggerSchema,
  outputSchema: CompileOutputSchema,
})
  .then(stepRunAgents)
  .then(stepCompile)
  .commit();
