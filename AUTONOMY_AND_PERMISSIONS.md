# Autonomy and Permission Model

## Purpose

Claude Code should work fluently without asking for trivial permissions, while stopping before consequential actions. The same philosophy will later govern the Ken Interlinking Agent.

## ALLOW
Proceed automatically for local, reversible development actions:
- read/analyze files;
- create/edit local code;
- add approved dependencies;
- create/run tests;
- run lint/type checks;
- run local Docker services;
- create migrations;
- use mock data;
- inspect cloned reference repos;
- implement the currently approved phase.

## ASK
Pause for explicit approval before:
- push to GitHub;
- create/merge PRs;
- real CMS writes;
- use of real write credentials;
- destructive DB actions;
- enabling auto-publishing;
- policy/skill production promotion;
- adding a new major framework/service;
- unclear-license reuse;
- sending code/data externally;
- system-wide installs outside the project environment;
- live deployment.

## BLOCK
Do not perform unless project policy is explicitly changed:
- mandatory paid API dependency;
- direct LLM production database writes;
- silent policy self-promotion;
- unlogged production mutation;
- irreversible publication without rollback metadata;
- committing credentials;
- bypassing protected-page policy;
- disabling validation merely to make a build pass.

## Agentic OS Permission Model

Initial examples:
- READ_PAGE: ALLOW
- READ_ANALYTICS: ALLOW
- GENERATE_RECOMMENDATION: ALLOW
- RUN_OFFLINE_EVAL: ALLOW
- PROPOSE_SKILL: ALLOW
- PROPOSE_POLICY: ALLOW
- WRITE_LINK_PRODUCTION: ASK
- ROLLBACK_PRODUCTION: ASK
- PROMOTE_SKILL: ASK
- PROMOTE_POLICY: ASK
- CHANGE_AUTONOMY_LEVEL: ASK
- DELETE_CONTENT: BLOCK

## Progressive Autonomy

A decision class may graduate from ASK to ALLOW only when minimum sample size is met, approval rate is high enough, edit and rollback rates are low enough, confidence calibration is acceptable, outcomes are non-negative and an explicit policy authorizes graduation.
