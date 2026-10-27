FROM python:3.11-slim

# Install system dependencies: ffmpeg for MP3 transcoding, nodejs for yt-dlp JS challenges
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir -U yt-dlp

COPY server.py .
COPY cookies.txt* ./

ENV PORT=10000
EXPOSE 10000

CMD ["python", "server.py"]
