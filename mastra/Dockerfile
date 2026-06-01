FROM node:20-slim AS deps

WORKDIR /app
COPY package*.json ./
RUN npm ci

# ── Build ─────────────────────────────────────────────────────────────────────
FROM node:20-slim AS builder

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# mastra build compiles TypeScript → .mastra/output/
RUN npx mastra build

# ── Production image ──────────────────────────────────────────────────────────
FROM node:20-slim

WORKDIR /app

# Only copy built output and production node_modules
COPY --from=builder /app/.mastra/output ./
COPY --from=deps    /app/node_modules   ./node_modules

# Ensure persistent storage dirs exist
RUN mkdir -p /data
ENV LIBSQL_URL="file:/data/brand_memory.db"

EXPOSE 4111

# mastra start serves the built output
CMD ["npx", "mastra", "start", "--port", "4111"]
