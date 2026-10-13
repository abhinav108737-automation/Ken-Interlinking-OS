# Project PostgreSQL container

| | |
|---|---|
| Image | `pgvector/pgvector:pg16` |
| Host address | `127.0.0.1:5433` → container `5432` (bound to loopback only) |
| Databases | `ken_interlink` (application), `ken_interlink_test` (integration tests) |
| Volume | `ken-interlink_postgres-data` |

## Why port 5433

The development machine already runs its own PostgreSQL on 5432, hosting unrelated data. Publishing
5432 here would collide, and if unnoticed the API would silently connect to the wrong database.
`make doctor` prints the resolved host **and port** so this cannot go unnoticed.

## Why this image, and why pgvector is not enabled

The image **provisions** pgvector (it appears in `pg_available_extensions`). Phase 0 never **enables**
it: no migration runs `CREATE EXTENSION`. Enablement belongs to the Phase 2 migration that creates the
first vector column. See [ADR-0003](../../../docs/adr/ADR-0003.md).

## init/

`init/01-create-test-database.sql` runs once, on first initialisation of the data volume, and creates
`ken_interlink_test`. It creates no extension.

Because init scripts run only against an **empty** volume, changing them later has no effect on an
existing volume. Recreating the volume deletes all local data — that is a destructive action; do it
deliberately.
