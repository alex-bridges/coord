# Agent coordination (`coord`)

Several agents work in one GitHub repository at once: Claude Code and Codex sessions, Claude
cloud sessions, and CI bots. `coord` gives each of them the same picture of what is in
flight, and a way to talk to the others. It is a Python 3.9+ standard-library tool over
the `gh` CLI (or direct HTTPS with a token). To use it in a repository, start with
[adopting.md](adopting.md).

## Configuration

Per-repo settings live in `.coord/config.json` at the root of the working tree
(`COORD_CONFIG` names another file). It is a JSON object; keys not set take the defaults
below (`coordlib/config.py`, `DEFAULTS`); nested objects merge key by key; unknown keys are
kept. A missing file means the generic defaults: `board`, `claim`, `release`, `send` and
`inbox` work, and the merge train, reviewer and conductor stay off. A feature that needs a
missing key stops with a message naming it.

| Key | Default | Used for |
|---|---|---|
| `default_branch` | none: asked from GitHub | the branch the train builds on and the board treats as trunk |
| `owner_login` | the repository's owner | who approves carve-outs and whose attestations count |
| `owner_mention` | `@` + `owner_login` | what notices @-mention to reach the owner |
| `needs_owner_label` | `needs-owner` | label that holds a carve-out PR until the owner approves its head |
| `owner_approved_label` | `owner-approved` | label applied when `coord train approve` records owner approval |
| `reviewer_bot_login` | none: reviewer disabled | the reviewer app's login, e.g. `my-bot[bot]` |
| `reviewer_trigger` | none | the PR comment that requests a review, e.g. `@my-bot review` |
| `ci_workflow` | none: train disabled | the CI workflow file the train dispatches and reads |
| `train_workflow` | none: train disabled | the workflow that runs `coord train step` |
| `notify_workflow` | none | a workflow dispatched to notify the owner (escalations, outage alerts) |
| `verdict_job`, `wake_job` | none | the CI workflow's aggregate jobs (red whenever any lane is) |
| `ci_lanes` | none: no dispatch inputs | the `lanes` input the train dispatches the gate with |
| `ignored_check_prefixes` | `[]` | check/status name prefixes excluded from PR/review and merge decisions |
| `owner_only_paths` | `[]` | path prefixes whose changes wait for the owner's approval of the head |
| `lane_classifier` | none: every PR is one lane | `{"script": ..., "rules": ...}`, paths at the default branch of main's own light/heavy selector |
| `runner_variables` | `[]` | repository variables that route CI to runners |
| `runner_control_variable` | unset | repository variable naming the control runner label |
| `conductor` | all unset | `agent_file` (prompt), `instructions_file`, `guard_hook`, `deny` (extra patterns) |
| `reviewer` | all unset | `agent_file`, `guard_hook` for the reviewer runs |
| `claude_weekly_reset` | none | the weekly usage reset for outages, e.g. `Thu 12:00 America/New_York` (`COORD_CLAUDE_WEEKLY_RESET` overrides) |
| `keychain_service` | `coord-github-app` | the Keychain item holding the GitHub App's key |
| `state_dir_name` | `coord` | per-user state under `~/.cache/<name>`, `~/.config/<name>`, `~/Library/Caches/<name>` |
| `legacy_state_dir_names` | `[]` | older names still read when the new one has nothing, never written |

The conductor adds its `agent_file`, `instructions_file` and `guard_hook` to the guarded
`owner_only_paths` list. The repository is read from the `origin` remote (`COORD_REPO`
overrides); `coord` says so when it cannot tell.

## Claude cloud sessions

A Claude cloud session has no `gh` CLI but has a proxied token. From one, `whoami`, `board`
(REST only, paged), `send`, `inbox` and `train status` all work, and a claim on a closed
issue is refused without posting. Codex cloud has not been probed. Cloud sessions have no
GitHub App: no config and no key, so `coord` there runs on the proxy's token and spends the
user's API budget (see The API budget).

## The model: compute the picture, don't maintain it

Nothing in this system is a stored status that someone has to remember to update.
Every `coord board` call builds the picture from what GitHub already knows:

| The board shows | Read from | Written by |
|---|---|---|
| open PRs: branch, head, files, CI result, Codex state | GraphQL, one query per 25 PRs | agents pushing and opening PRs, GitHub, the Codex bot |
| what CI is running and what waits | Actions runs and jobs | GitHub |
| who is working on which issue, on which branch, expecting which files | marked comments (`coord claim`) | agents |
| messages | marked comments (`coord send`) | agents |
| the merge train | the `train` label, `train/*` branches, marked comments | `coord train add`, the train workflow |

Only the last three rows are new writes, and each is an ordinary issue or PR comment
that a human can read on GitHub. If an agent dies, nothing is left locked: its claim
simply stops showing activity and turns *stale* after 48 hours.

A board costs about a dozen API calls: one GraphQL query per 25 open PRs, a few for
the Actions queue (plus one per running workflow run), and one per 100 comments of the
last 14 days, where claims and messages live. Each identity has one hourly quota
(5,000 REST and 5,000 GraphQL points), shared by every agent that uses it (see Identity
and The API budget), so the board is cached per machine for 60 seconds (5 minutes for the
session-start hook). `--fresh` bypasses the cache.

## Commands

```bash
coord board            # everything in flight; --brief, --json, --fresh
coord whoami           # this agent's id, repo, branch
coord doctor           # offline identity, runtime, transport and cache preflight
coord doctor --online --json # verify one repository read; never tests writes
coord claim 123 --files src/foo.py --note "one line of context"
coord release 123 --note "handed to …"
coord send --on 123 "question about the API"                 # to whoever works on #123
coord send --to 'codex@*' "rebasing main in 10 min"        # to every Codex agent
coord inbox            # messages to you + comments on your claimed work; --peek, --all
coord ci core          # queue ci_workflow lanes on the current branch
coord train add 984    # queue a ready PR for the merge train; then stop (see Merge train)
coord train add 984 --head <full-sha>   # the same, refused unless GitHub shows the head you just pushed
coord train status     # the current batch and each queued PR's readiness; a one-off look, never a poll
coord train report --days 7 --json # one-shot receipts, queue timing and measured runner minutes
coord train review 984 --head <full-sha> --by <agent> --note "…"   # record a clean review of that head by any agent (old name: substitute-review)
coord train approve 984 --head <full-sha> --note "…"              # on the owner's word: approve a guarded/carve-out PR at that head
coord send --to 'conductor@*' "the train has held #984 for a day"   # to the conductor
coord conductor status  # its hosts, pause, escalations, and this machine's recent wakes and tokens
coord conductor diagnose --runtime codex --input report.json --json # read-only diagnosis
coord conductor diagnose --runtime claude --handoff # diagnose the board; send repair advice if needed
coord conductor pause --for 2h --note "…"      # no model wakes; resume ends it
coord conductor escalate "…"                   # put something in front of the owner on the board for a day
coord outage start claude --note "weekly limit" # every Claude agent down until the weekly reset (--until to override)
coord outage end claude
coord token status     # app mode on or off and why; each cached app token's expiry and permissions
coord token exec -- gh pr list   # run a command with an app token in its environment only
```

Without `gh`, `coord` talks to the API directly when `GH_TOKEN` or `GITHUB_TOKEN` is
set (`COORD_TRANSPORT=http` forces it).

**Claude cloud sessions** have no `gh` and a token whose value starts with `prox`: the
session's proxy swaps in real credentials (no OAuth scopes are reported, so `coord` never
inspects the token). The proxy also refuses paths it cannot canonicalize: numeric
`repositories/<id>/…` paths (GitHub writes its next-page links that way; `coord` rewrites
them to `repos/<owner>/<name>/…`) and paths ending in `/`. It refuses GraphQL with HTTP 403,
so `coord` reads through REST instead: at once when `CLAUDE_CODE_REMOTE=true`, after the
first 403 anywhere else, or always with `COORD_REST_ONLY=1`. The REST path builds the same
PR data the GraphQL query returns, so every rule is computed once. It costs about seven
calls per open PR instead of one query, so it is not the default. REST cannot tell whether a
review thread was resolved; there a thread counts as answered only if someone replied.

Exit codes: `0` success, `1` usage, `2` conflict (someone else holds the claim, or
you lost a simultaneous claim), `3` GitHub or `gh` error, `4` not found, `5` the API
budget guard refused (see below).

## The API budget

Budgets are counted per **principal**, the account whose counter a call spends, not
per token:

| Principal | Who spends it | Recorded as |
|---|---|---|
| User counter | the owner's `gh`, every agent's `gh`, cloud proxy tokens, and `coord` commands using the user identity (see Identity below) | `user` |
| The GitHub App installation | `coord`'s reads and operational writes in app mode (see Identity). Its hourly tokens are one budget, not several | `app:<installation id>` |
| Actions | the merge train's `GITHUB_TOKEN` (1,000 an hour per repository) | not recorded |

Without a GitHub App, every agent and the user's own `gh` share one user budget of 5,000
REST calls an hour. Polling `coord train status` from REST-only cloud workers can exhaust
that budget and lock out every agent and `gh` until reset. App mode takes `coord`'s own
local traffic off that budget; cloud sessions still spend it.

`coord` reads the budget from the `X-RateLimit-Remaining`, `-Used`, `-Limit`, `-Reset`
and `-Resource` headers of its real responses (`-Limit` stands in for a missing `-Used`)
and keeps the last readings per principal and resource (core and graphql) in the
per-machine cache, which every worktree on a machine shares. User readings live in
`budget.json` (`{"core": {…}, "graphql": {…}}`) and app principal readings live in
`budget-app.json` (`{"app:<installation id>": {"core": {"windows": […]}, …}}`, under its own
lock). A file that cannot be read (corrupt, or nested too deep) counts as no reading and is
replaced by the next write. The `gh` transport sees no headers, so before a gated command on
the user identity it probes with one `gh api -i` call when the reading is more than two
minutes old; the app's calls go over HTTPS and record their own. Never the `rate_limit`
endpoint, which can report full quota while real endpoints return 403.

**The app's counter can span multiple windows.** Consecutive app calls can alternate between
two rate-limit windows whose resets are seconds apart across tokens, so one reading can be
lower than the one before it. For the app, `budget-app.json` keeps the highest `used` seen
for each reset still in the future, and **left = 5,000 minus the sum of those maxima**
(`COORD_BUDGET_APP_LIMIT`). A lower reading never replaces a higher one for the same reset,
and a window whose reset has passed drops out.

**Left, for the user counter,** is not `Remaining` alone. A cloud proxy token may
authenticate as the same user while reporting `X-RateLimit-Limit: 15000` where `gh` reports
5,000, while `Used` is one counter shared by both. So left is the lower of `Remaining` and
`COORD_BUDGET_USER_LIMIT` (5000) minus `Used`, from the latest reading. The board's header
shows it as `API: N left (resets HH:MMZ)`; in app mode it shows the app's budget first and
then the user's: `API (app): N left (resets …) · user: M left (resets …)`.

A reading whose reset has passed counts as a full budget, and so does one more than an
hour old, since a window never lasts longer. A budget file of the wrong shape counts as no
reading: the guard lets commands run, and the next probe replaces it.

The guard checks what is left of the core budget of the principal the command spends
(the app's in app mode, the user's for commands that stay on user identity) before each
command, with the same floors for both. An exhausted app never spills onto the user's
budget: reads serve the cached board and writes are refused, as below. The read that finds
the budget exhausted (a 403 or 429 whose headers put it below the read floor) serves the
cache too.

| Left (core) | `board`, `board --brief`, `train status`, `inbox` | `ci` | `claim`, `release`, `train add/remove/review/substitute-review/approve` | `send` | `train step` |
|---|---|---|---|---|---|
| below `COORD_BUDGET_READ_FLOOR` (1500) | served from the cached snapshot, whatever its age, with a one-line warning and no API call; no snapshot: refused, exit 5 | dispatches (one call), does not read the queue back | run | runs | not gated |
| below `COORD_BUDGET_HARD_FLOOR` (300) | as above | refused, exit 5 | refused, exit 5 | runs | not gated |

`send` always runs, so an agent can always report. `train step` runs in Actions on the
Actions token, a separate budget. `coord token exec` is refused below the hard floor
and runs with a warning below the read floor; `coord token status` makes no call. The
child's own API calls do not update `coord`'s saved budget reading: their spend becomes
visible when a later `coord` API response records the installation's headers. Repeated
`token exec` commands can therefore start against the same earlier reading; this wrapper is
not a live quota meter for the child.

## Identity

`coord` uses two GitHub identities:

- **The GitHub App** (configured via `~/.config/<state_dir_name>/github-app.json`), for
  `coord`'s reads and its operational writes on a Mac with the app installed. Reads use a
  read-only installation token (every permission `read`). Operational writes use a second
  token with only what they need: Issues write, Pull requests write, Actions write, and
  Metadata read.
- **The user's login** (`gh`/keyring or `GH_TOKEN`), for everything else: attestations, a
  hand-run train step, conductor ticks and control records, and all commands when app mode is off.

Why both Issues and Pull requests write: GitHub enforces whichever permission matches the
issue/PR number (Issues write for issues, Pull requests write for PRs), so operational
writes request both.

**App mode** is on when all of these hold:
- the non-secret config `~/.config/<state_dir_name>/github-app.json` exists;
- the command is not running in GitHub Actions (`GITHUB_ACTIONS` is not `true`): the
  train keeps `GITHUB_TOKEN`;
- `COORD_APP` is not `off`;
- `GITHUB_API_URL` is unset or `https://api.github.com`: the app's JWT and tokens go to
  GitHub and nowhere else;
- the repository is the one the app config names (`repo`).

`coord token status` says which condition turned it off.

Only a config path that does not exist counts as absent. A path `coord` cannot check (a
folder above it without search permission) or a config it cannot read or parse is a broken
config: app mode stays on and every app call refuses, naming the cause and `COORD_APP=off`.
Turning app mode off there would silently fall back to the user's identity.

**`COORD_APP=off`** is the deliberate override: `coord` runs on the user's `gh`/keyring
identity for everything. It is the way to recover by hand when the app cannot be used, and
nothing ever switches to it on its own.

| Write | Command | Identity in app mode | Why |
|---|---|---|---|
| claim and release comments | `claim`, `release` | the app (ops token) | operational; the marker names the agent, and `snapshot.resolve_claims` never reads the author |
| messages, the mailbox label and issue | `send` | the app (ops token) | operational; `inbox` takes the sender from the marker |
| train-add record, `train` label, the labels the train uses, the train wake | `train add` | the app (ops token) | operational; `snapshot.train_queue` and `train_step.train_added` read the marker, not the author |
| train-remove record, `train` label removed | `train remove` | the app (ops token) | operational |
| `ci_workflow` dispatch | `ci` | the app (ops token) | operational; `train.is_gate_run` counts only `github-actions[bot]`, so it never gates a batch |
| review-clean record, `reviewed` label | `train review` (and its old name `train substitute-review`) | user | an attestation, not an operation; the train counts the record only when a trusted user posted it, never a bot |
| owner-approve record, `<owner_approved_label>` label | `train approve` | user | an attestation, only on the owner's word; counted only when a trusted user posted it, never a bot |
| batch branches, main, train comments | `train step` run by hand | user | moves and deletes branches, which needs Contents write; in Actions the step uses `GITHUB_TOKEN` |
| outage records, and the conductor issue if missing | `outage start`, `outage end` | the app (ops token) | operational; `outage.windows` reads the marker, not the author |
| pause, resume and escalation records | `conductor pause`, `resume`, `escalate` | user | the watcher accepts control records from the configured owner login |
| conductor status | `conductor status` | the app (read token) | reads the board |
| lease beats, the outage records it finds itself | `conductor tick` | user | lists Actions runners (Administration read), edits its own heartbeat comments, and gates spending on the user's budget |
| PRs, `@codex review`, review-thread replies, `git push` | not coord | user | `review.is_bot` counts a reply from any `[bot]` login as no answer to a Codex finding |

Each call gets its token on that one HTTPS request. `GH_TOKEN` is never exported: it
would move `git push` onto the app through `gh auth git-credential`. The token (and, when
minting, the app's JWT) travels in an Authorization header that no redirect copies: a
redirect to another scheme, host or port is refused, and one within GitHub's own origin
gets the credential back (`gh.SameOriginRedirects`).

**Notifications.** App-authored `coord` comments notify the owner on threads they take part
in; filter or unwatch the mailbox and conductor issues as preferred. Posting claims,
releases, messages and outages on the user identity instead is one line in `cli.py`
(`USER_IDENTITY = ATTESTATIONS | NEEDS_ALEX | CONVERSATION`).

**Minting.** The app's private key is stored in the macOS login Keychain (service
`<keychain_service>`, account = the client id, value = the base64 of the PEM). It is read
only to mint, with `/usr/bin/security`, and reaches `/usr/bin/openssl` through an inherited
pipe (`/dev/fd/N`), never a file. `openssl` signs a JWT that lives nine minutes; `coord`
finds the installation and mints a token narrowed to the configured repository and the
kind's permissions, and refuses it unless GitHub granted exactly those. Each kind is cached
in its own file in `~/Library/Caches/<state_dir_name>/` (mode `0600`, with its expiry,
installation and permissions) and reused while more than five minutes remain; the cached
path never reads the Keychain, and a file lock makes concurrent agents mint once. A cached
token whose permissions do not match its kind is never used. When GitHub rejects an
installation with 404 and a replacement mint succeeds, the cached replacement keeps the
exact rejected ids and cleans up obsolete entries for the other kind under lock without
blocking.

**No automatic fallback.** When the app has no token, nothing is sent on the user identity.
That covers:
- the config cannot be read;
- the Keychain is locked or prompting;
- `openssl` fails, or the network fails;
- GitHub refuses the mint;
- GitHub answers 401 to a cached token (revoked, or the installation suspended). That
  token is also dropped from the cache.

`board`, `inbox`, `train status` and `conductor status` serve the cached board. The
board's first line, the brief's and `conductor status`'s then begin
`STALE: snapshot from 3h00m ago; GitHub App token unavailable: …`.
Every other command is refused (exit 3), naming the cause and `COORD_APP=off`.

A failed mint is remembered for five minutes in `~/Library/Caches/<state_dir_name>/`
(`<kind>.failed.json`, mode `0600`: the time, the cause, the stage, and the client id and
repository it belongs to, never a secret). A failure getting the key holds both kinds; a
refused mint holds only its own. `coord token status` shows a held failure, and deleting
the file retries at once.

**`coord token`** never prints a token. `status` shows whether app mode is on and why,
the installation, and each cached token's expiry and permissions, from files only.
`exec [--kind read|ops] -- <command>` runs the command with `GH_TOKEN` in that
command's environment only and exits with its exit code; the kind is `read` unless
`--kind ops` is given. The command inherits no other GitHub token: `GITHUB_TOKEN`,
`GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN` and any `GH_TOKEN` of the caller's are
removed first. `GH_CONFIG_DIR` is left alone, so the command can still run `gh auth token`
and reach the user's keyring: `exec` is a convenience wrapper, not a sandbox.

**Output additions with app mode:**
- `whoami` has a `githubIdentity` line (JSON key `githubIdentity`), and its `apiBudget`
  names the app's budget first in app mode.
- `board --json`: `apiBudget.principal` names the budget `coord` spends (`user`,
  `app:<id>`, or null before the first token), and in app mode `apiBudget.user` holds the
  user's. A board served from the cache carries `stale` (`ago`, `reason`).
- The board's first line and the brief lead with `STALE: …` when the board came from
  the cache because nothing fresher could be read.
- Errors are printed through a redaction that replaces anything shaped like a token, a
  JWT or key material with `[redacted]`.
- **Tests only:** `COORD_SECURITY_BIN` replaces `/usr/bin/security` for subprocess tests.
  It is honoured only when `COORD_APP_CONFIG` names a non-default config whose client id is
  not a default config's client id; otherwise `coord` refuses (exit 3) and never falls back
  to `/usr/bin/security`.

**The marker.** The author of a `coord` comment says nothing about which agent wrote it:
every agent posts as the same app or user login. Each `coord` comment ends with a marker
naming the agent:

```
<!-- coord:v1 {"agent":"claude@host:worktree-0123456789abcdef","branch":"…","files":[…],"issue":12,"kind":"claim","v":1} -->
```

Automatic ids are `<harness>@<host>:<worktree-label>-<16-hex-digest>`. The label is
readable; the digest distinguishes tasks and same-named worktrees. `codex` is detected
from a checkout under `~/.codex/` or a `CODEX_*` variable; other harnesses include
`claude`, `claude-cloud`, `actions`, and `human`.

Identity precedence is:
1. `COORD_AGENT`: the exact address supplied by the caller (also used by the train and conductor).
2. `COORD_SESSION_ID`: a stable task id supplied by any agent runtime.
3. `CODEX_THREAD_ID` for Codex; Claude's remote/session id for Claude.
4. The normalized real checkout path. Claude cloud without a session id uses a
   persistent container id, published atomically; it refuses if it cannot keep a stable
   id, naming the explicit overrides instead of choosing a new address on every call.

The session id or checkout path is hashed, never published raw. Simultaneous tasks in the
**same** checkout must supply different session ids. Resuming the same task on the same host
and checkout keeps its address. Use `COORD_AGENT` when an address must survive moving
between hosts or checkouts. Broadcasts such as `codex@*` still work.

Only the last non-blank line of a comment counts (a trailing Claude Code footer, which
cloud sessions append, is skipped), so quoting someone's comment never impersonates them.

## Interoperability contract and preflight

`coord:v1` markers require a JSON object with integer `v: 1`, a supported `kind`, a
nonempty string `agent`, and the fields required by that kind. Known field types are
validated before snapshot ingestion; booleans are not issue numbers. Duplicate JSON
keys, non-finite numbers, excessive nesting, unsupported versions, and malformed
records are ignored. A bad external comment cannot become a claim or crash the board.
Additive v1 fields remain permitted, and an empty attestation head still revokes an
earlier attestation. `tests/fixtures/coord-protocol.json` contains the conformance
examples; `marker.validation_error` provides value-free diagnostics.

For supported read commands, `--json` keeps stdout machine-readable on degraded and
error paths. An unavailable inbox has `status: "stale"`, `items: null`, and
`markedRead: false`; this is different from an empty inbox. Failures carry
`schema: 1`, `status: "error"`, and an `error` object with `code`, `kind`, and a redacted
`message`, plus the documented nonzero exit code. Human diagnostics remain on stderr.

`coord doctor` checks local configuration and reports the chosen identity, installed
CLIs, authentication principal, transport, and cached snapshot age. It starts no model,
mints no token, and makes no network request. `--online` adds a repository metadata GET
using `coord`'s normal authentication and budget policy. It verifies read access only;
neither installed `gh` nor a successful read proves operational write permissions.

The CLI targets Python 3.9+ on macOS, Linux, and native Windows (`coordlib/compat.py`
provides cross-platform `fcntl`/`msvcrt` file locks, Win32 Job Object process-tree
isolation, Windows DPAPI credential storage, and pure-Python RS256 signing). Codex cloud
is still **unprobed**; passing local contract tests does not establish cloud authentication
or network access. On macOS, the sidechain reviewer's extra `ps -E` run-tag sweep catches
processes that escape their process group; on Windows, the Win32 Job Object terminates the
entire process tree atomically on close.
To validate another environment, run `whoami --json`, offline/online `doctor`, and
`board --json`/`inbox --peek --json`; verify two concurrent tasks receive distinct ids.
Test claim/send/release and train submission only in a designated test repository or
issue with authorization. Do not distribute a local app's private key to cloud workers.

## Claims

A claim says "I am working on this issue, on this branch, and expect to touch these
files". It is an announcement, not a lock:

- **One holder.** If another agent already holds the issue, `claim` refuses (exit 2)
  and names them. Message them, pick other work, or take over with `--force` and a
  `--note` saying why.
- **Simultaneous claims.** GitHub comments have no compare-and-swap, so two agents can
  post at the same moment. After posting, `claim` reads the issue back: the earliest
  comment holds it, and a later claimant withdraws automatically. If replication lag
  hides the winner from the loser, both claims stay up and the board marks the later
  one `CONTESTED`, so the collision is visible instead of silent.
- **Ending.** `release`, the issue closing, or 48 hours without a PR or a push to the
  branch (shown as *stale*; anyone may `release --everyone` it with a note). Time the
  claim's agent spent down in an outage does not count toward the 48 hours.
- **Paths** are repository-relative and normalized (`./a/b`, `a/x/../b` and `a/b/` all
  mean `a/b`), matching how GitHub lists a PR's files; a path outside the repository is
  refused.
- **Overlaps.** The board lists pairs of PRs and claims that touch the same files or
  directories. A PR counts the files its linked claim declared as well as the ones it
  has changed. It is a prompt to talk, not a block; branches keep edits apart until
  merge.
- **Window.** Claims are read from the last 14 days of comments; re-claim to renew an
  older one.

## Owning a PR

**The agent that opens a PR owns it until it merges or is closed.** A claim covers the
issue; the review between opening a PR and `train add` needs an owner too. The owner:

- **answers each review round**: fixes the whole batch, replies to each finding with the
  mechanism and a witness, and pushes once per round (then asks for the next review);
- **queues it** with `train add` once the head has Codex's 👍 or a recorded clean review
  and every finding is answered, and **re-queues it after a drop**: when the train
  ejects or removes it, fix what the train's comment names (the failing check; merge
  main only for a conflict), push, and `train add` again (pass `--head` with the sha you
  pushed, or let it compare with `git ls-remote`; see Merge train). A removal no push fixes
  (a fork, a `.github/workflows/` change while the train has no app token, a base other
  than the default branch) goes to the owner instead;
- **hands off explicitly** if it has to stop: `release` the claim and
  `send --to <next owner>` (or the lead who dispatched it) naming the PR, its head and
  what is open.

An agent that cannot stay alive until the merge (a one-shot subagent, a cloud session
with a fixed budget) queues with `coord train add <pr> --owner-fallback <lead>`, naming the
dispatching lead's `COORD_AGENT`: the train-add record carries it, and the train's held,
ejected and removed notices go to the worker, the lead and the conductor. "After
`train add`, stop" means stop polling, not stop owning: the train's comments and anyone who
needs the owner reach it through `inbox`.

**Taking over a stalled PR** (its owner has stopped, or left findings unanswered):

1. Make sure the owning session has stopped, so two agents are not pushing to one
   branch. `send --to <owner>` is only a pull-delivered message, so wait for its reply
   or its `release`; go ahead without one only if you stopped the session yourself, or
   its dispatcher confirms it has ended.
2. Fetch its branch into a worktree under a local branch of your own:
   `git fetch origin <branch>` and
   `git worktree add -b takeover/<branch> ../<dir> origin/<branch>`. Push with
   `git push origin HEAD:<branch>`.
3. Re-claim the issue: `coord claim <issue> --force --branch <branch> --files … --note
   "taking over #<pr> from <owner>: <why>"`.
4. Work the open round as the owner would: one commit, one push, on top of the existing
   head. Never rebase, amend or force-push another agent's branch.

## Messages

`send --on N` posts on issue or PR N and reaches whoever works on it. `send --to X`
without `--on` posts on the mailbox issue (label `coord:mailbox`, created on first
use). Addresses: an exact agent id, a prefix ending in `*` (`codex@*`,
`claude@workstation:*`), or `all`.

`inbox` shows, since your last read: messages addressed to you (any `coord` record with a
`to` list, which includes the merge train's held notices), and every comment
(including Codex review comments) on the issues you claim and on the PR of your current
branch. Delivery is pull-based: you see a message when you run `inbox` or a session
starts, so expect a delay of minutes, not seconds.

## Session start

A `SessionStart` hook (e.g. `.claude/hooks/coord-board.sh`) can put `coord board --brief`
into every session's context and fail open/silent if `python3`, `gh`, authentication or the
network is missing. For harnesses without hooks, instructions such as `AGENTS.md` can ask
the agent to run `coord board` first.

## Merge train

GitHub's built-in merge queue is not available for a private repository owned by a
personal account, so the train is built from Actions parts
(`coordlib/train.py`, `train_step.py`, and your `train_workflow`):

1. **Queue.** `coord train add <pr>` labels the PR `train` and records the head it was
   added at. Adding asserts the PR is not an owner-only carve-out; carve-outs carry
   `needs_owner_label` (default `needs-owner`) until the owner adds `owner_approved_label`
   (default `owner-approved`) with `coord train approve`.

   A push reaches the branch before GitHub's PR head catches up, so `train add` compares
   the head GitHub reports for the PR with the branch's head on `origin` (`git ls-remote`,
   which spends no API budget); while they differ it re-reads GitHub's head, at most twice
   and five seconds apart, and if they still differ it adds nothing and prints
   `head still propagating: GitHub <sha>, remote <sha>; re-run train add`.
   `train add <pr> --head <full-sha>` names the head you pushed instead: it records only
   once GitHub shows that head (same bounded re-read) and otherwise refuses with both shas.

   **After `train add`, the worker is done: stop.** Do not poll `train status` or the PR
   for the merge; `train add` prints whether the PR is ready or held and why. The train
   comments on the PR when it tests, merges, splits, ejects or removes it, and when it
   holds it.

2. **Readiness**, checked when a batch is built and again, from freshly read PR state,
   just before trunk moves: Codex 👍 on the current head AND Codex's pinned summary
   comment naming that head as reviewed and completed, or a recorded clean review of that
   head, no unanswered findings, CI not red at the head, not a draft, and no GitHub list
   read only in part. A failed check whose first annotation begins `"The job was not started
   because"` (a job GitHub refused to start for billing/limits) counts as not run: neither
   red nor green, shown on the board as `"N not run (billing)"`, unless an earlier attempt
   of that check at the head failed for real or could not be read.

   A PR carrying `needs_owner_label` also needs owner approval of its current head
   (`coord train approve`); the `owner_approved_label` label alone does not count, because
   labels survive pushes. PRs changing paths listed in `owner_only_paths` are held the same
   way, label or not, and so is a PR that changes `.github/workflows/` when the step holds
   an installation token (`COORD_TRAIN_APP_TOKEN`; without it that PR is taken out for a
   hand merge).

   Instead of Codex's 👍, a clean review of the current head by any agent counts, recorded
   with `coord train review <pr> --head <full-sha> --by <agent> --note "…"`. `--head` is
   the full 40-character SHA of the head that was reviewed, never a prefix; the command
   refuses unless it equals the PR's head, and `coord train approve` takes and checks
   `--head` the same way. `substitute-review` is the command's old name and still works,
   and `review-substitute` / `alex-approve` records still count alongside `review-clean` /
   `owner-approve`. A clean-review or approval record counts only when GitHub lists a
   trusted user (`owner_login`, defaulting to the repository owner) as its author, never a
   bot or another account: anyone who can comment can write a marker. That is why
   attestations never run on the app identity.

   A PR whose head moved, that git confirms conflicts with trunk, or comes from a fork is
   taken out with a comment, and so is one that changes `.github/workflows/` when the step
   has no app token. GitHub's asynchronously computed `CONFLICTING` value alone only holds
   it (`merge-check`), and the step verifies the merge locally against pinned trunk before
   removing it.

   **Held notices.** The first time the train holds a queued PR for a given `(head, reason
   class)`, it comments once on the PR, stating the reason and what clears it, addressed to
   the agent that added it (and its `--owner-fallback`, if any) and to `conductor@*`. The
   classes are fixed (`train.HOLD_CLASSES`: `draft`, `partial-data`, `needs-owner`,
   `no-review`, `unanswered-findings`, `ci-red`, `head-moved`, `conflict`, `merge-check`).
   On the Actions train, only a comment `github-actions[bot]` posted whose marker names
   `train@actions` counts, so a forged marker never silences it. Holds only the owner can
   clear (`needs-owner`, `partial-data`) also @-mention `owner_mention`.

3. **Batch.** Up to three ready PRs, in the order they were added, are merged onto trunk
   as `--no-ff` merge commits on `train/<stamp>` (computed with `git merge-tree`, no
   checkout). Before advancing a branch, the train matches its name and exact tip to
   its initial CI reservation or testing comment on the first PR. It skips branches
   without either record. The reservation precedes the push, so an interrupted build
   can still resume. One `ci_workflow` run gates it (dispatched with `lanes=<ci_lanes>` if
   configured). Only a run the train dispatched itself counts (`workflow_dispatch` whose
   initial actor is `github-actions[bot]`); someone's narrower dispatch on the same commit
   never moves trunk, and `coord ci` refuses `train/` branches.

   **Light before heavy, bounded:** if `lane_classifier` is configured, trunk's own
   selector script classifies each ready PR as light or heavy, and a batch never mixes the
   two. When both are ready, at most one light batch may go ahead of the oldest ready heavy
   PR and then heavy goes next.

4. **Green:** trunk is fast-forwarded to exactly the tested commit through the API (`force:
   false`, so it fails if trunk moved and the batch is rebuilt). Because a ref update does
   not fire `Closes #N`, the step closes each still-open issue a merged PR names itself,
   then posts a receipt. **Red:** the step reads the failed and cancelled jobs and their
   annotations and classifies the failure (`train.classify`), setting aside `verdict_job`
   and `wake_job`. A person's cancel (`canceled by @…`) abandons the batch for a rebuild. A
   timeout (`exceeded the maximum execution time`) is re-run once, then abandoned with a
   `timed-out` receipt. CI infrastructure failures (runner id 0 or no steps, failure in
   `"Set up job"`, or annotations indicating a job never started or a runner shut down/lost
   contact) are re-run up to two more times than a real failure, then abandoned with an
   `infra` receipt. Neither splits or ejects PRs; both notify the conductor. A real failure
   is re-run once; then a multi-PR batch is split (each PR labelled `train:solo` and tested
   alone) and a lone PR is taken out with a comment naming the failed jobs. Every re-run
   reserves an attempt from the daily budget.

Each workflow run is one step and never waits on a gate. It is woken by `coord train add`,
by the CI workflow's wake job at the end of each batch's CI (since `workflow_run` does not
fire for runs dispatched by `GITHUB_TOKEN`), by the step that closes a batch, by a cron
schedule as a backstop, and by the conductor when the train is asleep.

**Repository variables:**

| Variable | Values | Effect |
|---|---|---|
| `COORD_TRAIN_MODE` | `dry-run` (unset) or `live` | dry-run tests batches and reports "would merge"; live moves trunk |
| `COORD_TRAIN_DAILY_LIGHT_ATTEMPTS` | nonnegative integer (default 12) | light CI attempts admitted in 24 hours, including retries |
| `COORD_TRAIN_DAILY_HEAVY_ATTEMPTS` | nonnegative integer (default 12) | heavy CI attempts admitted in 24 hours, including retries |
| `COORD_TRAIN_DAILY_RUNS` | optional nonnegative integer | an additional combined attempt cap |

**Limits:**
- The Actions `GITHUB_TOKEN` cannot push `.github/workflows/` changes. Optionally,
  `train_workflow` can mint an installation token (`COORD_TRAIN_APP_TOKEN`) from a GitHub
  App with Contents and Workflows write permissions to move refs for workflow-changing
  batches; such PRs still require owner approval (`coord train approve`). Without that
  token, PRs touching `.github/workflows/` are taken out of the train for a hand merge.
- Moving trunk via the API with `GITHUB_TOKEN` starts no push-triggered CI run (the batch
  already tested that exact commit). Moving trunk with a GitHub App token does trigger
  ordinary push workflows.

### Receipts and resource accounting

Each new batch carries an immutable `Train-Plan` commit trailer: base, member PR/head
pairs, queue times, batch capacity, and the lanes selected from trunk. Terminal train
comments also carry a machine-readable receipt with the exact tested tip, run id,
attempt and outcome.

Before every dispatch or failed-job retry, the train records a `ci-requested` reservation.
The rolling attempt budget reconciles those reservations with Actions runs, including old
runs retried in the current window and requests whose response was lost. Uncertainty costs
an attempt; unknown historical classifications charge the heavy pool.

`coord train report --days N` is an explicit one-shot report showing queue-to-batch waiting
samples, batch occupancy, attempts/retries, split/ejection/merge receipts, hold notices,
actual job minutes by runner and batch class, and API calls used to build the report.

## The conductor

### Runtime boundary and read-only diagnosis

`coordlib/runtime.py` owns CLI invocation and result normalization.

`coord conductor diagnose` is a bounded read-only diagnostic operation. It defaults to
Codex, accepts `--runtime claude`, and uses the CLI's default model unless `--model` is
specified. It reads the board or a focused `--input` file and runs in a fresh temporary
directory. Codex runs ephemeral with ignored user configuration/rules, a read-only sandbox,
approvals disabled, and tool providers explicitly disabled (connectors, plugins, hooks,
shell, browser, delegation, web search). Claude diagnosis disables tools and external
customizations. GitHub tokens are removed from the child environment; model authentication
stays with the chosen CLI.

The escaped prompt is limited to 120,000 bytes; larger reports need a focused `--input`
file. The result must be a completed runtime response containing exactly `summary`,
`needsRepair` (boolean), and `reason`. Truncated, failed, or malformed output cannot
trigger a handoff. The default timeout is 300 seconds (maximum 3600); timeout kills the
process group. Only the explicit `--handoff` flag sends an advisory repair message to
`conductor@*`, and only when `needsRepair` is true.

Before upgrading Codex CLI, you can verify the zero-tools boundary locally without model
spend:

```bash
COORD_TEST_CODEX_RUNTIME=1 python3 -m unittest discover -s tests -p test_runtime_boundary.py
```

### Watcher loop

One agent answers the others, keeps the merge train moving and repairs CI. Agents reach it
with `send --to 'conductor@*'`; the train's held, ejected and removed notices and its
`timed-out` and `infra` receipts go to it too. It is built to cost nothing while nothing
happens (`coordlib/conductor.py`):

- **A watcher, not a session.** `conductor/install.sh` installs a macOS launchd agent that
  runs `coord conductor tick` every few minutes. The tick is plain Python: it checks the
  local machine and, if it is the leader, reads the conductor's inbox and periodically
  checks the train queue, `train/*` branches, recent CI and merge-train runs, runners, and
  configured routing variables. Most ticks end without invoking any model.
- **A fresh run per wake.** When something needs judgement, the tick starts a fresh
  `claude -p --agent conductor` run with the reasons in its prompt. Memory lives in GitHub
  plus a repository-specific notes file. For `octo/tools`, the default directory is
  `$XDG_STATE_HOME/<state_dir_name>-conductor/repo-octo%2Ftools`. It contains
  `notes.md`, the review queue, and `wakes.jsonl`. Set `CONDUCTOR_STATE_DIR` to keep
  using an existing directory dedicated to that repository.
- **Cheap model first.** Messages and train holds wake the triage model
  (`CONDUCTOR_TRIAGE_MODEL`, default `sonnet`); red CI, a failed train workflow, an offline
  runner, queued control jobs or a low disk wake the repair model (`CONDUCTOR_REPAIR_MODEL`,
  default `opus`). A triage run that finds repair work ends with `ESCALATE:` and is rerun
  on the repair model.
- **What it does without a wake.** Wakes an asleep merge train (not in dry-run), escalates
  `timed-out`/`infra` receipts and suspected poisoned runners (3+ fast failures under 90 s
  within an hour while another runner can take the label), and maintains the leader lease.
- **Bounded.** One run at a time; at most `CONDUCTOR_MAX_WAKES_PER_DAY` (24) wakes a day;
  a run is killed after an hour; a reason handed to three failed runs is escalated to the
  owner instead of retried.
- **Model-free standby.** `conductor/install.sh --no-wake` installs the watcher with
  `tick --no-wake`; Claude need not be installed. It still participates in the leader
  lease, maintains heartbeats, wakes an asleep merge train and posts operational
  escalations, handing off pending repair/review work to a human or active Codex session.
- **Guardrails beyond the prompt.** The prompt fences quoted comments as data. Each run
  gets a `permissions.deny` list (`conductor.DENY`) and an optional `PreToolUse` guard hook
  configured via `conductor.guard_hook`. A guard hook is a blocking tripwire, not an OS
  sandbox; a branch ruleset on trunk (allowing only the train's Actions token, plus an
  optional workflow-pushing app) is what enforces merge-train-only landing on GitHub.
  The watcher refuses to wake a run while files in `owner_only_paths` or conductor control
  files are modified in its checkout.
- **Several CI boxes.** Run `install.sh` on each. Every watcher keeps one heartbeat comment
  on the conductor issue (label `coord:conductor`) and edits it each tick; the oldest one
  that beat in the last 15 minutes is the leader. Standbys monitor their own disk and take
  over if the leader goes quiet.
- **Reaching the owner.** `coord conductor escalate`, exhausted retries and the daily cap
  post on the conductor issue (visible on the board for 24 h) and optionally dispatch
  `notify_workflow` so GitHub notifies the owner as `github-actions[bot]`.

The conductor's checkout stays detached at the default branch: the watcher fast-forwards it
periodically when clean, so merged changes take effect on the next tick.

## Reviews on request (sidechain reviewer)

When `reviewer_bot_login`, `reviewer_trigger` and `reviewer.agent_file` are configured, a
PR comment matching `reviewer_trigger` (e.g. `@my-bot review`) triggers an adversarial
read-only review on the CI box, posted by the configured GitHub App (`coordlib/reviewer.py`).
Append a model name (`sonnet`, `haiku`, `model=…`) to override the default (`opus`).

- **Pick-up.** No webhook is required: the conductor's watcher detects the trigger in
  comments it already reads each tick (at most one review a tick, on the leader, never in
  the same tick as a conductor wake). Tags inside quotes, code blocks, issues, or bot
  comments are ignored.
- **The run.** A fresh `claude -p --agent reviewer` reads and tests the PR's head in a
  throwaway worktree. It starts in the conductor's checkout at the default branch so hooks and
  settings come from trunk; every `.claude` entry is stripped from the PR's throwaway tree
  first and `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` is unset. The run receives **no
  GitHub credential** (empty `gh` config dir, isolated git config, stripped token env
  vars), runs Bash inside Claude Code's OS sandbox (no network, no writes outside the PR
  tree and scratch, no reads of app config, token cache, or `~/Library/Keychains`), and
  withholds non-Bash tools so every file open is enforced by the OS sandbox.
- **The verdict is code's.** Pass means no P0, P1 or P2 finding; P3 notes do not block. A
  `changes` verdict holds that head in the train even if Codex approved; only a new push
  answers it.
- **Who posts.** The configured GitHub App posts the review with a `review` marker. If the
  app has no token, the request waits; a review is never posted on the user's identity.
- **A pass is the head's clean review.** For a pass, the watcher's code records a
  `review-clean` attestation on the user identity (since the train accepts attestations
  only from trusted users), applies `reviewed`, and posts the review as the app.
- **Bounds.** One review per `(PR, head, model)`; `CONDUCTOR_MAX_REVIEWS_PER_DAY` (8) a
  day; `CONDUCTOR_REVIEW_TIMEOUT` (45 min) a run; three failed runs post a reply and stop.

## Outages

When a model harness hits a shared usage limit, `coordlib/outage.py` records an outage on
the conductor issue. The conductor's watcher posts one automatically on a usage-limit
refusal, using the reset time in the error or `claude_weekly_reset` (`COORD_CLAUDE_WEEKLY_RESET`).
Anyone may post one with `coord outage start claude` or end it with `coord outage end claude`.

While an outage lasts:
- the board and session-start brief show `OUTAGE  claude agents are down until …`;
- `send` to a down agent tells the sender when it will read the message;
- claims held by down agents do not age toward stale;
- the conductor wakes nobody, retains pending mail, and alerts the owner once per reason
  per day so a human or another harness can take over.

## Safety: what can go wrong, and how to stop it

| Part | What it can write | Worst case | Undo |
|---|---|---|---|
| `coord` commands | comments, labels, mailbox issue, CI dispatches | comment noise; extra CI runs; spending an hourly API quota | delete comments or labels; cancel runs; quota refills within the hour |
| GitHub App | in app mode, what `coord` commands write; read token writes nothing; ops token has Issues/PRs/Actions write. If the app also holds Contents/Workflows write for workflow-changing train batches, protect its private key accordingly and gate sensitive deployment workflows with GitHub Environments | leaked ops token (valid up to 1 h): comment/label/workflow-dispatch abuse; leaked private key: full app permissions until revoked | `COORD_APP=off`; suspend/uninstall app or rotate private key; remove Contents/Workflows permissions if unused; audit branch pushes |
| session-start hook | nothing (read-only) | slower session start | remove hook from settings |
| merge train, dry-run | comments, labels, `train/*` branches, CI dispatches and re-runs | bounded batch CI attempts | disable the workflow |
| merge train, live | also: fast-forward trunk | a PR that was not ready lands on trunk | `git revert -m 1 <merge>` |
| conductor watcher | comments on conductor issue; model wakes within standing orders | needless wakes (capped per day) or misread message | `coord conductor pause --for 1d`; `conductor/install.sh --uninstall` |

What makes trunk safe:
- The only write to trunk is a REST ref update with `force: false` (fast-forward only).
- The only branches deleted are its own `train/*` batches carrying a `Train-Batch` trailer.
- Trunk receives the exact commit that passed batch CI.
- Any branch ruleset restricting pushes on trunk to the train's Actions token (and optional
  app) enforces merge-train-only landing on GitHub itself.

Off switches, instant and reversible:

```bash
gh workflow disable merge-train.yml        # stop the train
gh variable delete COORD_TRAIN_MODE        # back to dry-run: trunk cannot move
coord conductor pause --for 1d             # pause every box for up to 14 days per record
```

## Code

`coordlib/`: `gh.py` (transport and identity), `config.py` (per-repo configuration),
`apptoken.py` (app mode and the app's tokens), `marker.py`, `identity.py`, `review.py`
(Codex and CI rules), `snapshot.py` (the board), `render.py`, `cache.py`, `budget.py`
(the API budget readings per principal, and the guard), `outage.py`, `lease.py` (the
conductor's heartbeats and pauses), `doctor.py` (preflight), `runtime.py` (model adapters),
`train.py` / `train_step.py` (batch planning and execution), `train_metrics.py` (receipts
and resource accounting), `conductor.py` (the watcher), `reviewer.py` (reviews on request),
`cli.py`. The conductor's installer is `conductor/install.sh`. Tests:
`cd tests && python3 -m unittest discover -s . -p 'test_*.py'`. The app tests use a
throwaway key, a fake `security` and a fake GitHub; none reads the real Keychain.
