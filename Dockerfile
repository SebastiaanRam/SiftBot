FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: run the daily digest once. For scheduled mode, use docker-compose
# with a cron entry or set SCHEDULE_MODE=true to use APScheduler.
CMD ["python", "-m", "worker.main"]
