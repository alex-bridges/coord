# Starter templates

Copy and adapt these files into your target repository when enabling the optional `coord`
integrations described in [docs/adopting.md](../docs/adopting.md):

| File | Copy to (in your repo) | Purpose |
|---|---|---|
| `coord-board.sh` | `.claude/hooks/coord-board.sh` | Claude Code `SessionStart` hook that injects `coord board --brief` into new sessions (fails open/silent) |
| `conductor.md` | `.claude/agents/conductor.md` | Standing orders for the unattended conductor agent (`conductor.agent_file`) |
| `reviewer.md` | `.claude/agents/reviewer.md` | Standing orders for the sandboxed read-only sidechain reviewer (`reviewer.agent_file`) |
| `conductor-guard.py` | `.claude/hooks/conductor-guard.py` | `PreToolUse` safety tripwire hook for unattended conductor and reviewer runs (`conductor.guard_hook` / `reviewer.guard_hook`) |

Remember to `chmod +x` the hook scripts (`coord-board.sh` and `conductor-guard.py`) and
reference them in `.coord/config.json` and `.claude/settings.json`.
