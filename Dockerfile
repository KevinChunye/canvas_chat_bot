# Any agent in this repo on maritime.sh (custom container contract: $PORT, GET /health, POST /chat, GET /schedules).
# AGENT picks which one runs: factcheck (default) or ghost_fleet. Set it in the agent's settings.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY canvas_core/ canvas_core/
COPY agents/ agents/
COPY scripts/ scripts/
# SQLite database, cycle lock and JSONL logs live on Maritime's persistent volume.
ENV AGENT=factcheck \
    AGENT_DATA_DIR=/data \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1
# Exec form launching a real program (Maritime's micro-VM init breaks shell-string CMDs).
CMD ["python", "-m", "agents"]
