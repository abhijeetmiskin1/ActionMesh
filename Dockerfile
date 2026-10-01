FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY actionmesh ./actionmesh

RUN pip install --no-cache-dir ".[server]"

ENV ACTIONMESH_HOST=0.0.0.0
ENV ACTIONMESH_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"

CMD ["uvicorn", "actionmesh.api:app", "--host", "0.0.0.0", "--port", "8080"]
