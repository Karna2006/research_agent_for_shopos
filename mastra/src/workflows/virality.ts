/**
 * Virality Workflow — single-step product virality scoring.
 * Delegates to Python's ViralityPredictor via internal endpoint.
 */
import { createWorkflow, createStep } from "@mastra/core/workflows";
import { z } from "zod";

const PYTHON_API = process.env.PYTHON_API_URL ?? "http://localhost:8000";
const INTERNAL_KEY = process.env.INTERNAL_SECRET_KEY ?? "dev-internal-key-change-in-prod";

const iHeaders = {
  "Content-Type": "application/json",
  "X-Internal-Key": INTERNAL_KEY,
};

const TriggerSchema = z.object({
  url:          z.string().optional(),
  product_name: z.string().optional(),
  description:  z.string().optional(),
  category:     z.string().optional(),
});

const ViralityOutputSchema = z.object({
  score:        z.number(),
  grade:        z.string(),
  analysis:     z.unknown().optional(),
  product_name: z.string().optional(),
});

type Trigger        = z.infer<typeof TriggerSchema>;
type ViralityOutput = z.infer<typeof ViralityOutputSchema>;

const stepScoreVirality = createStep({
  id:           "score-virality",
  inputSchema:  TriggerSchema,
  outputSchema: ViralityOutputSchema,
  execute: async ({ inputData }: { inputData: Trigger }): Promise<ViralityOutput> => {
    const res = await fetch(`${PYTHON_API}/internal/virality/score`, {
      method:  "POST",
      headers: iHeaders,
      body:    JSON.stringify(inputData),
    });
    if (!res.ok) {
      const msg = await res.text().catch(() => res.statusText);
      throw new Error(`Virality scoring failed: ${msg}`);
    }
    return res.json() as Promise<ViralityOutput>;
  },
});

export const viralityWorkflow = createWorkflow({
  id:           "viralityWorkflow",
  inputSchema:  TriggerSchema,
  outputSchema: ViralityOutputSchema,
})
  .then(stepScoreVirality)
  .commit();
