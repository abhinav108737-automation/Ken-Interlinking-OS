-- Runs once, on first initialisation of the project container's data volume.
--
-- Creates the scratch database used by integration tests, so that tests can
-- run migrations up and down freely without touching ken_interlink.
--
-- Deliberately does NOT run CREATE EXTENSION vector. pgvector is provisioned
-- by the image but not enabled in Phase 0 (docs/adr/ADR-0003.md).
CREATE DATABASE ken_interlink_test OWNER ken;
