FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y git \
    && rm -rf /var/lib/apt/lists/*

# The built desk lives in server/static, so this image can serve both the API
# and the bundled UI without a Node runtime.
COPY . .
RUN pip install --upgrade pip && pip install ".[memory]"

EXPOSE 8030 8020

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8030"]
