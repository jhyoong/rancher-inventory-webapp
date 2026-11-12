FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
WORKDIR /src
COPY pyproject.toml .
COPY inventory/ inventory/
RUN pip wheel --wheel-dir /wheels .

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN useradd --uid 10001 --create-home inventory
COPY --from=build /wheels /wheels
RUN pip install --no-index --find-links /wheels rancher-inventory \
    && rm -rf /wheels
COPY config/defaults/ /app/defaults/
ENV DEFAULTS_DIR=/app/defaults DATA_DIR=/data SITES_FILE=/config/sites.yaml PYTHONDONTWRITEBYTECODE=1
USER 10001
VOLUME /data
EXPOSE 8443
HEALTHCHECK --interval=1m CMD ["inventory", "healthcheck"]
ENTRYPOINT ["inventory"]
CMD ["serve"]
