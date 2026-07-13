# MCP server wiring (placeholder)

This directory holds **MCP server configs** that connect the hunting box to
Claude Code. It is a placeholder describing the intended wiring — **the servers
themselves are not implemented here**. See [`../../docs/PLAN.md`](../../docs/PLAN.md)
(§4 reuse-vs-build): the MCP/integration layer is *reused*, the judgment layer
is *built*.

The framework reuses two MCP integrations as references rather than rebuilding
tool execution:

## 1. Caido — Claude Skill (human-in-the-loop)

Caido runs on the **host/desktop** (not in compose) and integrates with Claude
through its official Skill, giving Claude access to the proxy's sitemap, scope,
HTTPQL search, Replay and Findings during manual deep-dives.

- Skill: https://github.com/caido/skills
- Role: AI-assisted *manual* workstation (the human decides what to exploit).
- Wiring: install the Caido skill into `.claude/skills/` per the upstream repo;
  point it at the running desktop Caido instance.

## 2. HexStrike-AI (MCP tool server)

HexStrike-AI is an MCP server exposing 150+ offensive tools. Reused as a
**reference** for command/agent decomposition (PLAN.md Phase 2) — not adopted
wholesale, and not auto-run against scope.

- Role: broad tool-execution surface behind MCP.
- Wiring: register the HexStrike MCP server in the Claude Code MCP config and
  gate every call behind the scope guard (`recon/active-scope.yaml`).

## Scope-guard reminder

Every MCP-driven tool call MUST respect the active scope contract in
[`../../docs/legal-scope.md`](../../docs/legal-scope.md): no call may target a
host that is not in the active program's in-scope set. Wire these servers so the
scope-guard hook (configured in `.claude/settings.json`) sees and can block
their requests.

## TODO (when implemented)

- [ ] `caido.json` — Caido skill connection config.
- [ ] `hexstrike.json` — HexStrike MCP server registration.
- [ ] Confirm each server's calls pass through the scope-guard hook.
