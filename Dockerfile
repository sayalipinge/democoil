FROM python:3.11-slim

WORKDIR /app

# System libs needed by opencv-headless
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libsm6 libxext6 libxrender1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first (cached layer — only rebuilds if requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

# Create data dir (will be overridden by Fly.io volume in production)
RUN mkdir -p /data/uploads

EXPOSE 8000

CMD ["python", "api/app.py"]
