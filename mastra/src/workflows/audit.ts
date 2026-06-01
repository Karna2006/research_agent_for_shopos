/**
 * Audit Workflow
 *
 * Two-step design:
 *   step 1 (run-agents): calls all 6 Python agent endpoints sequentially,
 *                        reports progress back after each one.
 *   step 2 (compile):    aggregates results, updates brand memory,
 *                        signals Python that the audit is complete.
 *
 * Each step receives the ORIGINAL workflow input via getInitData() so
 * audit_id / url / brand_name are always available.
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
  brand_basics:    z.unknown(),
  content_catalog: z.unknown(),
  performance_ads: z.unknown(),
  geo_visibility:  z.unknown(),
  store_cro:       z.unknown(),
  research:        z.unknown(),
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

// ── Step 1: Run all 6 agents ──────────────────────────────────────────────────

const stepRunAgents = createStep({
  id:           "run-agents",
  inputSchema:  TriggerSchema,   // first step — receives trigger directly
  outputSchema: AgentResultsSchema,
  execute: async ({
    inputData,
  }: {
    inputData: Trigger;
  }): Promise<AgentResults> => {
    const { audit_id, url, brand_name } = inputData;

    const pipeline: Array<{
      key: keyof AgentResults;
      endpoint: string;
    }> = [
      { key: "brand_basics",    endpoint: "/internal/agent/brand_basics" },
      { key: "content_catalog", endpoint: "/internal/agent/content"      },
      { key: "performance_ads", endpoint: "/internal/agent/ads"          },
      { key: "geo_visibility",  endpoint: "/internal/agent/geo"          },
      { key: "store_cro",       endpoint: "/internal/agent/store"        },
      { key: "research",        endpoint: "/internal/agent/research"     },
    ];

    const results: Partial<AgentResults> = {};

    for (const { key, endpoint } of pipeline) {
      await reportProgress(audit_id, key, "running");
      try {
        results[key] = await callAgent(endpoint, url, brand_name);
        await reportProgress(audit_id, key, "done", results[key]);
      } catch (e: unknown) {
        const err = e instanceof Error ? e.message : String(e);
        results[key] = { agent: key, error: err };
        await reportProgress(audit_id, key, "error", undefined, err);
      }
    }

    return results as AgentResults;
  },
});

// ── Step 2: Compile + memory + notify Python ──────────────────────────────────

const stepCompile = createStep({
  id:           "compile-report",
  inputSchema:  AgentResultsSchema,   // receives step 1 output
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

    // ── Brand memory note (via Python endpoint — Python writes to its own DB) ───
    let memoryNote: string | undefined;
    try {
      const resource  = brandResource(url);
      const geoResult = results.geo_visibility as Record<string, unknown> | undefined;
      const currentGeoScore =
        (geoResult?.analysis as Record<string, unknown> | undefined)?.geo_score as number | undefined;

      // Ask Python to compute and return the trend note (Python has full audit history)
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

    // ── Signal Python that the workflow is done ───────────────────────────────
    await fetch(`${PYTHON_API}/internal/audit/${audit_id}/complete`, {
      method:  "PUT",
      headers: iHeaders,
      body:    JSON.stringify({ results }),
    }).catch(() => {});

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
