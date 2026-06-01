FROM python:3.11-slim

WORKDIR /app

# Playwright system deps (Chromium)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg \
    libnss3 libnspr4 \
    libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (layer-cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's Chromium browser + its own OS deps
RUN playwright install chromium --with-deps

# Copy application source
COPY . .

# Ensure writable runtime dirs exist
RUN mkdir -p output demo static reports/output

EXPOSE 8000

# Use uvicorn directly — main.py __main__ block does browser-open which breaks containers
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--log-level", "info"]
