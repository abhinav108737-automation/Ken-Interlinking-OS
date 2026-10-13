# Ken Interlink OS -- API image. Also used by the `migrate` and `worker`
# compose services (same code, different command).
#
# Build from the repository root:  docker build -f apps/api/Dockerfile .
# No secrets are baked in: all configuration arrives as KEN_* environment
# variables at run time (.env.compose in Mode B).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Packaging metadata first (README.md is referenced by pyproject's `readme`).
# requirements.lock.txt pins every transitive dependency; without it each build
# could resolve different versions than the tested lockfile.
COPY pyproject.toml README.md requirements.lock.txt ./

# The four package roots of the single multi-root distribution (ADR-0001).
COPY contracts/python ./contracts/python
COPY services ./services
COPY apps/api ./apps/api
COPY agents ./agents

# Migrations, so the same image can run `alembic upgrade head`.
COPY alembic.ini ./
COPY migrations ./migrations

# Editable install keeps multi-root package discovery identical to development;
# non-editable multi-root builds are not yet verified (ADR-0001).
RUN pip install --no-cache-dir -e . -c requirements.lock.txt \
    && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin ken

USER ken

EXPOSE 8000

# --no-access-log: RequestIdMiddleware already emits one structured access line
# per request (with request_id); uvicorn's own line would duplicate it.
CMD ["uvicorn", "ken_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
