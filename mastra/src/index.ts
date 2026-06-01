/**
 * Mastra app entry point.
 *
 * The Mastra CLI (`mastra dev` / `mastra build`) reads this file and
 * creates an HTTP server at port 4111 exposing:
 *
 *   GET  /api/workflows                           — list all workflows
 *   POST /api/workflows/:id/execute               — execute (fire-and-forget)
 *   POST /api/workflows/:id/runs                  — create async run
 *   GET  /api/workflows/:id/runs/:runId/watch     — SSE progress stream
 *   GET  /mastra                                  — built-in workflow playground UI
 */
import { Mastra } from "@mastra/core";
import { LibSQLStore } from "@mastra/libsql";
import { auditWorkflow }            from "./workflows/audit.js";
import { viralityWorkflow }         from "./workflows/virality.js";
import { auditOrchestratorAgent }   from "./agents/auditOrchestrator.js";

const storageUrl = process.env.LIBSQL_URL ?? "file:./mastra_storage.db";

export const mastra = new Mastra({
  workflows: {
    auditWorkflow,
    viralityWorkflow,
  },
  agents: {
    auditOrchestratorAgent,
  },
  storage: new LibSQLStore({
    id:  "mastra-storage",
    url: storageUrl,
  }),
});
