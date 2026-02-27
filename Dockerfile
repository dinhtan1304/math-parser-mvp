# ============================================================
# Math Exam Parser — Railway Production Dockerfile
# ============================================================

FROM python:3.11-slim

# System deps for: PyMuPDF (libgl, libglib), lxml, argon2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libxml2-dev \
    libxslt1-dev \
    gcc \
    g++ \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps (separate layer for Docker cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Create uploads dir
RUN mkdir -p uploads

EXPOSE 8000

# Railway sets $PORT automatically
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1