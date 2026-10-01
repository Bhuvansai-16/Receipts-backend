# The Receipts API on Google Cloud Run. The UI is the separate receipts-frontend repository, on Vercel.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY receipts/ receipts/
COPY migrations/ migrations/

# SWE-bench Verified cached in the image, so a cold start skips the 15-30 s Hugging Face download
RUN python -c "from receipts import swebench; swebench._dataset()"

# Settings and secrets come from Cloud Run's environment (no .env in the image). Cloud Run sets PORT, and
# `serve` then listens on 0.0.0.0:$PORT.
CMD ["python", "-m", "receipts", "serve"]
