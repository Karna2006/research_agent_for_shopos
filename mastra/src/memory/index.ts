/**
 * Brand Memory — persists audit history per brand URL.
 *
 * Each brand gets its own resource namespace (keyed by normalized URL).
 * Stores scores over time so we can say:
 *   "GEO score improved from 45 → 62 since last audit 23 days ago."
 */
import { Memory } from "@mastra/memory";
import { LibSQLStore } from "@mastra/libsql";

const storageUrl = process.env.LIBSQL_URL ?? "file:./brand_memory.db";

export const brandMemory = new Memory({
  storage: new LibSQLStore({
    id:  "brand-memory",
    url: storageUrl,
  }),
  options: {
    lastMessages: 10,        // keep last 10 audit summaries in context window
    semanticRecall: false,   // keyed lookups, not similarity search
  },
});

/**
 * Derive a stable resource ID from a brand URL.
 * "https://rarerabbit.in/products/abc" → "brand:rarerabbit.in"
 */
export function brandResource(url: string): string {
  try {
    const host = new URL(url).hostname.replace(/^www\./, "");
    return `brand:${host}`;
  } catch {
    return `brand:${url.slice(0, 60)}`;
  }
}

/** Thread ID for audit history — one thread per brand, accumulates over time. */
export const AUDIT_THREAD = "audit-history";
