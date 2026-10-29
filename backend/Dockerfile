FROM python:3.12-slim-trixie

LABEL org.opencontainers.image.title="OpsSOP Backend" \
      org.opencontainers.image.version="2.2.0" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/liduojia123/OpsSOP"

WORKDIR /srv/opssop

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive

# 平台内置备份需要与服务端 16.x 匹配的 pg_dump/pg_restore，否则恢复工具无法读取
# 新版本归档头。使用 PGDG 源安装 16.x 客户端，并安装 tzdata 供定时任务使用。
ARG APT_MIRROR=
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get -o Acquire::ForceIPv4=true update \
    && apt-get -o Acquire::ForceIPv4=true install -y --no-install-recommends ca-certificates curl gnupg tzdata \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -4 -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get -o Acquire::ForceIPv4=true update \
    && apt-get -o Acquire::ForceIPv4=true install -y --no-install-recommends postgresql-client-16 \
    && apt-get -o Acquire::ForceIPv4=true purge -y --auto-remove curl gnupg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# 可选镜像源：构建环境访问默认 PyPI 较慢时传入 --build-arg PIP_INDEX_URL=...
ARG PIP_INDEX_URL=
RUN pip install --no-cache-dir ${PIP_INDEX_URL:+--index-url ${PIP_INDEX_URL}} -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
# 评测入口(scripts/eval_suite.py)与测试都在容器内运行,需要版本化评测集
COPY eval_data ./eval_data

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/system/health')"

CMD ["sh", "scripts/start.sh"]
