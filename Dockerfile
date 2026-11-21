FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/var/cache/huggingface

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git libgomp1 libopus0 tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY bot.py audio.py ./

CMD ["python", "bot.py"]
