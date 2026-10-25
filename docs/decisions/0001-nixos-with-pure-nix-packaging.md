---
status: accepted
date: 2026-10-25
decision-makers: Pierre
---

# NixOS with pure Nix packaging

## Context and Problem Statement

The agenda runs on a single VM. A colleague operates it with Claude, and deploys straight from their laptop. The colleague has no command-line or SSH experience, so whether they know Debian or NixOS doesn't come into it. What matters is how easily a bad change can be undone, and how much Claude can manage on its own.

## Considered Options

* NixOS, with services as native NixOS modules
* Debian with docker compose
* NixOS with docker compose

## Decision Outcome

Chosen option: "NixOS, with services as native NixOS modules". Every deploy is a new system generation, and rolling back is one command. The operator can't judge whether a fix is right, so it matters that any change can be undone mechanically.

NixOS with compose was rejected because it gives two packaging stories and two rollback stories.

### Consequences

* Good, because every change can be rolled back, both by hand and automatically after a failed health check.
* Good, because the whole system (PostgreSQL, Caddy, the app, timers, backups) lives as one declarative config in the repo.
* Bad, because if Claude can't fix a Nix evaluation error, the operator can't either. Pierre is the escalation path.

### Confirmation

* The flake builds the full system configuration, and `nix flake check` passes.
* The deploy command refuses to deploy when checks fail, and rolls back automatically when the post-deploy health check fails.

## Hard constraints

* No docker or compose on the host. Every service is a NixOS module or a Nix-built package.
* Deploys go through one command, which runs all checks first.
