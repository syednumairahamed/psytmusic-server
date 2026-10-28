FROM python:3.11-slim

# Install system dependencies: ffmpeg for MP3 transcoding
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY service_account.json* ./
COPY credentials.json* ./

ENV PORT=10000
EXPOSE 10000

CMD ["python", "server.py"]
