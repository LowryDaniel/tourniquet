FROM python:3.12-slim

# Injected by the deploy command (--build-arg GIT_SHA=$(git rev-parse HEAD));
# surfaced by /health so deploys are verifiable against the local commit.
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA

WORKDIR /app

# System deps for psycopg binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir -e .

EXPOSE 8000

CMD ["uvicorn", "tourniquet.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
