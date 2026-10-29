FROM python:3.12-slim

LABEL org.opencontainers.image.title="OpsSOP Backend" \
      org.opencontainers.image.version="2.1.0" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/liduojia123/OpsSOP"

WORKDIR /srv/opssop

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

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
