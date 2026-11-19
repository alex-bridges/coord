# coord

`coord` shows what the agents working in a GitHub repository have in flight and carries
messages between them. It reads GitHub and writes only marked comments; nothing is
locked. Pure Python 3.9+ standard library; needs `gh` (authenticated) or a GH_TOKEN.

## Standalone use

```sh
pip install .            # installs a `coord` command (or run ./coord from a clone)
cd your-checkout         # the repo is read from the origin remote; COORD_REPO overrides
coord board              # what is in flight
coord claim 12 --files src/foo.py
coord inbox
```

With no configuration, board, claim, send and inbox work. The merge train, reviewer and
conductor stay off until the repository configures them. A feature that needs a missing
setting stops with a message naming the key. If the repository cannot be detected
(no github.com origin, no COORD_REPO), coord says so instead of guessing.

## Documentation

- [docs/adopting.md](docs/adopting.md): step by step for a new repository (install, auth, config, labels, the merge-train workflow, the conductor).
- [docs/agent-coordination.md](docs/agent-coordination.md): the model, commands, claims, merge train, conductor, outages, API budget and identity.

## Configuration

Per-repo settings live in `.coord/config.json` at the root of the working tree
(`COORD_CONFIG` names another file). Keys and defaults are in `coordlib/config.py`
(`DEFAULTS`): default branch, owner login and mention, CI/train/notify workflow names,
the verdict and wake job names, owner-only paths, lane classifier, conductor and
reviewer prompt/hook files, runner variables, and the state naming below.

Per-user state lives under `~/.cache/<state_dir_name>`, `~/.config/<state_dir_name>`
and `~/Library/Caches/<state_dir_name>` (default `coord`); the GitHub App's key is
the Keychain item `keychain_service` (default `coord-github-app`).
`legacy_state_dir_names` (for example `["inisa-coord"]`) lists older names that are
still read when the new one has nothing, and never written.

## Conductor

`conductor/install.sh --repo owner/name [--home DIR] [--interval S]` installs a launchd
agent (`com.coord.conductor.<owner>-<name>`, log
`~/Library/Logs/coord-conductor-<name>.log`) that runs `coord conductor tick` from a
checkout kept at origin/main (default `~/conductor/<name>`). `--uninstall` removes it.

## History

Commit messages here cite `(#NNN)`: those are pull-request numbers of the INISA
repository, where coord was developed before it moved to its own repository.

## Running the tests

The tests use the standard library's `unittest`; pytest isn't required (and isn't
installed on the Macs). Run them from the repository root:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'   # everything, about 1.5-2.5 min
python3 -m unittest tests.test_budget                  # one file
python3 -m unittest tests.test_budget.RecordingTests   # one class (or .test_name)
```

`discover -s tests -t .` fails with `Start directory is not importable`: `tests` has no
`__init__.py`, so name it with `-s tests` and no `-t`.

In a Claude cloud session, `CLAUDE_CODE_REMOTE=true` switches coord to its REST-only
reads and many tests fail against fakes that expect GraphQL; run the suite with
`env -u CLAUDE_CODE_REMOTE python3 -m unittest …` there. Even then,
`test_reviewer`'s process-kill tests can error in Linux containers (which ones varies
by container); the Macs are the reference.
