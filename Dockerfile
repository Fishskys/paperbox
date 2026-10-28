# paperbox 应用容器（只打包应用本身；PG/OpenSearch/MinIO/Embedding 是外部依赖，见 infra/）
FROM docker.io/library/eclipse-temurin:21-jdk-jammy AS figure-builder
RUN apt-get update && apt-get install -y --no-install-recommends git python3 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY scripts/build_pdffigures2.py /tmp/build_pdffigures2.py
RUN python3 /tmp/build_pdffigures2.py --output /opt/pdffigures2/pdffigures2.jar

FROM docker.io/library/eclipse-temurin:21-jre-jammy AS java-runtime
FROM docker.io/library/python:3.12-slim
COPY --from=java-runtime /opt/java/openjdk /opt/java/openjdk
COPY --from=figure-builder /opt/pdffigures2 /opt/pdffigures2
ENV JAVA_HOME=/opt/java/openjdk \
    PATH="/opt/java/openjdk/bin:$PATH" \
    PDFFIGURES2_JAR=/opt/pdffigures2/pdffigures2.jar

# 依赖与虚拟环境统一由 uv 管理（与本机开发环境同一套 pyproject/uv.lock）
# Pinned (review 2026-10-05, P3): a floating tag made every rebuild a
# dependency lottery for a --frozen sync.
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /usr/local/bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/srv/.venv

WORKDIR /srv

# 先只拷贝依赖清单，让这一层可缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 应用代码
COPY alembic.ini /srv/alembic.ini
COPY migrations /srv/migrations
COPY app /srv/app
COPY scripts /srv/scripts

EXPOSE 8077

# 应用以非 root 运行；数据全部在外部服务里，容器无状态
RUN useradd --create-home --uid 10001 paperbox && chown -R paperbox:paperbox /srv
USER paperbox

# 监听地址/端口走 PAPER_API_HOST / PAPER_API_PORT（与 app/core/config.py 的字段同名），
# 便于 compose/命令行覆盖；exec 保证信号能直达 uvicorn。
CMD ["sh", "-c", "exec uv run --no-sync uvicorn app.main:app --host ${PAPER_API_HOST:-0.0.0.0} --port ${PAPER_API_PORT:-8077}"]
