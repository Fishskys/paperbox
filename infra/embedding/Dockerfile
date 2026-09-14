FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir --default-timeout=120 "fastembed>=0.7.1,<0.9" fastapi uvicorn pydantic httpx

COPY server.py /srv/server.py
EXPOSE 8090
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1", "--log-level", "info"]