# coord

Shared awareness, messaging, and an optional merge train for multiple AI coding agents
(Claude Code, Codex, cloud agents, CI bots) working in one GitHub repository at the same time.

`coord` runs with **zero external infrastructure**: it reads live state from GitHub and writes
only human-readable comments with a trailing JSON marker. Nothing is locked — if an agent
crashes or exits, its claim simply ages out. Pure Python 3.9+ standard library.

## What it looks like

```text
owner/repo board · main 8c4e12a90 · 2026-11-19T17:00:00Z · 8 API calls · API: 4820 left (resets 18:00Z)
you: claude@workstation:repo-a1b2c3d4e5f60718

IN FLIGHT
  PR #42  feat/parser-cache · in train · claude@workstation:wt1-9f8e7d6c5b4a3210
      Cache parsed ASTs across runs
      CI green · clean review · updated 6m ago
  claim #45  fix/retry-jitter · codex@workstation:wt2-1234567890abcdef
      Add jitter to backoff loop
      no PR · branch not pushed · claimed 14m ago · active 14m ago

OVERLAPS (same files in two pieces of work; coordinate, nothing is locked)
  PR #42 and claim #45: src/client.py

CI
  idle

TRAIN
  1. PR #42 · added 4m ago by claude@workstation:wt1-9f8e7d6c5b4a3210 · ready

INBOX  1 new for you (coord inbox)
```

## Quickstart (zero configuration)

Install with `pipx` (or `pip`, or run `./coord` from a clone):

```sh
pipx install git+https://github.com/alex-bridges/coord
```

Authenticate with `gh auth login` (or set `GH_TOKEN` / `GITHUB_TOKEN`), then run inside any
GitHub checkout (`COORD_REPO=owner/name` overrides auto-detection from `origin`):

```sh
coord doctor --online                                    # verify auth, identity, and read access
coord board                                              # see open PRs, claims, overlaps, CI, inbox
coord claim 45 --files src/client.py --note "retry fix"  # announce work on issue #45
coord send --on 42 "I am also touching src/client.py"    # message whoever owns PR #42
coord inbox                                              # read messages & comments on your work
coord release 45 --note "handed off"                     # end a claim
```

Nothing needs to be configured for `board`, `whoami`, `doctor`, `claim`, `release`, `send`,
and `inbox`.

## Adopt progressively

You only enable the pieces you want in `.coord/config.json`:

| Tier | What you get | Setup required |
|---|---|---|
| **1. Core CLI** | Board, claims, file-overlap warnings, direct/broadcast messaging, inbox, API rate-limit guard, `doctor` | None — works out of the box |
| **2. Merge train** | GitHub Actions merge queue (batching, retry/split on failure, human-approval gates for sensitive paths, review gating) | `.coord/config.json` (`ci_workflow`, `train_workflow`) + Actions workflow |
| **3. GitHub App mode** *(macOS)* | Separate 5,000 req/hr API quota for `coord` reads & operational writes (keeps your personal `gh` quota clear) | GitHub App + `~/.config/coord/github-app.json` + macOS Keychain |
| **4. Conductor & Reviewer** *(macOS)* | `launchd` watcher that wakes a Claude agent only when CI breaks, mail arrives, or a PR requests a sidechain review | `conductor/install.sh` + prompt/guard files (see [`examples/`](examples/)) |

## Wiring your coding agents

The easiest way to get value from `coord` is to add a short rule to your repository's
`CLAUDE.md` or `AGENTS.md`:

```markdown
## Coordination (`coord`)
- Run `coord board` before starting work to see open PRs, active claims, and file overlaps.
- Claim your issue before editing: `coord claim <issue> --files <paths> --note "<summary>"`.
- Check `coord inbox` between tasks and reply with `coord send --on <issue-or-pr> "..."`.
- Release claims when handing off or stopping: `coord release <issue> --note "<why>"`.
```

For Claude Code, you can also install [`examples/coord-board.sh`](examples/coord-board.sh) as a
`SessionStart` hook so every new session starts with `coord board --brief` in context.

## Commands at a glance

| Command | Purpose |
|---|---|
| `coord board [--brief \| --json \| --fresh]` | Live snapshot of open PRs, claims, file overlaps, CI queue, train, and unread count |
| `coord claim <issue> [--files ...] [--note ...]` | Announce active work on an issue (detects simultaneous claims; refuses if already held unless `--force`) |
| `coord release <issue> [--note ...]` | End your claim on an issue |
| `coord send --on <n> "<msg>"` / `--to <agent>` | Post a routed message on an issue/PR or the shared mailbox (`codex@*`, `all`, etc.) |
| `coord inbox [--peek \| --all \| --json]` | Read unread messages addressed to you and new comments on your claimed issues/PRs |
| `coord doctor [--online] [--json]` | Preflight check of identity, transport, credentials, and cache (read-only) |
| `coord whoami [--json]` | Show this session's automatic agent ID, repo, branch, identity, and API budget |
| `coord train add/remove/status/review/approve/report/step` | Queue PRs, record clean reviews or owner approvals, inspect train state, or run a train step in CI |
| `coord outage start/end <harness>` | Mark a model harness down (e.g., usage limit) so claims don't go stale and other agents see why |
| `coord conductor status/pause/resume/escalate/diagnose/tick` | Manage the unattended conductor watcher or run a read-only `codex`/`claude` board diagnosis |
| `coord token status` / `coord token exec -- <cmd>` | Inspect GitHub App mode or run a command with a scoped app token in its environment |

## Platform support

- **Core CLI & Merge Train:** macOS and Linux, Python 3.9+ (standard library only; requires `gh` CLI or `GH_TOKEN`/`GITHUB_TOKEN`). Native Windows is not supported (uses Unix file locks).
- **GitHub App Keychain minting, `conductor/install.sh` (`launchd`), and sidechain reviewer process cleanup (`ps -E`):** macOS only.

## Documentation & examples

- [docs/adopting.md](docs/adopting.md) — step-by-step adoption guide (config, labels, merge-train workflow, non-Codex review gating, GitHub App setup, conductor, and public-repo security notes).
- [docs/agent-coordination.md](docs/agent-coordination.md) — full architecture, protocol specification, API budget guard, merge train rules, conductor, reviewer, and safety model.
- [examples/](examples/README.md) — ready-to-copy starter templates (`coord-board.sh`, `conductor.md`, `reviewer.md`, `conductor-guard.py`).

## Running the tests

Standard library `unittest` only (no external test dependencies):

```sh
python3 -m unittest discover -s tests -p 'test_*.py'   # full suite (~2-3 min)
python3 -m unittest tests.test_config                  # one module
python3 -m unittest tests.test_budget.RecordingTests   # one class
```

If running inside a Claude cloud session (`CLAUDE_CODE_REMOTE=true` forces REST-only reads),
run with `env -u CLAUDE_CODE_REMOTE python3 -m unittest …`. On Linux, `test_reviewer`'s
macOS-only `ps -E` cleanup test is skipped automatically.

## License

MIT — see [LICENSE](LICENSE).
