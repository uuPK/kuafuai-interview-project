ARG DOCKER_CLI_IMAGE=docker:29-cli
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.27
ARG PYTHON_IMAGE=python:3.13-slim-bookworm
FROM ${DOCKER_CLI_IMAGE} AS docker-cli
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE}
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=build /opt/venv /opt/venv
RUN groupadd --gid 10001 dailynews && useradd --uid 10001 --gid dailynews --no-create-home dailynews \
    && mkdir -p /data && chown dailynews:dailynews /data
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
WORKDIR /opt/news
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-m", "app.healthcheck", "api"]
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
