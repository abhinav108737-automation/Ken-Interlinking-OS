# `ken_agents` — agent runtime

Phase 0 contains one thing: the **worker** (`ken_agents/worker.py`). Its design is fixed by
[ADR-0005](../../docs/adr/ADR-0005.md).

## What the worker is (Phase 0)

A plain `asyncio` heartbeat tick loop. Every `KEN_WORKER_TICK_SECONDS` (default 30) it:

1. runs the readiness probes (the same set behind `GET /ready`);
2. emits one structured `worker.heartbeat` log line (aggregate status, per-check statuses, tick
   number, duration, and a fresh `correlation_id`);
3. appends one `worker.heartbeat` row to the `events` table, with the same `correlation_id`.

A dependency blip (database down, model server down) is logged and the loop carries on; it never
crashes the process. SIGTERM and SIGINT stop it promptly and cleanly: the providers are closed and the
database engine is disposed.

## What it deliberately is NOT

- **Not a queue consumer.** No Celery, Redis, Kafka or Temporal, and no in-process `asyncio.Queue`
  "job system", task table or polling for work. That would be shadow infrastructure (ADR-0005).
- **Not an agent.** No LangGraph, no model inference, no decisions.
- **Not a server.** It opens no port.

## Running it

Natively, against the project PostgreSQL on port 5433 (migrated with `alembic upgrade head`):

```bash
.venv/bin/python -m ken_agents.worker --once   # one tick; exit 0 only if the heartbeat was written
.venv/bin/python -m ken_agents.worker          # tick loop until SIGTERM / Ctrl-C
```

In Docker Compose it is the `worker` service (profile `full`), running
`python -m ken_agents.worker` after the `migrate` service has completed:

```bash
docker compose --profile full up -d worker
```

## What it will host later

- **Phase 1:** the crawl scheduler.
- **Phase 4:** the LangGraph supervisor runner.

Both will be designed deliberately on the PostgreSQL-backed event bus (TRD §15), not grown into this
loop by accident.
