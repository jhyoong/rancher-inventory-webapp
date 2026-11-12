# Build stage: wheels for exactly the versions in uv.lock (hash-checked), plus the app wheel.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.28@sha256:59240a65d6b57e6c507429b45f01b8f2c7c0bbeee0fb697c41a39c6a8e3a4cfb /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt > requirements.txt \
    && pip wheel --require-hashes --wheel-dir /wheels -r requirements.txt
COPY inventory/ inventory/
RUN pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN useradd --uid 10001 --create-home inventory
COPY --from=build /wheels /wheels
COPY --from=build /src/requirements.txt /tmp/requirements.txt
RUN pip install --no-index --find-links /wheels --require-hashes -r /tmp/requirements.txt \
    && pip install --no-index --no-deps --find-links /wheels rancher-inventory \
    && rm -rf /wheels /tmp/requirements.txt
COPY config/defaults/ /app/defaults/
ENV DEFAULTS_DIR=/app/defaults DATA_DIR=/data SITES_FILE=/config/sites.yaml PYTHONDONTWRITEBYTECODE=1
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001
VOLUME /data
EXPOSE 8443
HEALTHCHECK --interval=1m --timeout=10s --start-period=2m CMD ["inventory", "healthcheck"]
ENTRYPOINT ["inventory"]
CMD ["serve"]
