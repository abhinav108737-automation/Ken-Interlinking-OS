-- Least-privilege APPLICATION role for the Ken Interlink OS (ADR-0014 section 5).
--
-- Idempotent. Run by infrastructure/scripts/db_roles.py (make db-roles) AS THE OWNER role
-- (the role that owns every object and runs Alembic), inside the target database.
-- NOT an Alembic migration: roles are cluster-level. The runner creates the role and sets
-- its password (generated at runtime, never in a file) BEFORE this script runs.
--
-- The application role (ken_app):
--   * LOGIN only: NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS, owns nothing;
--   * CONNECT on this database, USAGE (never CREATE) on schema public;
--   * SELECT/INSERT/UPDATE/DELETE on application tables; SELECT only on alembic_version;
--   * USAGE/SELECT/UPDATE on sequences (nextval needs USAGE; setval/ALTER needs UPDATE);
--   * NO TRUNCATE, REFERENCES, TRIGGER, no DDL, no ownership.
-- Trigger functions need no EXECUTE grant to fire: PostgreSQL checks EXECUTE on a trigger
-- function when the trigger is CREATED (by the owner), not when it fires. The guard
-- functions are not SECURITY DEFINER, so they run with the caller's (ken_app's) rights and
-- only read the rows/settings they need, all of which ken_app can SELECT.

ALTER ROLE ken_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
    NOINHERIT CONNECTION LIMIT -1;

DO $$
DECLARE owner_role text := current_user;
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM ken_app', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO ken_app', current_database());

    REVOKE ALL ON SCHEMA public FROM ken_app;
    GRANT USAGE ON SCHEMA public TO ken_app;

    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ken_app;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ken_app;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM ken_app;
    GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO ken_app;

    -- Tables and sequences created by FUTURE migrations (run as the owner) are covered.
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
        'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ken_app', owner_role);
    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
        'GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO ken_app', owner_role);
END
$$;

-- The migration ledger is read-only for the application (the readiness probe reads it).
-- Applied after the blanket grant above, and guarded: a database never migrated has none.
DO $$
BEGIN
    IF to_regclass('public.alembic_version') IS NOT NULL THEN
        REVOKE INSERT, UPDATE, DELETE ON public.alembic_version FROM ken_app;
    END IF;
END
$$;
