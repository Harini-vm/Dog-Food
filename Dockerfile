FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src DOGFOOD_FIXTURES=/app/fixtures.json DOGFOOD_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY src ./src
COPY tests ./tests
COPY tools ./tools
COPY fixtures.json run.py pyproject.toml .dogfood.toml ./
RUN useradd --system --uid 10001 dogfood && mkdir -p /data && chown dogfood /data
USER dogfood
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --retries=12 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"
CMD ["python", "-m", "dogfood"]
