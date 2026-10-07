FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# Versions pinned (review 2026-10-05, P3): unpinned web stacks drifted with
# every rebuild and silently moved the line numbers 04-embedding.md anchors to.
RUN pip install --no-cache-dir --default-timeout=120 "fastembed>=0.7.1,<0.9"     "fastapi==0.141.1" "uvicorn==0.52.4" "pydantic==2.13.5" "httpx==0.28.1"

COPY server.py /srv/server.py
EXPOSE 8090
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1", "--log-level", "info"]