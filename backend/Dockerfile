FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# libpq supports psycopg; age encrypts customer exports before they leave the
# API container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 postgresql-client age \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/
RUN pip install --no-cache-dir .

COPY . /app/

RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# Local development and CI can opt into tools such as pytest and Ruff without
# shipping them in the production image.
FROM runtime AS development
RUN pip install --no-cache-dir -e ".[dev]"
