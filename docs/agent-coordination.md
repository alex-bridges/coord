# Agent coordination (`coord`)

Several agents work in one GitHub repository at once: Claude Code and Codex sessions, Claude
cloud sessions, and CI bots. `coord` gives each of them the same picture of what is in
flight, and a way to talk to the others. It is a Python 3.9+ standard-library tool over
the `gh` CLI. To use it in a repository, start with [adopting.md](adopting.md).

> **How to read this document.** It was written in the INISA repository, where coord was
> developed, and the protocol, the commands, the merge train, the conductor, outages and the
> API budget are described as they run there. Names that belong to that deployment (the
> `inisa-sidechain` GitHub App, `Alex` as the repository owner, Tycho and Eidolon as hosts,
> `ci.yml`, `merge-train.yml`, `MK4_RUNNER_*` variables, the `.claude/` files, issue and PR
> numbers, dates) are INISA's values for settings that are configurable, or INISA-only
> behaviour. In your repository, "Alex" is the `owner_login` (default: the repository's
> owner), and workflow, job and path names come from `.coord/config.json`. Where behaviour
> exists only in INISA's own files (its guard hooks, agent prompts, runner tooling), the
> text says so or points at the file; see Example: INISA configuration at the end. The
> commands are shown as `coord ...`; from a clone without installing, run `./coord ...`.

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
| `reviewer_bot_login` | none: reviewer disabled | the reviewer app's login, e.g. `my-bot[bot]` |
| `reviewer_trigger` | none | the PR comment that requests a review, e.g. `@my-bot review` |
| `ci_workflow` | none: train disabled | the CI workflow file the train dispatches and reads |
| `train_workflow` | none: train disabled | the workflow that runs `coord train step` |
| `notify_workflow` | none | a workflow dispatched to notify the owner (escalations, outage alerts) |
| `verdict_job`, `wake_job` | none | the CI workflow's aggregate jobs (red whenever any lane is) |
| `ci_lanes` | none: no dispatch inputs | the `lanes` input the train dispatches the gate with |
| `owner_only_paths` | `[]` | path prefixes whose changes wait for the owner's approval of the head |
| `lane_classifier` | none: every PR is one lane | `{"script": ..., "rules": ...}`, paths at the default branch of main's own light/heavy selector |
| `runner_variables` | `[]` | repository variables that route CI to runners, which the conductor may read and set |
| `conductor` | all unset | `agent_file` (prompt), `instructions_file`, `guard_hook`, `deny` (extra patterns) |
| `reviewer` | all unset | `agent_file`, `guard_hook` for the reviewer runs |
| `claude_weekly_reset` | none | the weekly usage reset for outages, e.g. `Thu 12:00 America/New_York` (`COORD_CLAUDE_WEEKLY_RESET` overrides) |
| `keychain_service` | `coord-github-app` | the Keychain item holding the GitHub App's key |
| `state_dir_name` | `coord` | per-user state under `~/.cache/<name>`, `~/.config/<name>`, `~/Library/Caches/<name>` |
| `legacy_state_dir_names` | `[]` | older names still read when the new one has nothing, never written |

Where this document says `train.ALEX_ONLY_PATHS`, read `owner_only_paths`; the conductor
adds its `agent_file`, `instructions_file` and `guard_hook` to that list. The repository
is read from the origin remote (`COORD_REPO` overrides); coord says so when it cannot tell.

## Claude cloud sessions

Probed in INISA on 2026-09-24 (five probes): a Claude cloud session has no `gh` but
has a proxied token. From one, `whoami`, `board` (REST only, paged), `send`, `inbox` and
`train status` all work (verified at deef6a004), and a claim on a closed issue is refused
without posting. Codex cloud has not been probed. Cloud sessions have no GitHub App: no
config and no key, so `coord` there runs on the proxy's token exactly as before, and spends Alex's user budget (see The API budget).

## The model: compute the picture, don't maintain it

Nothing in this system is a stored status that someone has to remember to update.
Every `coord board` call builds the picture from what GitHub already knows:

| The board shows | Read from | Written by |
|---|---|---|
| open PRs: branch, head, files, CI result, Codex state | GraphQL, one query per 25 PRs | agents pushing and opening PRs, GitHub, the Codex bot |
| what Tycho is running and what waits | Actions runs and jobs | GitHub |
| who is working on which issue, on which branch, expecting which files | marked comments (`coord claim`) | agents |
| messages | marked comments (`coord send`) | agents |
| the merge train | the `train` label, `train/*` branches, marked comments | `coord train add`, the train workflow |

Only the last three rows are new writes, and each is an ordinary issue or PR comment
that a human can read on GitHub. If an agent dies, nothing is left locked: its claim
simply stops showing activity and turns *stale* after 48 hours.

A board costs about a dozen API calls: one GraphQL query per 25 open PRs, a few for
the Actions queue (plus one per running workflow run), and one per 100 comments of the
last 14 days, where claims and messages live (459 comments, five pages, on
2026-09-24). Each identity has one hourly quota (5,000 REST and 5,000 GraphQL points),
shared by every agent that uses it (see Identity and The API budget), so the board is
cached per machine for 60 seconds (5 minutes for the session-start hook). `--fresh`
bypasses the cache.

## Commands

```bash
coord board            # everything in flight; --brief, --json, --fresh
coord whoami           # this agent's id, repo, branch
coord doctor           # offline identity, runtime, transport and cache preflight
coord doctor --online --json # verify one repository read; never tests writes
coord claim 123 --files Packages/CoreKit/Sources/Foo.swift --note "one line of context"
coord release 123 --note "handed to …" 
coord send --on 123 "question about the ramp API"          # to whoever works on #123
coord send --to 'codex@*' "rebasing main in 10 min"        # to every Codex agent
coord inbox            # messages to you + comments on your claimed work; --peek, --all
coord ci mk4           # queue ci.yml lanes on the current branch
coord train add 984    # queue a ready PR for the merge train; then stop (see Merge train)
coord train add 984 --head <full-sha>   # the same, refused unless GitHub shows the head you just pushed
coord train status     # the current batch and each queued PR's readiness; a one-off look, never a poll
coord train report --days 7 --json # one-shot receipts, queue timing and measured runner minutes
coord train review 984 --head <full-sha> --by <agent> --note "…"   # record a clean review of that head by any agent (old name: substitute-review)
coord train approve 984 --head <full-sha> --note "…"              # on Alex's word: approve a needs-alex PR at that head
coord send --to 'conductor@*' "the train has held #984 for a day"   # to the conductor
coord conductor status  # its hosts, pause, escalations, and this machine's recent wakes and tokens
coord conductor diagnose --runtime codex --input report.json --json # read-only diagnosis
coord conductor diagnose --runtime claude --handoff # diagnose the board; send repair advice if needed
coord conductor pause --for 2h --note "…"      # no model wakes (Alex driving it himself); resume ends it
coord conductor escalate "…"                   # put something in front of Alex on the board for a day
coord outage start claude --note "weekly limit" # every Claude agent down until the weekly reset (--until to override)
coord outage end claude
coord token status     # app mode on or off and why; each cached app token's expiry and permissions
coord token exec -- gh pr list   # run a command with an app token in its environment only
```

Without `gh`, `coord` talks to the API directly when `GH_TOKEN` or `GITHUB_TOKEN` is
set (`COORD_TRANSPORT=http` forces it).

**Claude cloud sessions** have no `gh` and a token whose value starts with `prox`: the
session's proxy swaps in real credentials (admin on the repository; no OAuth scopes are
reported, so `coord` never inspects the token). The proxy also refuses paths it
cannot canonicalize: numeric `repositories/<id>/…` paths (GitHub writes its next-page
links that way; `coord` rewrites them to `repos/<owner>/<name>/…`) and paths ending in
`/`. It refuses GraphQL with HTTP 403, so `coord` reads through REST instead: at once when `CLAUDE_CODE_REMOTE=true`,
after the first 403 anywhere else, or always with `COORD_REST_ONLY=1`. The REST path
builds the same PR data the GraphQL query returns, so every rule is computed once. It
costs about seven calls per open PR instead of one query (28 against 8 for two PRs), so
it is not the default. REST cannot tell whether a review thread was resolved; there a
thread counts as answered only if someone replied, which the house rule requires
anyway.

Exit codes: `0` success, `1` usage, `2` conflict (someone else holds the claim, or
you lost a simultaneous claim), `3` GitHub or `gh` error, `4` not found, `5` the API
budget guard refused (see below).

## The API budget

Budgets are counted per **principal**, the account whose counter a call spends, not
per token:

| Principal | Who spends it | Recorded as |
|---|---|---|
| Alex's user counter | Alex's own `gh`, every agent's `gh`, `coord` outside app mode, in app mode the commands that stay his (`train approve`, `train review` and its old name `train substitute-review`, a hand-run `train step`, `conductor tick`), and the Claude cloud proxy's token | `user` |
| The GitHub App installation | `coord`'s reads and operational writes in app mode (see Identity). Its hourly tokens are one budget, not several | `app:<installation id>` |
| Actions | the merge train's `GITHUB_TOKEN` (1,000 an hour per repository) | not recorded |

Before the app, every agent and Alex's own `gh` shared the one user budget of 5,000
REST calls an hour. On 2026-09-25 it ran out twice, at 5,229 and about 7,700 calls an
hour, mostly from cloud workers polling `coord train status` after their PRs were
queued (a refresh from a cloud session, REST only, costs dozens of calls). Each time
every agent and Alex's `gh` were locked out until the reset. The app takes coord's own
local traffic off that budget; cloud sessions still spend it.

`coord` reads the budget from the `X-RateLimit-Remaining`, `-Used`, `-Limit`, `-Reset`
and `-Resource` headers of its real responses (`-Limit` stands in for a missing
`-Used`) and keeps the last readings per principal and resource (core and graphql) in
the per-machine cache, which every worktree on a Mac shares. Alex's are in
`budget.json`, exactly as #1033 writes it (`{"core": {…}, "graphql": {…}}`), because
some worktrees run a coord from before the app. The app principals' are in a file of
their own, `budget-app.json` (`{"app:<installation id>": {"core": {"windows": […]}, …}}`,
under its own lock): an older coord replaces `budget.json` without a lock, so anything
else kept there could be lost or rolled back between its read and its write. Earlier
heads of #1046 kept the app under `"app:<id>"` in `budget.json`; no merged coord did, so
such keys are ignored, not moved, and the next write of Alex's readings drops them. A
file that cannot be read (corrupt, or nested too deep) counts as no reading and is
replaced by the next write. The
`gh` transport sees no headers, so before a gated command on Alex's identity it probes
with one `gh api -i` call when the reading is more than two minutes old; the app's
calls go over HTTPS and record their own. Never the `rate_limit` endpoint: that night it
reported 5000/5000 while real calls got 403.

**The app's counter is two counters.** Probed 2026-09-25 (#1035): 25 calls in a row
alternated between two windows whose resets were 3 seconds apart, one reading `used` 32
to 40 and the other 23 to 38, across two different tokens. One reading can therefore
be lower than the one before it. For the app, `budget-app.json` keeps the highest `used`
seen for each reset still in the future, and **left = 5,000 minus the sum of those
maxima** (`COORD_BUDGET_APP_LIMIT`). A lower reading never replaces a higher one for the
same reset, and a window whose reset has passed drops out. Alex's counter showed one
window, rising steadily, in the same probe, so his keeps the rule below.

**Left, for Alex's counter,** is not `Remaining` alone. The cloud proxy's token
authenticates as the same user but reports `X-RateLimit-Limit: 15000` where Alex's gh
reports 5000, and `Used` is one counter shared by both (probed 2026-09-25 06:07Z). With
the 5,000 that locks out Alex's gh spent, a cloud session still reads about 10,000
remaining. So left is the lower of `Remaining` and `COORD_BUDGET_USER_LIMIT` (5000)
minus `Used`, from the latest reading. The board's header shows it as
`API: N left (resets HH:MMZ)`; in app mode it shows the app's budget first and then
Alex's: `API (app): N left (resets …) · Alex: M left (resets …)`.

A reading whose reset has passed counts as a full budget, and so does one more than an
hour old, since a window never lasts longer. A budget file of the wrong shape counts as no
reading: the guard lets commands run, and the next `gh` probe replaces it.

The guard checks what is left of the core budget of the principal the command spends
(the app's in app mode, Alex's for the commands that stay his) before each command, with
the same floors for both. An exhausted app never spills onto Alex's budget: reads serve
the cached board and writes are refused, as below. The read that finds the budget
exhausted (a 403 or 429 whose headers put it below the read floor) serves the cache too.

| Left (core) | `board`, `board --brief`, `train status`, `inbox` | `ci` | `claim`, `release`, `train add/remove/review/substitute-review/approve` | `send` | `train step` |
|---|---|---|---|---|---|
| below `COORD_BUDGET_READ_FLOOR` (1500) | served from the cached snapshot, whatever its age, with a one-line warning and no API call; no snapshot: refused, exit 5 | dispatches (one call), does not read the queue back | run | runs | not gated |
| below `COORD_BUDGET_HARD_FLOOR` (300) | as above | refused, exit 5 | refused, exit 5 | runs | not gated |

`send` always runs, so an agent can always report. `train step` runs in Actions on the
Actions token, a separate budget. `coord token exec` is refused below the hard floor
and runs with a warning below the read floor; `coord token status` makes no call. The
child's own API calls do not update coord's saved budget reading: their spend becomes
visible when a later coord API response records the installation's headers. Repeated
`token exec` commands can therefore start against the same earlier reading; this
wrapper is not a live quota meter for the child (#1052).
The session-start hook passes on one notice as a line of its own, the budget's warning or
refusal (the app's budget uses the same words), and still starts the session. It does
not pass on an app failure's notice today; a board served from the cache after one
says so in its own first line instead (see Identity).

## Identity

`coord` uses two GitHub identities (#1035):

- **The app, `inisa-sidechain[bot]`,** for coord's reads and its operational writes, on
  a Mac with the app installed. Reads use a read-only installation token (every
  permission `read`). Operational writes use a second token with only what they need:
  Issues write, Pull requests write, Actions write, and Metadata read.
- **Alex's login, `alex-bridges`,** for everything else: attestations, a hand-run train
  step, the conductor's tick, and all of coord when app mode is off.

Why both Issues and Pull requests write: GitHub's docs say comments and labels need "at
least one of" the two, but GitHub enforces the one that matches the number. Measured on
2026-09-25 around 23:15Z with requests that could not change anything (invalid bodies, a
missing label): Issues write alone was refused (403, "Resource not accessible by
integration") for a comment, a label add and a label remove on PR #1046, and worked on
issue #991; Pull requests write alone was the mirror image; both together worked on
both.

**App mode** is on when all of these hold:
- the non-secret config `~/.config/<state_dir_name>/github-app.json` exists;
- the command is not running in GitHub Actions (`GITHUB_ACTIONS` is not `true`): the
  train keeps `GITHUB_TOKEN`;
- `COORD_APP` is not `off`;
- `GITHUB_API_URL` is unset or `https://api.github.com`: the app's JWT and tokens go to
  GitHub and nowhere else;
- the repository is the one the app config names (`repo`).

`coord token status` says which condition turned it off.

Only a config path that does not exist counts as absent. A path coord cannot check (a
folder above it without search permission) or a config it cannot read or parse is a broken
config: app mode stays on and every app call refuses, naming the cause and `COORD_APP=off`.
Turning app mode off there would run the command as Alex, a fallback.

**`COORD_APP=off`** is the deliberate override: coord runs on Alex's gh/keyring
identity for everything, exactly as before the app. It is the way to recover by hand
when the app cannot be used, and nothing ever switches to it on its own.

| Write | Command | Identity in app mode | Why |
|---|---|---|---|
| claim and release comments | `claim`, `release` | the app (ops token) | operational; the marker names the agent, and `snapshot.resolve_claims` never reads the author |
| messages, the mailbox label and issue | `send` | the app (ops token) | operational; `inbox` takes the sender from the marker |
| train-add record, `train` label, the labels the train uses, the train wake | `train add` | the app (ops token) | operational; `snapshot.train_queue` and `train_step.train_added` read the marker, not the author |
| train-remove record, `train` label removed | `train remove` | the app (ops token) | operational |
| `ci.yml` dispatch | `ci` | the app (ops token) | operational; the run's actor is the app, and `train.is_gate_run` counts only `github-actions[bot]`, so it never gates a batch, as before |
| review-clean record, `reviewed` label | `train review` (and its old name `train substitute-review`) | Alex | an attestation, not an operation; the train counts the record only when Alex's login posted it, never a bot |
| alex-approve record, `alex-approved` label | `train approve` | Alex | an attestation, and only on Alex's word; counted only when Alex's login posted it, never a bot |
| batch branches, main, train comments | `train step` run by hand | Alex | it moves and deletes branches, which needs Contents write, which the app does not have; in Actions the step uses `GITHUB_TOKEN` |
| outage records, and the conductor issue if it is missing | `outage start`, `outage end` | the app (ops token) | operational; `outage.windows` reads the marker, not the author |
| pause and resume records, escalations and their Notify Alex dispatch; `conductor status` is a board read | `conductor status`, `pause`, `resume`, `escalate` | the app (read and ops tokens) | operations like the others; `lease.pause` and `lease.escalations` read the marker, not the author |
| lease beats, the outage records it finds itself | `conductor tick` | Alex | it lists the Actions runners (Administration read, which the app lacks), edits the comments it posted as Alex, and gates its spending on Alex's budget (`conductor.Watcher.budget_left`) |
| PRs, `@codex review`, review-thread replies, `git push` | not coord | Alex | `review.is_bot` counts a reply from any `[bot]` login as no answer to a Codex finding, and there is no exemption for the app |

Each call gets its token on that one HTTPS request. `GH_TOKEN` is never exported: it
would move `git push` onto the app through `gh auth git-credential`. The token (and, when
minting, the app's JWT) travels in an Authorization header that no redirect copies: a
redirect to another scheme, host or port is refused, and one within GitHub's own origin
gets the credential back (`gh.SameOriginRedirects`).

**Notifications.** App-authored coord comments notify Alex on threads he takes part in
(every agent PR is authored by him). He handles that with notification settings, for
example by unwatching the mailbox and conductor issues and filtering on
`inisa-sidechain[bot]`. Before the app, agents posted as Alex, and his own activity
never notified him. Posting claims, releases, messages and outages as Alex again is one
line in `cli.py` (`USER_IDENTITY = ATTESTATIONS | NEEDS_ALEX | CONVERSATION`).

**Minting.** The app's private key is in the login Keychain (service
`<keychain_service>`, account = the client id, the value the base64 of the PEM). It
is read only to mint, with `/usr/bin/security`, and reaches `/usr/bin/openssl` through
an inherited pipe (`/dev/fd/N`), never a file. openssl signs a JWT that lives nine
minutes; coord finds the installation and mints a token narrowed to INISA and the
kind's permissions, and refuses it unless GitHub granted exactly those. Each kind is
cached in its own file in `~/Library/Caches/<state_dir_name>/` (mode 0600, with its expiry,
installation and permissions) and reused while more than five minutes remain; the
cached path never reads the Keychain, and a lock makes concurrent agents mint once. A
cached token whose permissions do not match its kind is never used. When GitHub rejects
an installation with 404 and a replacement mint succeeds, the cached replacement keeps
the exact rejected ids, including earlier rejections still awaiting cleanup. After
releasing its own mint lock, each token lookup (cached ones too) tries the other kind's
lock without waiting and removes only a token still
naming one of those ids. Later lookups retry skipped cleanup; refreshes and consecutive
reinstalls retain the proof. After a successful cleanup under the other kind's lock,
coord releases that lock and tries its own lock without waiting to retire the proof,
provided its cached entry has not changed. Different ids or mint timestamps alone do
not prove which installation is obsolete, so fresh discovery without a rejection preserves ambiguous
entries until refusal, expiry or remint. The budget principal can still name an old
entry until it is removed or replaced. Other-kind failure holds identify no installation
and remain until their owning kind succeeds or the hold expires.

**No automatic fallback.** When the app has no token, nothing is sent as Alex. That
covers:
- the config cannot be read;
- the Keychain is locked or prompting;
- openssl fails, or the network fails;
- GitHub refuses the mint;
- GitHub answers 401 to a cached token (revoked, or the installation suspended). That
  token is also dropped from the cache.

`board`, `inbox`, `train status` and `conductor status` serve the cached board. The
board's first line, the brief's and `conductor status`'s then begin
`STALE: snapshot from 3h00m ago; GitHub App token unavailable: …`.
Every other command is refused (exit 3), naming the cause and `COORD_APP=off`.

A failed mint is remembered for five minutes in `~/Library/Caches/<state_dir_name>/`
(`<kind>.failed.json`, mode 0600: the time, the cause, the stage, and the client id and
repository it belongs to, never a secret). So a locked Keychain costs one prompt or one
stall, not one per command. A failure getting the key holds both kinds; a refused mint
holds only its own (in the command that hit it, too: a refused read mint does not stop
the ops token). A record for another app config is ignored. `coord token status` shows a
held failure, and deleting the file retries at once.

**`coord token`** never prints a token. `status` shows whether app mode is on and why,
the installation, and each cached token's expiry and permissions, from files only.
`exec [--kind read|ops] -- <command>` runs the command with `GH_TOKEN` in that
command's environment only and exits with its exit code; the kind is `read` unless
`--kind ops` is given. The command inherits no other GitHub token: `GITHUB_TOKEN`,
`GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN` and any `GH_TOKEN` of the caller's are
removed first. `GH_CONFIG_DIR` is left alone, so the command can still run `gh auth
token` and reach Alex's keyring: `exec` is a convenience wrapper, not a sandbox. `gh api` exits 1 and prints `gh: HTTP 304` on a Not Modified
answer, so anything that later sends conditional requests through it must read that as
not modified; an ETag is only valid from the same client with the same `Accept` header.

**Output that changed with the app** (all additions; nothing existing was renamed):
- `whoami` has a `githubIdentity` line (JSON key `githubIdentity`), and its `apiBudget`
  names the app's budget first in app mode.
- `board --json`: `apiBudget.principal` names the budget coord spends (`user`,
  `app:<id>`, or null before the first token), and in app mode `apiBudget.user` holds
  Alex's. A board served from the cache carries `stale` (`ago`, `reason`).
- The board's first line and the brief lead with `STALE: …` when the board came from
  the cache because nothing fresher could be read.
- Errors are printed through a redaction that replaces anything shaped like a token, a
  JWT or key material with `[redacted]`.
- **Tests only:** `COORD_SECURITY_BIN` replaces `/usr/bin/security`, for the tests that
  run the `coord` script as a subprocess. It is honoured only when `COORD_APP_CONFIG`
  names a config that is not a default one by any spelling (a symlink, a hard link, `..`,
  other letter case; under `$HOME` or under the real home from the password database, so
  moving `HOME` changes nothing) and whose client id is not the real app's. Otherwise
  coord refuses (exit 3, "COORD_SECURITY_BIN is set but refused for this config"). It
  never falls back to `/usr/bin/security`, so a test or probe that set a fake binary
  cannot read the real key and mint live instead. The check comes before the token cache
  is read, so such a probe cannot get the real config's cached token either.

**The marker.** The author of a coord comment says nothing about which agent wrote it:
every agent posts as the same app, or as `alex-bridges`. Each coord comment ends with a
marker naming the agent:

```
<!-- coord:v1 {"agent":"claude@Eidolon:agent-coord","branch":"…","files":[…],"issue":989,"kind":"claim","v":1} -->
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

The session id or checkout path is hashed, never published raw. This fixes Codex
worktrees whose different parent directories all contain a folder named `INISA`.
Simultaneous tasks in the **same** checkout must supply different session ids. Resuming
the same task on the same host and checkout keeps its address. Use `COORD_AGENT` when an
address must survive moving between hosts or checkouts. Existing automatic addresses
change once on upgrade; there is no aliasing or inbox/claim migration, because the old
address may have represented several tasks. Finish/release old claims under their
explicit old address before switching. Broadcasts such as `codex@*` still work.

Only the last non-blank line of a comment counts (a
trailing Claude Code footer, which cloud sessions append, is skipped), so quoting
someone's comment never impersonates them.

## Interoperability contract and preflight

`coord:v1` markers require a JSON object with integer `v: 1`, a supported `kind`, a
nonempty string `agent`, and the fields required by that kind. Known field types are
validated before snapshot ingestion; booleans are not issue numbers. Duplicate JSON
keys, non-finite numbers, excessive nesting, unsupported versions, and malformed
records are ignored. A bad external comment cannot become a claim or crash the board.
Additive v1 fields remain permitted, and an empty attestation head still revokes an
earlier attestation. `tests/fixtures/coord-protocol.json` contains the
conformance examples; `marker.validation_error` provides value-free diagnostics.

For supported read commands, `--json` keeps stdout machine-readable on degraded and
error paths. An unavailable inbox has `status: "stale"`, `items: null`, and
`markedRead: false`; this is different from an empty inbox. Failures carry
`schema: 1`, `status: "error"`, and an `error` object with `code`, `kind`, and a redacted
`message`, plus the documented nonzero exit code. Human diagnostics remain on stderr.

`coord doctor` checks local configuration and reports the chosen identity, installed
CLIs, authentication principal, transport, and cached snapshot age. It starts no model,
mints no token, and makes no network request. `--online` adds a repository metadata GET
using coord's normal authentication and budget policy. It verifies read access only;
neither installed `gh` nor a successful read proves operational write permissions.

The core CLI targets Python 3.9+ on macOS/Linux. Native Windows is unsupported because
the process/file-lock implementation uses Unix APIs. Codex cloud is still **unprobed**;
passing local contract tests does not establish cloud authentication or network access.
The sidechain reviewer's run-tag process cleanup is macOS-only: it reads process
environments with BSD `ps -E`. Its live process integration test is explicitly skipped
on other platforms; that skip does not establish Linux reviewer cleanup support.
To validate another environment, run `whoami --json`, offline/online `doctor`, and
`board --json`/`inbox --peek --json`; verify two concurrent tasks receive distinct ids.
Test claim/send/release and train submission only in a designated test repository or
issue with authorization. Do not distribute the local app's private key to cloud workers.

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
issue; the review between opening a PR and `train add` needs an owner too. In the
2026-09-27 load test, fix agents opened PRs and exited, Codex left findings on 9 of the
first ~17, and coord routed them to inboxes nobody read. The owner:

- **answers each review round**: fixes the whole batch, replies to each finding with the
  mechanism and a witness, and pushes once per round (then asks for the next review);
- **queues it** with `train add` once the head has Codex's 👍 or a recorded clean review
  and every finding is answered, and **re-queues it after a drop**: when the train
  ejects or removes it, fix what the train's comment names (the failing check; merge
  main only for a conflict), push, and `train add` again (pass `--head` with the sha you pushed, or let it compare with `git ls-remote`; see Merge train). A removal no push fixes (a
  fork, a `.github/workflows/` change while the train has no app token, a base other
  than `main`) goes to Alex instead;
- **hands off explicitly** if it has to stop: `release` the claim and
  `send --to <next owner>` (or the lead who dispatched it) naming the PR, its head and
  what is open.

An agent that cannot stay alive until the merge (a one-shot subagent, a cloud session
with a fixed budget) says so when it opens the PR, and whoever dispatched it keeps
ownership. It queues with `coord train add <pr> --owner-fallback <lead>`, naming the
dispatching lead's `COORD_AGENT`: the train-add record carries it, and the train's held,
ejected and removed notices go to the worker, the lead and the conductor (#1148). "After `train add`, stop" (see Merge train) means stop polling, not stop
owning: the train's comments and anyone who needs the owner reach it through `inbox`.

**Review-round limit.** A PR gets at most six bot review rounds (codex-drain's
`reviewCeiling`, counted from the PR's reviews, so a new session does not reset it). A PR
that reaches it goes to Alex, with a note of what is still open, instead of another
round: six rounds of findings usually mean the change needs a design look, not a seventh
patch.

**Taking over a stalled PR** (its owner has stopped, or left findings unanswered):

1. Make sure the owning session has stopped, so two agents are not pushing to one
   branch. `send --to <owner>` is only a pull-delivered message, so wait for its reply
   or its `release`; go ahead without one only if you stopped the session yourself, or
   its dispatcher confirms it has ended.
2. Fetch its branch into a worktree under a local branch of your own (the owner's
   worktree may still hold a local branch of the same name):
   `git fetch origin <branch>` and
   `git worktree add -b takeover/<branch> ../<dir> origin/<branch>`. Push with
   `git push origin HEAD:<branch>`.
3. Re-claim the issue: `coord claim <issue> --force --branch <branch> --files … --note
   "taking over #<pr> from <owner>: <why>"`.
4. Work the open round as the owner would: one commit, one push, on top of the existing
   head. Never rebase, amend or force-push another agent's branch.

From then on you own the PR.

## Messages

`send --on N` posts on issue or PR N and reaches whoever works on it. `send --to X`
without `--on` posts on the mailbox issue (label `coord:mailbox`, created on first
use). Addresses: an exact agent id, a prefix ending in `*` (`codex@*`,
`claude@Eidolon:*`), or `all`.

`inbox` shows, since your last read: messages addressed to you (any coord record with a
`to` list, which includes the merge train's held notices), and every comment
(including Codex review comments) on the issues you claim and on the PR of your current
branch. Delivery is pull-based: you see a message when you run `inbox` or a session
starts, so expect a delay of minutes, not seconds.

## Session start

`.claude/hooks/coord-board.sh` runs at SessionStart in every wired root (INISA,
CoreKit, AppAlpha, AppGamma) and puts `coord board --brief` into the session's
context. It stays silent if `python3`, `gh`, authentication or the network is
missing. Codex has no hook; `AGENTS.md` asks it to run `coord board` first.

## Merge train

GitHub's built-in merge queue is not available for a private repository owned by a
personal account, so the train is built from Actions parts
(`coordlib/train.py`, `train_step.py`, `.github/workflows/merge-train.yml`):

1. **Queue.** `coord train add <pr>` labels the PR `train` and records the head it was
   added at. Adding asserts the PR is not a carve-out: sound-character changes,
   persistence changes without a migration and public API breaks carry `needs-alex`
   until Alex adds `alex-approved`.

   A push reaches the branch before GitHub's PR head catches up, so `train add` run
   seconds after a push used to record the pre-push head, print "ready now", and then be
   dropped for a head move (#1734). It now compares the head GitHub reports for the PR
   with the branch's head on `origin` (`git ls-remote`, which spends no API budget);
   while they differ it re-reads GitHub's head, at most twice and five seconds apart,
   and if they still differ it adds nothing and prints `head still propagating: GitHub
   <sha>, remote <sha>; re-run train add`. Run from a checkout whose `origin` is not
   this repository (or that has no such branch), it skips the comparison and says so.
   `train add <pr> --head <full-sha>` names the head you pushed instead: it records
   only once GitHub shows that head (same bounded re-read) and otherwise refuses with
   both shas. When GitHub caught up only on a re-read, the board's review state is the
   earlier head's, so `train add` does not say "ready now"; the train judges readiness
   on its next step.

   **After `train add`, the worker is done: stop.** Do not poll `train status` or the
   PR for the merge; that polling is what ran the shared API budget dry twice (see The
   API budget). `train add` prints whether the PR is ready or held and why. The train
   comments on the PR when it tests, merges, splits, ejects or removes it, and when it
   holds it (see Readiness); `Closes #N` releases the claim when it merges; and anyone
   who needs the worker messages it.
2. **Readiness**, checked when a batch is built and again, from freshly read PR state,
   just before main moves: Codex 👍 on the current head AND Codex's pinned summary
   comment naming that head as reviewed and completed, or a recorded clean review of
   that head (below), no unanswered findings, CI not red at the head, not a draft, and
   no GitHub list read only in part. A failed check
   whose first annotation begins "The job was not started because" (a job the Actions
   budget refused, #1026) counts as not run: neither red nor green, shown on the board
   as "N not run (billing)", unless an earlier attempt of that check at the head failed
   for real, or could not be read. A `needs-alex`
   PR also needs Alex's approval of its current head (`coord train approve`); the
   `alex-approved` label alone does not count, because labels survive pushes. A PR that
   changes what governs the unattended conductor (`train.ALEX_ONLY_PATHS`: `Tools/coord/`,
   `CLAUDE.md`, `.claude/agents/`, `.claude/settings.json`, `.claude/hooks/`, and
   `Tools/ci-runner/`, the laptop runner's on/off tool, which runs with Alex's token) is held the
   same way, label or not, and so is a PR that changes `.github/workflows/` when the step
   holds the inisa-sidechain app token (see Limits; without it that PR is taken out for a
   hand merge). Instead of Codex's 👍, a clean review of the current head by
   any agent counts (Alex, 2026-09-26), recorded with
   `coord train review <pr> --head <full-sha> --by <agent> --note "…"`. `--head` is the
   full 40-character SHA of the head that was reviewed, never a prefix (a prefix can be
   matched by a crafted commit); the command refuses, recording nothing, unless it equals
   the PR's head, and `coord train approve` takes and checks `--head` the same way.
   Record one only when a real review of that exact head was done by an agent other than
   the one that wrote the change, and its verdict is clean: no open in-scope findings,
   every finding fixed, or answered and moved to an issue by Alex's decision. The record
   names the head, so a new push needs a new review; the `reviewed` label it adds is
   informational. It counts while Codex is still re-reviewing that head (👀 on the PR)
   too (Alex, 2026-09-29, #1181): the train does not wait for Codex's verdict on a head
   that already has a clean review. That releases only the wait. Codex findings on the
   head still hold the PR (`unanswered-findings`), clean review or not, and findings that
   land while it is in a batch take it out at the final check before main moves: the
   batch is rebuilt without it, and the agent that added it (with its `--owner-fallback`)
   and the conductor get the held notice. That is the train's existing rule for findings
   on a queued head. A finding posted after the merge lands on the merged PR, where
   GitHub notifies Alex; no agent's `coord inbox` shows it, because inbox reaches an agent
   through its standing claims and open PRs, and a merged PR is neither. That was already
   so for a head merged on a clean review before Codex answered; this widens it to heads
   Codex was still reviewing (follow-up: #1182). `substitute-review` is the command's old
   name and still works, and the `review-substitute` records it wrote still count for the head they name. The
   `codex-unavailable` label is retired: the train no longer reads it and coord no longer
   applies it. A clean-review or approval record counts only when GitHub lists Alex's
   login (the repository owner, `snapshot.trusted_attesters`) as its author, never a bot
   or another account: anyone who can comment can write the marker, the Codex connector
   and coord's own app included. That is why attestations never run on the app identity.
   A PR that is not ready waits (`coord train status` says why). A PR whose head moved,
   that git confirms conflicts with main, or comes from a fork is taken out with a comment, and so is
   one that changes `.github/workflows/` when the step has no app token.
   GitHub's asynchronously computed `CONFLICTING` value alone only holds it: read-only
   views report `merge-check`, and the step fetches the exact PR head and verifies its
   merge against pinned main. A clean local merge proceeds through the other gates;
   an unavailable check or a head that moves during the fetch leaves the PR queued.

   > **Delegated re-approval (#1102, confirmed by Alex 2026-09-28).** Once Alex has
   > approved a PR's direction, the lead session may re-record `coord train approve` on a
   > later head when the change since his approval is a review fix, a merge from main, or a
   > scope reduction, with a `--note` naming his original approval (the head and where he
   > gave it). Nothing else is delegated: new scope, a changed design, or a head that is not
   > the lead's to vouch for waits for Alex. `CLAUDE.md` and `AGENTS.md` state the same rule.

   **Held notices (#1034).** The first time the train holds a queued PR for a given
   (head, reason class), it comments once on the PR, stating the reason and what
   clears it, addressed to the agent that added it (from its `train-add` record) and to
   `conductor@*`; both see it in `coord inbox`. The classes are fixed (`train.HOLD_CLASSES`:
   `draft`, `partial-data`, `needs-alex`, `no-review`, `unanswered-findings`, `ci-red`,
   `head-moved`, `conflict`, `merge-check`), so a reason whose wording changes (another failing check)
   is not a new notice; a new head or a different class is. The train reads its own
   earlier `held` comments on the PR to know what it has said, so a new runner never
   repeats one. On the Actions train, only a comment `github-actions[bot]` posted whose
   marker names `train@actions` counts: every agent posts as Alex or as the app and can
   write any marker, so a `held` (or `merged`) marker anyone else writes never silences it. A
   `coord train step` run by hand also counts its own earlier comments (by its agent id),
   so repeated hand runs say nothing twice; the Actions train may repeat, once, what a
   hand run said first. A hold only
   Alex can clear (`needs-alex`, and `partial-data`, which the train never merges) also
   @-mentions `@alex-bridges`, so it reaches his GitHub notifications when no agent is
   running. A notice is best effort: a failed read or post is retried by the next step
   and never changes what the step decides.
3. **Batch.** Up to three ready PRs, in the order they were added, are merged onto main
   as `--no-ff` merge commits on `train/<stamp>` (computed with `git merge-tree`, no
   checkout). One `ci.yml` run gates it, dispatched with `lanes=changed`: what the
   batch's diff from main selects, so a docs-only batch skips the gates. Only a run
   the train dispatched itself counts (`workflow_dispatch` whose initial actor is the
   Actions bot); someone's narrower dispatch on the same commit never moves main, and
   `coord ci` refuses `train/` branches.

   **Light before heavy, bounded** (Alex, 2026-09-25): main's own `select-ci-lanes.py`
   makes each ready PR light or heavy (heavy when its diff selects any lane but `docs`
   and `screenshots`, which includes every CI-infrastructure change and any doubt), and
   a batch never mixes the two. When both are ready, one light batch may go ahead of the
   oldest ready heavy PR and then heavy goes next; with one class ready, the order PRs
   were added decides, as before. The overtake is recorded as a train comment on the
   heavy PR (only the train's own counts, as with held notices), so it holds across
   restarts, in dry-run, and when the light batch never reaches main.
4. **Green:** main is fast-forwarded to exactly the tested commit through the API (it
   fails if main moved, and the batch is rebuilt). GitHub then shows each PR as merged,
   but it does not fire `Closes #N` for a ref update (13 of 15 such issues were still
   open on 2026-09-26), so the step closes each still-open issue a merged PR names itself,
   then posts a receipt. It is best effort: an issue it cannot close is noted and left
   for a person (never a reason to hold the batch), and it never closes a pull request or
   an issue in another repository. **Red:** the step first reads the failed and cancelled
   jobs and their annotations and judges why (`train.classify`), by what GitHub recorded,
   never by how long a job ran (an 8-second exit 1 is real). The verdict and wake jobs are
   set aside. A person's cancel ("canceled by @…") abandons the batch for a rebuild. A
   timeout ("exceeded the maximum execution time"; such a job ends `cancelled`, not
   `timed_out`, though a `timed_out` job counts too) is re-run once, then the batch is
   abandoned with a `timed-out` receipt.
   CI infrastructure (a job with runner id 0 or no steps, one that failed in "Set up job",
   or an annotation saying the job was not started, the runner received a shutdown signal
   or lost communication) is re-run up to two more times than a real failure, then the
   batch is abandoned with an `infra` receipt. Neither is ever split or ejected; both
   receipts go to the conductor. A run counts as infrastructure only when every judged
   job does: any real failure in the mix makes it real, whatever the run's conclusion (a
   docs timeout beside it ends the run `cancelled`). A real failure is re-run once
   (flakes like #626); then a batch is split (each PR labelled `train:solo` and tested
   alone) and a lone PR is taken out with a comment naming the failed jobs. Every re-run
   reserves an attempt from the daily budget.

Each workflow run is one step and never waits on a gate. It is woken by `coord train
add`, by ci.yml's `wake-train` job at the end of each batch's CI (GitHub's `workflow_run`
does not fire for runs the Actions token dispatched), by the step that closes a batch
(so the next batch starts at once), by a 15-minute cron as a backstop (cron has run
hours late in this repository, and fired 5 of 96 steps on 2026-10-07), and by the
conductor when the train is asleep (see The conductor). The wake job checks out no code
and cannot change the run's result. A step may wait up to two minutes for a batch run
whose jobs have all finished to be marked completed.

**Switches (repository variables, Alex's to set):**

| Variable | Values | Effect |
|---|---|---|
| `COORD_TRAIN_MODE` | `dry-run` (unset) or `live` | dry-run tests batches and reports "would merge"; live moves main |
| `COORD_TRAIN_DAILY_LIGHT_ATTEMPTS` | a nonnegative number (unset = 12) | light CI attempts admitted in 24 hours, including retries |
| `COORD_TRAIN_DAILY_HEAVY_ATTEMPTS` | a nonnegative number (unset = 12) | heavy CI attempts admitted in 24 hours, including retries |
| `COORD_TRAIN_DAILY_RUNS` | an optional nonnegative number | an additional combined attempt cap, preserving an explicitly configured operator limit |
| `CI_PULL_REQUEST_LANES` | `full` (unset) or `light` | light keeps heavy lanes off PR pushes; gates then run on train batches, main, and `coord ci` requests. A PR that changes `.github/workflows/` keeps its heavy lanes: without the app token the train cannot merge it, so that run may be its only gate before main |
| `COORD_APP_CLIENT_ID` | the inisa-sidechain app's client id (`Iv23exampleClientId00`) | with the secret `COORD_APP_PRIVATE_KEY` (the app's `.pem`), merge-train.yml mints an installation token (contents and workflows write) that pushes, fast-forwards and deletes the refs of batches changing `.github/workflows/`; such PRs are then held for Alex's approval of the head instead of being taken out. Either one unset, or a failed mint: workflow PRs are taken out for a hand merge, and the step log says so |

Alex decided on 2026-09-24 to take heavy lanes off PR pushes. The order is: merge this
PR by hand (it changes workflows), watch a dry-run batch go green, set
`COORD_TRAIN_MODE=live`, then `CI_PULL_REQUEST_LANES=light`. Never the second before
the first: without a live train nothing heavy would run before a merge.

**Limits:**
- The Actions token cannot push workflow changes. A batch that changes
  `.github/workflows/` moves its refs (the `train/<stamp>` push, main's fast-forward, the
  branch deletion) with the inisa-sidechain app's installation token instead, minted by
  merge-train.yml for that one step (`COORD_TRAIN_APP_TOKEN`); comments, labels and the
  `ci.yml` dispatch stay on the Actions token. Such a PR is held `needs-alex` until Alex
  approves its head. With no app token, PRs touching `.github/workflows/` are taken out
  of the train and merged by hand, as before.
- Main moves through the API with the Actions token, which starts no push-triggered
  `ci.yml` run. The batch run already tested that exact commit; the next ordinary push
  to main diffs from the last green push run, which selects more lanes, never fewer.
  A workflow batch's main move is made by the app, so it does start the follow-on runs a
  hand merge starts (a push `ci.yml` run on main, `cancel-merged-branch-runs` per PR):
  the same as merging such a PR by hand did. Its `train/<stamp>` push starts nothing
  (no workflow runs on pushes outside main).
- All `train/*` branches share one build cache on Tycho
  (`.github/scripts/attach-branch-caches.sh`), so consecutive batches build
  incrementally instead of each cloning main's cache.

### Receipts and resource accounting

Each new batch carries an immutable `Train-Plan` commit trailer: base, member PR/head
pairs, queue times, batch capacity, and the lanes selected from main. Terminal train
comments also carry a machine-readable receipt with the exact tested tip, run id,
attempt and outcome. The receipt remains after the batch branch is deleted. It is
observability data; the existing fresh readiness and exact-commit merge checks remain
the authority to land code.

Before every dispatch or failed-job retry, the train records a `ci-requested`
reservation. The rolling attempt budget reconciles those reservations with Actions
runs, including old runs retried in the current window and requests whose response was
lost. Uncertainty costs an attempt; unknown historical classifications charge the
heavy pool. An unreadable or truncated ledger never permits more CI. Cheap light
batches have a separate pool, and retries consume capacity.

`coord train report --days N` is an explicit one-shot report, not a monitor. It reports
queue-to-batch waiting samples, batch occupancy, attempts/retries, split/ejection/merge
receipts, hold notices, actual job minutes by runner and batch class, and API calls used
to build the report. Job intervals are summed across attempts, including concurrent
jobs; pipeline elapsed time is not runner time. Timings are limited to the newest 100
runs to bound API use. Missing old plans, receipts, or timings are exposed as coverage
gaps. Hold counts do not establish hold duration, and the report does not invent a
counterfactual number of CI minutes saved. Collect these measurements before tuning
batch size or introducing parallel trains.

## The conductor

### Runtime boundary and read-only diagnosis

`coordlib/runtime.py` owns CLI invocation and result normalization. The existing
conductor uses its Claude repair adapter with the same standing orders and deny rules.
Neither installing this change nor running `doctor` switches or launches that worker.

`coord conductor diagnose` is a separate bounded diagnostic operation. It defaults to
Codex, accepts `--runtime claude`, and uses the CLI's default model unless `--model` is
specified. It reads the board or a focused `--input` file and runs in a fresh temporary
directory. Codex runs ephemeral with ignored user configuration/rules, a read-only
sandbox, approvals disabled, and tool providers explicitly disabled, including
connectors, plugins, hooks, shell, browser, delegation, and web search. The filesystem
sandbox alone would not prevent connector writes. Its global `AGENTS.md` can still
load; this is not an instruction or confidentiality isolation boundary.
Claude diagnosis disables tools and external customizations. GitHub tokens are removed
from the child environment; model authentication stays with the chosen CLI. These
controls are for diagnosis only; they do not isolate the repair adapter, and no repair
isolation work is planned. Alex's 2026-10-16 issue review kept the conductor guard as a
deliberately accepted tripwire and closed #1051 as not now, so widening what the
conductor may do needs its own bounded design and Alex's approval.

The escaped prompt is limited to 120,000 bytes so it fits portable command argument
limits; larger reports need a focused `--input` file. The result must be a completed
runtime response containing exactly `summary`,
`needsRepair` (boolean), and `reason`. Truncated, failed, or malformed output cannot
trigger a handoff. The default timeout is 300 seconds (maximum 3600); timeout kills
the process group. JSON output includes usage and duration. A diagnosis does not clear
pending conductor work, record a review, approve a head, change the train, or write to
GitHub. Only the explicit `--handoff` flag sends an advisory repair message to
`conductor@*`, and only when `needsRepair` is true. The existing conductor rechecks the
evidence under its own orders. This provides a working Codex role without pretending
Claude's guard hooks also protect a Codex repair process.

The adapters were exercised with Codex CLI 0.154.0 and Claude Code 2.1.283. Before a
Codex upgrade, run the following command. This optional probe
uses a temporary empty home and a local fake model endpoint, without authentication or
model spend, and verifies that the actual request advertises zero tools.

```bash
COORD_TEST_CODEX_RUNTIME=1 python3 -m unittest discover -s tests -p test_runtime_boundary.py
```

One agent answers the others, keeps the merge train moving and repairs CI. Agents reach it
with `send --to 'conductor@*'`; the train's held, ejected and removed notices go to it too,
and so do its `timed-out` and `infra` receipts (a batch abandoned for a timeout or for CI
infrastructure).
It is built to cost nothing while nothing happens (`coordlib/conductor.py`):

- **A watcher, not a session.** launchd runs `coord conductor tick` every 5 minutes
  (`conductor/install.sh`). The tick is plain Python: it checks this machine
  and, if it is the leader, reads the conductor's inbox, and every 15 minutes the train
  queue and `train/*` branches, the newest ci.yml run on main, the merge-train workflow,
  the runners, the runner routing variables, and the newest 100 Actions runs with the
  jobs of those it needs (unfinished runs other than pull-request CI while a control label
  is set, and runs running or failed within the hour). About 4 API calls a tick and 20 on
  the slow ones, plus one per run in flight, roughly 100 to 150 an hour. Most ticks end there.
- **A fresh run per wake.** When something needs judgement, the tick starts one
  `claude -p --agent conductor` run with the reasons in its prompt. The standing orders
  are `.claude/agents/conductor.md`. A fresh run has the same prefix every time (about
  19,500 tokens: system prompt, tools, the agent file, CLAUDE.md), which the prompt cache
  holds for an hour, so an idle day costs nothing and a wake after a quiet night pays for
  that prefix once, never for a transcript. The run's memory is GitHub plus
  `~/.local/state/inisa-conductor/notes.md`; each wake is logged with its tokens in
  `wakes.jsonl` there.
- **Cheap model first.** Messages and train holds wake the triage model
  (`CONDUCTOR_TRIAGE_MODEL`, default `sonnet`); red CI, a failed train workflow, an offline
  runner, queued control jobs or a low disk wake the repair model (`CONDUCTOR_REPAIR_MODEL`,
  default `opus`). A
  triage run that finds repair work ends with `ESCALATE:` and is rerun on the repair model.
- **What wakes it.** Messages addressed to it and comments on the work it claims; train
  holds for CI red, a conflict, an unavailable merge check or partial data, ejections and removals,
  and `timed-out` and `infra` receipts; a PR held in
  the train for 12 hours; the newest main or merge-train run red; a runner offline for 10
  minutes, unless it is parked (it carries none of the labels the routing variables
  `MK4_RUNNER_MACOS`, `MK4_RUNNER_MACOS_LIGHT`, `MK4_RUNNER_CONTROL` and
  `MK4_RUNNER_LINUX_ARM64` name: the laptop by day); a routing variable whose label no
  online runner has carried for 10 minutes (`routing`: such jobs queue, or go to
  GitHub-hosted runners and spend the Actions budget, as `xcode-27` did on 2026-10-07); a
  job for the `MK4_RUNNER_CONTROL` label queued over 15 minutes (`control-queued`); this
  machine's data volume under 20 GB (`CONDUCTOR_DISK_LOW_GB`). Broadcasts to
  `all` and the other holds (waiting for Codex, findings, a draft) ride along at the next
  wake instead of causing one.
- **What it does without a wake.** The train is asleep when a queued PR is ready or a
  `train/*` branch exists, no batch's CI is running, no merge-train run is waiting or
  running, and none has started for 20 minutes: the tick dispatches `merge-train.yml`
  itself (not in a dry run). The first `timed-out` or `infra` receipt goes to Alex as an
  escalation, then at most once a day per kind. A runner that failed three or more
  distinct jobs (a job is a run and a job name: re-runs within a run count once, the same
  job failing in different runs or batches counts each time) in under 90 seconds each
  within the hour, while another online runner
  carries the labels those jobs asked for, is escalated as possibly poisoned, once per
  episode; nothing is changed (the jobs may have failed for real, and a fast real failure
  is common in the light lanes). The routing checks read `GET actions/variables` with the
  tick's token, which is Alex's (`conductor tick` runs as Alex, as the runners read
  needs): a classic token's `repo` scope covers it, a fine-grained one needs "Variables:
  read". When the variables cannot be read the tick notes it once, skips the routing
  check and counts every runner, parked or not, as before. Any other failed read for
  these checks (the recent jobs, the `train/*` branches) skips only that check, with a
  line in the tick log, until the next slow tick; the rest of the slow tick still counts.
- **Bounded.** One run at a time; at most `CONDUCTOR_MAX_WAKES_PER_DAY` (24) wakes a day;
  a run is killed after an hour; a reason handed to three failed runs is escalated to
  Alex instead of retried. A condition that persists (a low disk) comes back a day later.
- **Model-free standby.** `conductor/install.sh --no-wake` installs the
  watcher with `tick --no-wake`; Claude need not be installed. It still participates
  in the leader lease, maintains heartbeats, wakes an asleep merge train and posts
  operational escalations. Refreshing an installation preserves this mode and its
  existing `CONDUCTOR_*` settings; `--wake` explicitly restores model wakes. This
  does not change lease election: a model-free leader keeps leading. It alerts Alex
  about pending repair work and requested reviews that need a model, even outside
  an outage, at most once per reason per day. A standby also hands off its own
  low-disk condition; GitHub work remains the leader's. Owner-authored handoff
  receipts carry notification deduplication across hosts. An operator pause
  suppresses these handoffs. A human or an active Codex session performs the repairs
  and reviews; the handoff leaves attempts and the inbox cursor intact for normal
  wake recovery.
  Finished review posts and known-error replies still proceed without a model.
- **Guardrails beyond the prompt.** The prompt fences quoted comments as data. Each run
  gets a `permissions.deny` list (`conductor.DENY`), and `.claude/hooks/conductor-guard.py`
  blocks, anywhere in a command rather than only as a prefix: moving main or refs by hand,
  `gh pr merge` or a merge through the API, `coord train approve`, repository variables and
  secrets, `launchctl` and `sudo`, and writes to its own hooks, settings and orders. For the
  GitHub App it adds rules that read the raw command, quoted strings included: `coord token
  exec` (it hands an app token to a shell) and `coord train approve`, with coord's options in
  any spelling argparse takes (`--repo X`, `--rep=X`, a bare `--`); any coord subcommand
  spelled with quotes or escapes (`coord 'token' exec`: the shell's quoting is not decoded,
  and no real command quotes its subcommand); reading the Keychain with `security` (the
  app's private key is there): a command word that is any prefix of a command that reads
  secrets, since `security` runs the first command it prefixes (`security f` is
  find-generic-password), a word with `$` or a backslash (`$'\x64ump-keychain'`),
  interactive mode (`-i`, `-p`), and the program name spelled with quotes or escapes
  (`"security"`, `secu\rity`: with every quote, backslash and `$` deleted, the command names
  `security` more often than as written); any command that names both `inisa-coord` and `cache`
  (case-folded, anywhere: the app's token cache is `~/Library/Caches/<state_dir_name>`, and a path has
  too many spellings to match; the repo's own name, as in `/Users/dev/Projects/INISA` or
  `--repo example-owner/example-repo`, is not enough, and a glob such as `inisa*` gets past it, an
  accepted cost recorded on #1051);
  the app's Keychain item (`example-coord-github-app`); and any command
  that names python and `apptoken` (on the NFKC-normalised, case-folded command; the test
  module's name `test_apptoken` aside). They do not tell prose from a command, so prose that
  names them is refused too (a commit message, a PR body, a `coord send`), and so is every
  shell way to write it to a file. The run writes such text
  with the Write tool (for example under `$CONDUCTOR_STATE_DIR`; the hook checks only the
  Write tool's path) and passes the file (`--body-file` / `-F`, or `"$(cat FILE)"` for
  `coord send`). A command over 20,000 characters is refused unread. The hook decides in a
  forked child and refuses when the verdict is not ready within 7 s (its own 10 s timeout
  would let the call through, and the push rule is cubic in repeated `git push`), and it
  refuses when it cannot fork that child at all. The rules are a denylist: a review sample
  of 40,000 commands the guard allows, run in bash and zsh, found 9,350 that really ran a
  program a rule is keyed on, through spellings the shell accepts and the rules do not
  match (#1046, round 11). The
  watcher will not wake a run while those files are modified in its checkout. A PR that
  changes what governs the conductor (`train.ALEX_ONLY_PATHS`: `.claude/agents/`,
  `.claude/settings.json`, `.claude/hooks/`, `Tools/coord/`, `CLAUDE.md`, `Tools/ci-runner/`) waits in the train
  for Alex's approval of its head, whoever queued it. These are tripwires, not a sandbox: a
  run bent on it could write a script that calls the API itself. No redesign is planned:
  Alex's 2026-10-16 issue review kept the denylist as a deliberately accepted tripwire and
  closed #1051 as not now, keeping its known false positives and missed spellings as
  recorded limits. Record a new bypass spelling or false positive as a comment on the
  closed #1051: it is a log, and no fix is owed. Reopen #1051 only if a live conductor run
  does something the guard exists to stop. Any expansion of the conductor's authority
  needs its own bounded design and Alex's approval. Nothing yet guarantees that main
  moves only through the train. The guarantee would be a **ruleset on main** that lets
  only the train's Actions token push. Since workflow batches move main with the
  inisa-sidechain app's installation token (see Merge train, Limits), that rule must also
  let the inisa-sidechain app through (a bypass entry for the app), or no workflow batch
  can land. That is Alex's setting to make, and it is not made: on 2026-10-16 the rules on
  main (`gh api repos/example-owner/example-repo/rules/branches/main`, ruleset 10000000) forbid
  only deleting main and force-pushing to it, so any token that can push to main can still
  move it outside the train (on 2026-09-25 the same call returned `[]`).
- **Several CI boxes.** Run install.sh on each. Every watcher keeps one heartbeat
  comment on the conductor issue (label `coord:conductor`) and edits it each tick; the
  oldest one that beat in the last 15 minutes is the leader and does the GitHub duties.
  The others check their own machine (disk) and take over if the leader goes quiet,
  starting from the inbox cursor the last leader's heartbeat carried. A standby's own runs
  handle only its machine and leave the inbox alone. The board lists every host.
- **Reaching Alex.** `coord conductor escalate`, exhausted retries and the daily cap post on
  the conductor issue and show on the board and in every session's start-up brief for a
  day. They also dispatch the Notify Alex workflow (`.github/workflows/notify-alex.yml`,
  #1044), which @-mentions him as `github-actions[bot]`. Every agent posts as Alex, and
  GitHub does not notify you of your own comments, so only a mention by the bot reaches his
  phone, the same way the train's do. If the dispatch fails, the escalation stays on the
  board.
- **Talking to it yourself.** In the conductor's checkout (`~/conductor/INISA` on Tycho),
  `COORD_AGENT=conductor@Tycho:conductor claude --agent conductor` starts it interactively
  with the conductor's inbox and guard. Pause the watcher first (`coord conductor pause
  --for 2h`) so a second one is not woken beside you.

The conductor's checkout stays detached at origin/main: the watcher fast-forwards it every
15 minutes when it is clean, so a merged change to the watcher or the standing orders takes
effect on the next tick. The conductor works in its own worktrees, never in that checkout.

## Reviews on request (`@inisa-sidechain review`)

A comment on a pull request that says `@inisa-sidechain review` gets a review from an agent
on the CI box, posted by the inisa-sidechain GitHub App (`coordlib/reviewer.py`,
standing orders `.claude/agents/reviewer.md`). Add a model to pick it: `@inisa-sidechain
review fable` (or `sonnet`, `haiku`, `model=…`); the default is Opus.

- **Pick-up.** The app's webhook is off, so the conductor's watcher finds the tag in the
  comments it already reads each tick: within five minutes, one review a tick, on the
  leader, never in a tick with a conductor wake. The app reacts 👀 on the tag. Tags in
  quotes or code, tags on issues, and anything a bot wrote are ignored.
- **The run.** A fresh `claude -p --agent reviewer` reads and tests the PR's head in a
  throwaway worktree. It starts in the conductor's checkout at origin/main, so the
  settings, hooks and guard it loads are main's, not the PR's; every `.claude` entry
  (directory, file or symlink) is removed from the PR's tree first, and
  `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` is unset, because Claude Code would load agents, skills and
  commands from an added directory (the diff still shows them). The run has **no GitHub
  credential**: `gh` gets an empty config directory, git reads no system or global config
  (so no Keychain helper) and cannot prompt, and token variables are removed. The PR's
  description comes in the prompt, fenced as the author's text. A deny list and the
  conductor guard's reviewer rules name any attempt to write. Its Bash, and everything a
  PR's test starts from it, runs in Claude Code's OS sandbox (Seatbelt; it refuses to start
  without it, and nothing runs outside it): no network, no writes to the conductor's
  checkout, and no reads of the app config, the token cache or `~/Library/Keychains`.
  Checked on Tycho: GitHub is unreachable, and a login-Keychain item that `security` reads
  outside the sandbox is "not found" inside it. Bash is its only tool (the agent definition
  and the deny list withhold Read, Edit, the search tools, WebFetch and subagents), so every
  file it opens goes through the sandbox, which checks at open time. It ends with JSON findings.
- **The verdict is code's.** Pass means no P0, P1 or P2 finding; P3 notes do not block. A
  `changes` verdict holds that head in the train, Codex's 👍 or a later pass by another model
  notwithstanding; only a new push answers it.
- **Who posts.** The app, as `inisa-sidechain[bot]`, with a `review` marker naming the PR,
  head, model and verdict. If the app has no token the request waits; a review is never
  posted as Alex (#1035's no-fallback rule).
- **A pass is the head's clean review.** The train accepts a clean review of the head by
  any agent (#1054), recorded as `review-clean` on a trusted user's comment. So for a pass
  the watcher's code (never the model) records one on Alex's identity, as `coord train
  review` does, `by` the reviewer and naming its request and model; then the `reviewed`
  label; then the app posts the review. A pass does not count when an earlier review of
  that head by the app requested changes, or when the PR's base moved during the run (it
  says why). A blocking verdict whose head has a recorded clean review is followed by a
  newer `review-clean` naming no head, which withdraws it.
- **Bounds.** One review per (PR, head, model); `CONDUCTOR_MAX_REVIEWS_PER_DAY` (8) a day;
  `CONDUCTOR_REVIEW_TIMEOUT` (45 min) a run; three failed runs get a reply and stop (a
  finished review whose post failed is posted again, not re-run); none
  while Claude is out of usage or the conductor is paused (a limit hit mid-review posts
  the outage and keeps the request). Requests are kept in the leader's state from the tick
  that first sees them (within 48 hours of being posted); a host that takes the lead
  rescans the last 48 hours and takes up every request the app has not answered, so a
  request the old leader held is not lost (no need to tag again).
  `coord conductor status` lists recent reviews;
  `reviews.jsonl` in the state directory logs each run's tokens.
- **What the bot author proves.** Any process on the Mac running as Alex can mint the app's
  tokens, so `inisa-sidechain[bot]` proves the review came through this Mac's app
  credentials, not that the reviewer wrote it. Cloud sessions and Codex cannot post as the
  app. A check run (Checks: write on the app, not granted today) would let the train
  verify a pass without trusting a marker.

## Outages

Every Claude agent draws on one subscription, so a usage limit stops all of them at once:
Claude Code on Eidolon and Tycho, every Claude cloud session, and the conductor. The weekly
limit resets **Thursday 12:00 America/New_York** (`COORD_CLAUDE_WEEKLY_RESET`, default
`Thu 12:00 America/New_York`: 16:00Z in summer, 17:00Z in winter). Codex has its own quota,
and the merge train needs no model; both keep running.

An outage is a record on the conductor issue (`coordlib/outage.py`). The
conductor's watcher posts one when a run is refused for a usage limit, with the reset the
message names, or the weekly reset for a weekly limit that names none, or an hour later
when it cannot tell. Anyone may post one with `coord outage start claude` (default: until
the weekly reset) or end it with `coord outage end claude`. It ends by itself when its time
passes; the conductor's first good run after that posts the `end`.

While it lasts:
- the board and every session's brief show `OUTAGE  claude agents are down until …`, so a
  Codex agent knows why a Claude agent is silent;
- `send` to an agent that is down says when it will read the message;
- claims held by those agents do not age toward stale;
- the conductor wakes nobody. What arrives meanwhile stays pending, because the watcher
  only peeks at the inbox and retains the messages it has read until they are
  consumed. Ordinary reads still cover at most 48 hours; a new leader reads from
  the adopted delivery cursor once to recover older pending mail. The first wake
  after the reset gets the retained mail, with a line saying Claude was out.
- the leader alerts Alex when newly pending waking reasons or requested reviews
  need a backup operator, including the reset time and evidence links. An unchanged
  reason is reported at most once per day within that outage, even when its reset
  deadline changes or leadership moves to another host. A later outage is a new
  episode and may alert immediately.
  A conductor wake that first hits the limit hands off in that same tick. The
  handoff consumes neither attempts nor the inbox cursor, so notifying someone
  does not pretend that the repair was completed.

During an outage, an active Codex session or a human manages the PR owners, answers
new review findings and submits reviewed heads to the train. The train still owns
the integration gates and the merge. A model-free watcher on a second host takes
over the Python duties when the old leader's heartbeat expires. Check `coord
conductor status` and the launchd log when a heartbeat becomes stale: a model
quota limit alone should not stop Python ticks. A watcher that is not running
cannot send its own outage handoff.

```bash
# Run in a trusted main checkout on the backup Mac.
coord outage start claude --note "weekly usage limit"
conductor/install.sh --no-wake
coord conductor tick --dry-run
coord inbox
```

Restore a model-enabled installation with `install.sh --wake` when appropriate;
an active outage continues to block Claude runs until it expires or is ended.
Codex's tool-free diagnosis adapter remains advisory. This standby procedure
does not depend on launching an unrestricted Codex repair daemon.

## Safety: what can go wrong, and how to stop it

Nothing the system does is irreparable. The worst it can do is put a bad commit on
main, and a revert undoes that.

| Part | What it can write | Worst case | Undo |
|---|---|---|---|
| `coord` commands | comments, labels, the mailbox issue, CI dispatches | comment noise; a CI flood from repeated `coord ci`; a runaway loop spending an hourly API quota | delete comments or labels; cancel runs; the quota refills within the hour |
| the GitHub App (`inisa-sidechain`) | in app mode, what the `coord` commands write. The read token writes nothing. The ops token can write any issue, pull request or label (comment, edit, close, reopen, request reviews). Through Actions write it can **dispatch any workflow that has `workflow_dispatch`, at any existing ref** (on main: `release.yml`, which uploads a build with the App Store Connect secrets and has no environment gate, `notify-alex.yml`, `ci.yml`, `merge-train.yml` and the CoreKit lanes), re-run and cancel runs, **delete workflow runs, their logs, artifacts and caches, and disable or enable workflows**. coord's own tokens cannot push code or change a workflow file. **The key can mint more:** since Alex granted the app "Workflows: Read and write" (2026-10-04) it can mint installation tokens with contents write and workflows write, as merge-train.yml does from the `COORD_APP_PRIVATE_KEY` secret, so whoever holds the key can push any branch, change any workflow file and move main (the main ruleset forbids only deleting main and force-pushing to it, 2026-10-16; see The conductor). Alex's own gh token, which every agent used before the app, already allows all of this and more. **Any process running as Alex can read the private key** with one `security find-generic-password -s example-coord-github-app -w` and no prompt, because `/usr/bin/security` created the Keychain item and is on its access list: the same class of exposure as `gh auth token`, but it lasts until the key is deleted, not an hour. Only in conductor runs, and as a tripwire: the conductor guard blocks that call and the token cache for Bash, and the run's deny list (`conductor.DENY`) keeps the Read tool out of the token cache with `Read(~/Library/Caches/<state_dir_name>/**)` and `Read(//Users/*/Library/Caches/inisa-coord/**)`. Claude Code documents `~/` as the home directory in Read rules; that it is expanded in the conductor's `-p --settings` run is not verified, which is why the `//` form is there too | a leaked ops token: all of that, for up to an hour; the key leaving the Keychain (or the Actions secret): all of that plus pushing code and workflow changes, until the key is deleted | `COORD_APP=off`; suspend or uninstall the installation; generate a new key and delete the old one, then replace the `COORD_APP_PRIVATE_KEY` secret (or delete it: the train falls back to dropping workflow PRs); to take back only the push power, remove the app's "Contents" and "Workflows" write permissions; review main and every branch for pushes by the app since the leak; a deleted run or log is not recoverable, and a dispatched TestFlight upload is on App Store Connect |
| session-start hook | nothing (read-only) | up to 20 s slower session start | remove its line from `settings.json` |
| merge train, dry-run | comments, labels, `train/*` branches, CI dispatches and re-runs | batch CI attempts on Tycho, bounded by separate light/heavy pools and any configured combined cap | disable the workflow |
| merge train, live | also: move main forward | a PR that was not ready lands on main | `git revert -m 1 <merge>` |
| conductor watcher | comments on the conductor issue; each run, whatever its orders allow (PRs, comments, re-runs) | a run acting on a misread message; usage spent on needless wakes (capped per day) | `coord conductor pause --for 1d`; `conductor/install.sh --uninstall` |
| `CI_PULL_REQUEST_LANES=light` | nothing (decides which lanes run) | PRs get less CI before merge | delete the variable |

What makes main safe:
- The one write to main is a REST ref update with `force: false`: it only succeeds as a
  fast-forward, so no commit can be lost or rewritten.
- The only branches it deletes are its own batches (branches whose tip carries its
  `Train-Batch` trailer); it never deletes issues, comments or anyone else's branch.
- Main receives exactly the commit the batch's CI passed, as ordinary merge commits.
- The Actions token cannot change `.github/workflows/` and expires with the job. The
  app token that can is minted only for the step's own run, with contents and workflows
  write only, is used only for the refs of a batch that changes workflow files after
  Alex approved each such PR's head, and is revoked when the job ends.
- Moving main ships nothing: no GitHub workflow on main uploads a build. Alex's policy
  is to keep Xcode Cloud workflows inactive and excluded from automatic PR, review,
  merge-train and main-branch pipelines. Legacy Cloud statuses are not merge signals;
  GitHub Actions and the documented local/native gates provide validation. Workflow
  activation is managed in App Store Connect. On 2026-10-16 all six workflows of the one
  Cloud product linked to this repository were made inactive, definitions and build
  history kept (record on #1450): three PR/branch test workflows, and three archive
  workflows with TestFlight internal distribution that started on tags (`v*`,
  `app1-v*`, `app2-v*`), never on a push to main. Manual TestFlight, local
  release/signing and tuning commands retain their existing authorization and guardrails.

What bounds a misbehaving train:
- One step at a time (a concurrency group), and each step does one thing.
- Separate bounded light/heavy CI attempt pools in 24 hours, with an optional combined
  `COORD_TRAIN_DAILY_RUNS` cap; failed-job retries count too.
- It never dispatches a second CI run on top of an existing one, and never builds a
  batch on a red main.
- A failure also red on main, a timeout, a person's cancel, or a CI infrastructure
  failure (a job never started, a runner that shut down or lost contact) never splits
  a batch or ejects PRs: those are re-run within the daily budget, then the batch is
  abandoned and rebuilt, and the conductor hears of it. A `startup_failure` on a batch that changes workflows removes only its
  workflow-changing PRs, with a notice to their owners and the conductor; ordinary
  PRs stay queued. This prevents a workflow that cannot start from blocking the
  queue through repeated rebuilds. A startup failure on a batch without workflow
  changes leaves every PR queued.

Off switches, instant and reversible:

```bash
gh workflow disable merge-train.yml        # stop the train
gh variable delete COORD_TRAIN_MODE        # back to dry-run: main cannot move
gh variable delete CI_PULL_REQUEST_LANES   # heavy CI back on every PR push
coord conductor pause --for 1d # no conductor wakes, on every box
```

## Why GitHub, and not a Project board or a separate service

- The old GitHub Project (`users/example-owner/projects/1`) is an auto-add list:
  256 items, 255 of them *Todo*. A stored board goes stale the moment an agent forgets
  to update it. If a browser or phone view is wanted, a workflow can copy the computed
  board into it later; agents never write to it.
- A dedicated service would give push delivery and real locks, but it needs its own
  credential in every cloud and Codex environment and would still have to mirror
  GitHub, where the PRs, reviews and CI already live.

## Code

`coordlib/`: `gh.py` (transport and identity), `config.py` (per-repo configuration), `apptoken.py` (app mode and
the app's tokens), `marker.py`, `identity.py`, `review.py` (Codex and CI rules),
`snapshot.py` (the board), `render.py`, `cache.py`, `budget.py` (the API budget
readings per principal, and the guard), `outage.py`, `lease.py` (the conductor's
heartbeats and pauses), `doctor.py` (preflight), `runtime.py` (model adapters),
`train.py` / `train_step.py` (batch planning and execution), `train_metrics.py`
(receipts and resource accounting), `conductor.py` (the watcher), `reviewer.py` (reviews on request), `cli.py`.
The conductor's standing orders are the file named by `conductor.agent_file`; its installer is
`conductor/install.sh`. Tests: `cd tests && python3 -m unittest discover -s . -p 'test_*.py'`. The app tests use a throwaway key,
a fake `security` and a fake GitHub; none reads the real Keychain item.


## Example: INISA configuration

INISA's deployment, for comparison. The settings the body above describes there are:
the CI workflow `ci.yml` (dispatched with `lanes=changed`), the train workflow `merge-train.yml`,
the `wake-train` job at the end of ci.yml, the Notify Alex workflow
(`.github/workflows/notify-alex.yml`), the owner-only paths (`Tools/coord/`, `CLAUDE.md`,
`.claude/agents/`, `.claude/settings.json`, `.claude/hooks/`, `Tools/ci-runner/`), main's own
`select-ci-lanes.py` as the lane classifier, the `MK4_RUNNER_*` repository variables, the conductor
and reviewer prompts `.claude/agents/conductor.md` and `.claude/agents/reviewer.md`, the reviewer
app `inisa-sidechain[bot]` triggered by `@inisa-sidechain review`, and the older state name
`inisa-coord` (a `legacy_state_dir_names` entry). The exact key values are in INISA's own
configuration. Everything else in the body above that names INISA is that setup:

- the GitHub App `inisa-sidechain`, the repository variable `COORD_APP_CLIENT_ID` and
  secret `COORD_APP_PRIVATE_KEY` that merge-train.yml uses for workflow-changing batches;
- the repository variables `COORD_TRAIN_MODE` (merge-train.yml reads it and passes it as `--mode`) and `CI_PULL_REQUEST_LANES`
  (INISA's ci.yml reads it; coord does not);
- the self-hosted runners, the "Tycho" CI box and its shared build cache script, the Eidolon
  laptop, the laptop runner's on/off tool, the Notify Alex workflow, and the SessionStart hook
  `.claude/hooks/coord-board.sh`;
- the 2026-09 rollout history: #989 merged the tool on 2026-09-24 with the train in dry-run;
  its first batch was #993; #994 added ci.yml's `wake-train` job; Alex set
  `COORD_TRAIN_MODE=live` and `CI_PULL_REQUEST_LANES=light` that day; the conductor replaced
  the four-hourly "shaman" task on 2026-09-25. Tracked in #991.

Those settings are not defaults; a new repository chooses its own.
