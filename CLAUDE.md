# Omi Development Guide
<!-- Official guidance for writing these files:
     CLAUDE.md: https://docs.anthropic.com/en/docs/claude-code/memory
     AGENTS.md: https://developers.openai.com/codex/guides/agents-md
     Format spec: https://agents.md -->

All agent instructions for this repository live in **[AGENTS.md](./AGENTS.md)** — that is the single source of truth for every agent (Claude Code, Codex, and any other).

- **Always read `AGENTS.md` before starting work in this repo.**
- When adding, changing, or removing any rule or guideline, edit **`AGENTS.md` only** — do not add instructions to this file.

(Component-specific guides still apply where present: `backend/CLAUDE.md`, `desktop/CLAUDE.md`, `app/e2e/SKILL.md`, `desktop/e2e/SKILL.md`.)

## Before creating anything new

1. **Read `AGENT_QUICK_REF.md`** at the project root.
2. **Search the registry** for what you're about to add (component, service, hook, task).
   Grep is faster than re-implementing.
3. **Extend, don't fork** — if 80% of what you need exists, extend it.

Full standard: see `playbook/standards/agent-discoverability.md` in
[ushadow-sdk](https://github.com/Ushadow-io/ushadow-sdk).

## Shared Ecosystem Skills

Skills are in `.claude/skills/`. Read the relevant file directly — do not use the Skill tool for these.

| Trigger | Skill file |
|---|---|
| Extracting, reusing, or sharing code across projects | `.claude/skills/extract-component.md` |
| Using `@ushadow-io/ui`, `ushadow-common`, or any shared dep | `.claude/skills/use-shared.md` |
| Deploying a service, creating a namespace, or wiring k8s network policies | `.claude/skills/k8s-service.md` |
