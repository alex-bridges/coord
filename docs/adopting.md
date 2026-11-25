# Adopting coord in a repository

This guide walks through adopting `coord` incrementally — from zero-config shared awareness
up through the GitHub Actions merge train, GitHub App identity, and macOS conductor. The full
protocol and architecture are in [agent-coordination.md](agent-coordination.md).

## 1. Install

```sh
pipx install git+https://github.com/alex-bridges/coord      # installs `coord` globally
# or, from a clone, without installing:
git clone https://github.com/alex-bridges/coord ~/tools/coord
ln -s ~/tools/coord/coord ~/.local/bin/coord                # or run ~/tools/coord/coord directly
```

Pure Python 3.9+ standard library (macOS, Linux, and Windows). Run `coord` inside a checkout of the
target repository; it detects `owner/name` from the `origin` remote (`COORD_REPO=owner/name`
overrides).

## 2. Authenticate and verify

Log in with the GitHub CLI (`gh auth login`), or export `GH_TOKEN` / `GITHUB_TOKEN` to use
direct HTTPS (`COORD_TRANSPORT=http` forces HTTPS). Verify read access without writing
anything:

```sh
coord doctor --online
```

## 3. Start with zero configuration

Without a config file, `board`, `whoami`, `doctor`, `claim`, `release`, `send`, `inbox`, and
`outage` (with `--until`) all work immediately:

```sh
coord board
coord claim 12 --files src/foo.py --note "adding foo"
coord send --on 12 "starting work on foo"
coord inbox
```

### Teaching your coding agents to use `coord`

Add a short block to `CLAUDE.md`, `AGENTS.md`, or your team's agent instructions:

```markdown
## Coordination (`coord`)
- Run `coord board` before starting work to see open PRs, active claims, and file overlaps.
- Claim your issue before editing: `coord claim <issue> --files <paths> --note "<summary>"`.
- Check `coord inbox` between tasks and answer messages with `coord send --on <n> "..."`.
- Release claims when stopping or handing off: `coord release <issue> --note "<why>"`.
- Once a PR is reviewed and ready (if the merge train is enabled), run `coord train add <pr>` and stop polling.
```

For Claude Code, copy [`examples/coord-board.sh`](../examples/coord-board.sh) to
`.claude/hooks/coord-board.sh` and wire it as a `SessionStart` hook in `.claude/settings.json`
so every session starts with `coord board --brief` automatically.

## 4. Configure `.coord/config.json` (optional features)

The merge train, sidechain reviewer, and conductor stay disabled until configured in
`.coord/config.json` at the root of the target repository (`COORD_CONFIG` overrides the path).
Every key and default is listed in [agent-coordination.md](agent-coordination.md#configuration)
and `coordlib/config.py`.

Minimal config to enable the merge train:

```json
{
  "ci_workflow": "ci.yml",
  "train_workflow": "merge-train.yml"
}
```

Full example (all optional features enabled; adjust names and paths to your repo):

```json
{
  "default_branch": "main",
  "owner_login": "your-login",
  "owner_mention": "@your-login",
  "needs_owner_label": "needs-owner",
  "owner_approved_label": "owner-approved",
  "reviewer_bot_login": "my-bot[bot]",
  "reviewer_trigger": "@my-bot review",
  "ci_workflow": "ci.yml",
  "train_workflow": "merge-train.yml",
  "notify_workflow": "notify-owner.yml",
  "verdict_job": "CI verdict",
  "wake_job": "Wake the merge train",
  "ci_lanes": "changed",
  "ignored_check_prefixes": [],
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
    "guard_hook": ".claude/hooks/conductor-guard.py"
  },
  "claude_weekly_reset": "Thu 12:00 America/New_York",
  "keychain_service": "coord-github-app",
  "state_dir_name": "coord",
  "legacy_state_dir_names": []
}
```

- `ci_lanes`: passed as the `lanes` input when dispatching `ci_workflow` (omit if your CI workflow takes no `workflow_dispatch` inputs).
- `verdict_job` / `wake_job`: optional aggregate jobs in your CI workflow excluded when diagnosing which underlying job failed.
- `ignored_check_prefixes`: optional status/check prefixes (e.g. legacy external CI) to ignore in PR CI readiness.

## 5. Labels & human-approval gates

`coord train step` creates these labels automatically if missing (or create them ahead of time
with `gh label create`):

| Label | Colour | Meaning |
|---|---|---|
| `train` | `0e8a16` | Queued for the merge train (`coord train add`) |
| `train:solo` | `fbca04` | The train tests this PR on its own (after a multi-PR batch failed) |
| `reviewed` | `c2e0c6` | Informational; a clean review was recorded (`coord train review`) |
| `needs-owner` | `b60205` | Holds a PR until the repository owner approves its exact head (rename via `needs_owner_label`) |
| `owner-approved` | `0e8a16` | Applied when `coord train approve` records owner approval of a head (rename via `owner_approved_label`) |

**How human-approval gates work:** PRs labelled `needs-owner`, PRs touching any path in
`owner_only_paths` (such as agent prompts, guard hooks, or `.coord/`), and PRs touching
`.github/workflows/` (when a workflow-capable app token is supplied) are held until a trusted
owner records approval of the exact 40-char commit SHA:

```sh
coord train approve 42 --head <full-40-char-sha> --note "approved in review"
```

Because labels persist across pushes, the label alone is never enough — a new push requires a
new approval.

## 6. The merge-train workflow

The train advances one step per workflow run (see Merge train in
[agent-coordination.md](agent-coordination.md)). Your `train_workflow` checks out the target
repository with push access plus a pinned commit of `coord`, and runs `coord train step`.
Pin `COORD_SHA` to a reviewed commit and bump it deliberately.

`coord train step --repo-dir PATH` reads the target checkout's config and discovers
the GitHub repository from its origin. `COORD_CONFIG` can select another config file.
For a local mirror or proxy origin, pass `coord --repo owner/name train step --repo-dir PATH`
or set `COORD_REPO`. Recognizable GitHub fetch and push URLs must match that repository.

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
      COORD_SHA: 0000000000000000000000000000000000000000   # pin a reviewed coord commit SHA
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
          path: coord-tool
      - name: Advance the train by one step
        working-directory: target        # so coord finds .coord/config.json
        run: python3 ../coord-tool/coord train step --mode "$MODE" --repo-dir .
```

**Going live:** Default mode is `dry-run` (batches are built and tested, trunk does not move).
Once dry runs look right, set the repository variable `COORD_TRAIN_MODE=live`. Add a small
final job in your CI workflow to dispatch `merge-train.yml` on batch completion, because runs
started by `GITHUB_TOKEN` do not trigger `workflow_run`.

### Review gating (with or without Codex)

Before merging a PR, the train requires a clean review of its exact head commit. Either signal
satisfies the gate:
1. **Manual / agent review attestation (works for any team):** After another agent or human
   completes a clean review, record it using the configured `owner_login` account (the repository owner by default):
   ```sh
   coord train review 42 --head <full-40-char-sha> --by <reviewer-agent-or-name> --note "clean review"
   ```
   (If you use the optional sidechain reviewer below, a passing review records this
   automatically.)
2. **GitHub Codex connector:** A fresh 👍 reaction from `chatgpt-codex-connector` plus its
   pinned summary comment naming that head as `Completed`, with no unanswered review threads.

## 7. GitHub App mode (optional, macOS & Windows)

If many local agents run concurrently, they share your user account's 5,000 REST calls/hour
rate limit. App mode moves `coord`'s reads and operational writes onto a dedicated GitHub App
installation (its own 5,000/hr quota), keeping attestations (`train approve`, `train review`)
on your user login.

1. **Create a GitHub App** in your GitHub settings (webhook inactive) and install it on your
   target repository with these repository permissions:
   - **Read token:** `Actions: Read`, `Checks: Read`, `Contents: Read`, `Issues: Read`,
     `Metadata: Read`, `Pull requests: Read`, `Commit statuses: Read`
   - **Ops token:** `Actions: Write`, `Issues: Write`, `Metadata: Read`, `Pull requests: Write`
   - *(Optional, only if you also want the merge train to land `.github/workflows/` PRs via
     `COORD_TRAIN_APP_TOKEN` in Actions: `Contents: Write` and `Workflows: Write`.)*
2. **Generate a private key (`.pem`)** and note the app's **Client ID** (`Iv23...`) and bot
   login (`<app-slug>[bot]`).
3. **Write the non-secret config** at `~/.config/coord/github-app.json` (or
   `~/.config/<state_dir_name>/github-app.json`):
   ```json
   {
     "client_id": "Iv23YourClientIdHere",
     "repo": "owner/name",
     "app_login": "my-coord-app[bot]"
   }
   ```
   Also set `"coord_app_login": "my-coord-app[bot]"` in the repository's `.coord/config.json`,
   so every reader trusts the handoff, ack and mailbox records this app posts. Write it with
   the `[bot]` suffix, as GitHub shows it; without the suffix it trusts nobody. `coord doctor`
   warns when the two differ.
4. **Import the `.pem` private key** into your OS credential store (macOS login Keychain or
   Windows DPAPI under `%LOCALAPPDATA%\coord\keys\`):
   ```sh
   coord token import-key path/to/your-app.private-key.pem
   ```
   *(On macOS this stores base64(PEM) in Keychain service `coord-github-app`; on Windows it
   encrypts the PEM to your Windows user account via DPAPI and signs JWTs in memory with
   pure-Python RS256 — no OpenSSL required.)*
5. **Verify:**
   ```sh
   coord token status
   coord doctor --online
   ```
   To temporarily bypass app mode and use your `gh` login instead, set `COORD_APP=off`.

## 8. The conductor and sidechain reviewer (optional, macOS & Windows)

The conductor is a periodic watcher that polls cheaply in plain Python and wakes Claude only
when there is real work (messages to `conductor@*`, red CI, train holds, offline runners, or
`@<bot> review` comments).

1. Copy and adapt the starter templates from [`examples/`](../examples/README.md) into your
   target repository (`.claude/agents/conductor.md`, `.claude/agents/reviewer.md`,
   `.claude/hooks/conductor-guard.py`) and point `.coord/config.json` at them.
2. Install the watcher on your CI machine:

   **macOS (`launchd`):**
   ```sh
   git clone https://github.com/alex-bridges/coord ~/tools/coord
   ~/tools/coord/conductor/install.sh --repo owner/name [--home DIR] [--interval 300] [--no-wake]
   ```
   Installs `com.coord.conductor.<owner>+<name>` (log
   `~/Library/Logs/coord-conductor-<owner>+<name>.log`), keeps a detached checkout of the default branch
   at `~/conductor/<owner>/<name>`, and runs `coord conductor tick` every `--interval`
   seconds. Use `--no-wake` for model-free ticks and `--uninstall` to remove it.

   **Windows (Task Scheduler, PowerShell):**
   ```powershell
   git clone https://github.com/alex-bridges/coord $HOME\tools\coord
   & "$HOME\tools\coord\conductor\install.ps1" -Repo owner/name [-HomeDir DIR] [-Interval 300] [-NoWake]
   ```
   Registers a per-user Scheduled Task `coord-conductor-<owner>+<name>` (log
   `%LOCALAPPDATA%\coord\logs\conductor-<owner>+<name>.log`) running from `~\conductor\<owner>\<name>`.
   Use `-NoWake` for model-free ticks and `-Uninstall` to remove it.

   On either OS, existing installations retain their previous scheduler name when the recorded
   repository matches. When reusing a checkout, any identifiable GitHub fetch or push URL for
   `origin` must point to the requested repository. Check status with `coord conductor status`.

## 9. Using `coord` on a public vs. private repository

`coord` was designed primarily for private repositories where everyone who can comment is a
collaborator, and it also enforces boundaries if used on a **public** repository:
- **Approvals & clean reviews:** `owner-approve` and `review-clean` records are only trusted
  when GitHub identifies the author as `owner_login`, which defaults to the repository owner (`User`, never a `[bot]` or
  external commenter).
- **Merge train:** PRs from forks are automatically rejected (`fromFork`), and batch CI runs
  only count when dispatched by `github-actions[bot]`.
- **Untrusted comments:** On a public repo, any GitHub user can still post comments containing
  `claim` or `msg` markers on open issues. If you run the unattended conductor on a public
  repo, treat all issue/PR comments as untrusted input and pair the guard hook with a GitHub
  branch ruleset on your default branch.
