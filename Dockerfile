# syntax=docker/dockerfile:1

# OpenDataLoader 运行时需要 Java 11+；固定使用 Java 21 JRE，并把它复制进 Python 镜像。
FROM eclipse-temurin:21-jre-jammy AS java-runtime

FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    JAVA_HOME=/opt/java/openjdk \
    NLTK_DATA=/app/nltk_data \
    LOG_FILE_ENABLED=false \
    PARSE_TEMP_DIR=/tmp/energy-carbon-rag \
    PDF_PARSER_BACKEND=opendataloader \
    RECALL_LTR_MODE=off \
    PATH="/opt/java/openjdk/bin:${PATH}" \
    TZ=Asia/Shanghai

COPY --from=java-runtime /opt/java/openjdk /opt/java/openjdk

WORKDIR /app

# OpenCV/OpenDataLoader 运行库、LightGBM OpenMP 运行库以及健康检查工具。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        fontconfig \
        fonts-noto-cjk \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY app ./app

RUN python -m pip install --upgrade pip \
    && python -m pip install .

# infinity-sdk 等分词依赖会读取这些 NLTK 资源。构建期固化，运行期不联网下载。
RUN python -m nltk.downloader \
    -d "${NLTK_DATA}" \
    punkt punkt_tab stopwords wordnet omw-1.4

COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY docker ./docker

RUN useradd --create-home --uid 10001 rag \
    && mkdir -p /tmp/energy-carbon-rag /app/logs \
    && chown -R rag:rag /tmp/energy-carbon-rag /app/logs

USER rag

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)" || exit 1

CMD ["/bin/sh", "/app/docker/entrypoint.sh"]
