# The Receipts API image (Render; Cloud Run works the same). The UI is the separate receipts-frontend repository.

# SWE-bench Verified, cached as one JSON file, so a cold start skips the 15-30 s Hugging Face download. Only
# this stage needs `datasets` (with pyarrow and pandas); the API never imports it.
FROM python:3.12-slim AS dataset
WORKDIR /app
COPY requirements.txt .
RUN grep -E '^(datasets|python-dotenv)==' requirements.txt > build.txt && pip install --no-cache-dir -r build.txt
COPY receipts/ receipts/
RUN python -c "from receipts import swebench; swebench._dataset()"

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN grep -v '^datasets==' requirements.txt > runtime.txt && pip install --no-cache-dir -r runtime.txt
COPY receipts/ receipts/
COPY migrations/ migrations/
COPY --from=dataset /app/.cache/ .cache/

# Settings and secrets come from the host's environment (no .env in the image). The host sets PORT, and
# `serve` then listens on 0.0.0.0:$PORT.
CMD ["python", "-m", "receipts", "serve"]
