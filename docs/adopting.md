# Adopting coord in a repository

This is the step-by-step for a new repository. The model and every command are in
[agent-coordination.md](agent-coordination.md).

## 1. Install

```sh
pipx install git+https://github.com/alex-bridges/coord      # installs a `coord` command
# or, from a clone, without installing:
git clone https://github.com/alex-bridges/coord ~/tools/coord
ln -s ~/tools/coord/coord ~/.local/bin/coord                # or run ~/tools/coord/coord directly
```

It is pure Python 3.9+ standard library. Run it from a checkout of the target repository;
the repository is read from the `origin` remote (`COORD_REPO=owner/name` overrides).

## 2. Authenticate

`gh auth login` (the `gh` CLI), or set `GH_TOKEN` / `GITHUB_TOKEN` and coord talks to the API
directly (`COORD_TRANSPORT=http` forces that). Check with `coord doctor --online`.

## 3. What works with no configuration

`board`, `whoami`, `doctor`, `claim`, `release`, `send`, `inbox` (and `outage`, whose weekly reset
needs `claude_weekly_reset` or `--until`). Try:

```sh
coord board
coord claim 12 --files src/foo.py --note "adding foo"
coord inbox
```

The merge train needs `ci_workflow` and `train_workflow`; the reviewer needs `reviewer_bot_login`
and `reviewer_trigger` plus a GitHub App; the conductor needs its prompt and guard files
(`conductor.*`), and a GitHub App for the reviewer. A feature missing a setting stops with a
message naming the key.

## 4. Configure: `.coord/config.json`

Commit it at the repository root (`COORD_CONFIG` names another path). All keys are listed with
their defaults in the Configuration section of agent-coordination.md and in
`coordlib/config.py`.

Minimal, enabling the merge train:

```json
{
  "ci_workflow": "ci.yml",
  "train_workflow": "merge-train.yml"
}
```

Full, INISA-style (illustrative values; the paths and names are yours to choose):

```json
{
  "default_branch": "main",
  "owner_login": "your-login",
  "owner_mention": "@your-login",
  "reviewer_bot_login": "my-bot[bot]",
  "reviewer_trigger": "@my-bot review",
  "ci_workflow": "ci.yml",
  "train_workflow": "merge-train.yml",
  "notify_workflow": "notify-owner.yml",
  "verdict_job": "CI verdict",
  "wake_job": "Wake the merge train",
  "ci_lanes": "changed",
  "owner_only_paths": [".coord/", "CLAUDE.md", ".claude/", ".github/scripts/"],
  "lane_classifier": {"script": "scripts/select-ci-lanes.py", "rules": "scripts/ci-lanes.json"},
  "runner_variables": ["RUNNER_MACOS", "RUNNER_LINUX"],
  "conductor": {
    "agent_file": ".claude/agents/conductor.md",
    "instructions_file": "CLAUDE.md",
    "guard_hook": ".claude/hooks/conductor-guard.py",
    "deny": []
  },
  "reviewer": {
    "agent_file": ".claude/agents/reviewer.md",
    "guard_hook": ".claude/hooks/reviewer-guard.py"
  },
  "claude_weekly_reset": "Thu 12:00 America/New_York",
  "keychain_service": "coord-github-app",
  "state_dir_name": "coord",
  "legacy_state_dir_names": []
}
```

`ci_lanes` is sent as the `lanes` input when the train dispatches your CI workflow, so that
workflow needs a `workflow_dispatch` `lanes` input if you set it; leave it unset to send no inputs.
`verdict_job` and `wake_job` name jobs in the CI workflow that aggregate the others.

## 5. Labels

`coord train step` creates these if missing (and ignores ones that exist); to create them ahead of
time, for example with `gh label create <name> --color <hex> --description "<text>"`:

| Label | Colour | Meaning |
|---|---|---|
| `train` | `0e8a16` | queued for the merge train (`coord train add`) |
| `train:solo` | `fbca04` | the train tests this PR on its own (it was in a red batch) |
| `reviewed` | `c2e0c6` | informational; a clean review is recorded |
| `needs-alex` | `b60205` | a carve-out; held until the owner approves its head |
| `alex-approved` | `0e8a16` | the owner approved a head (`coord train approve`) |

The `needs-alex` and `alex-approved` names are fixed in the code whatever `owner_login` is.

## 6. The merge-train workflow

The train advances one step per workflow run (see Merge train in agent-coordination.md). Your
`train_workflow` file checks out the target repository with push access and a pinned copy of coord
next to it, then runs one step. `COORD_REPO_TOKEN` is a repository secret holding a read-only token
(a fine-grained PAT with Contents: read on `alex-bridges/coord`) so the private repository can be
cloned; omit `token` if coord's repository is public to your runner. Pin `COORD_SHA` to a reviewed
commit and bump it deliberately.

```yaml
name: Merge train
on:
  workflow_dispatch:
    inputs:
      mode:
        type: choice
        options: [default, dry-run, live]
        default: default
  schedule:
    - cron: "*/15 * * * *"        # backstop; also wake it from your CI workflow's last job
permissions:
  contents: write
  pull-requests: write
  issues: write
  actions: write
  checks: read
  statuses: read
concurrency:
  group: merge-train
  cancel-in-progress: false
jobs:
  step:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    env:
      GH_TOKEN: ${{ github.token }}
      COORD_TRANSPORT: http
      COORD_AGENT: train@actions
      COORD_REPO: ${{ github.repository }}
      COORD_SHA: 0000000000000000000000000000000000000000   # pin: a reviewed coord commit
      MODE: ${{ (inputs.mode != '' && inputs.mode != 'default') && inputs.mode || vars.COORD_TRAIN_MODE || 'dry-run' }}
    steps:
      - uses: actions/checkout@v4
        with:
          path: target
          fetch-depth: 0
      - uses: actions/checkout@v4
        with:
          repository: alex-bridges/coord
          ref: ${{ env.COORD_SHA }}
          token: ${{ secrets.COORD_REPO_TOKEN }}
          path: coord-tool
      - name: Advance the train by one step
        working-directory: target        # so coord finds .coord/config.json
        run: python3 ../coord-tool/coord train step --mode "$MODE" --repo-dir .
```

Notes. The default mode is a dry run (batches are tested, nothing merges); set the repository
variable `COORD_TRAIN_MODE=live` when the dry runs look right. `actions/checkout` leaves its
credential in the clone, which is what lets the step push `train/*` branches and fast-forward main
with the Actions token; if your default branch is protected so that token cannot push, see Limits
under Merge train in agent-coordination.md. In your CI workflow, add a job that dispatches or
wakes this workflow after each batch (INISA's is `wake-train`), because runs started by the
Actions token do not trigger `workflow_run`.

## 7. The conductor (optional, macOS)

The conductor is a launchd agent that wakes an agent only when there is work. On the CI Mac:

```sh
git clone https://github.com/alex-bridges/coord ~/tools/coord
~/tools/coord/conductor/install.sh --repo owner/name [--home DIR] [--interval 300] [--no-wake]
```

It installs `com.coord.conductor.<owner>-<name>` (log `~/Library/Logs/coord-conductor-<owner>-<name>.log`),
keeps a checkout of the repository detached at origin/main (default `~/conductor/<name>`), and runs
`coord conductor tick` from it every `--interval` seconds. `--no-wake` installs model-free ticks;
`--uninstall` removes the agent. It needs `git`, `python3` and `gh` on PATH. Set the `conductor`
keys (prompt, instructions file, guard hook) in the repository's `.coord/config.json` first; every CI
box runs one and the lease picks the leader. Check with `coord conductor status`.
