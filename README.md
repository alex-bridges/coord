# coord

`coord` shows what the agents working in this repository have in flight and carries
messages between them. It reads GitHub and writes only marked comments; nothing is
locked. How to use it: [`Docs/agent-coordination.md`](../../Docs/agent-coordination.md).

## Running the tests

The tests use the standard library's `unittest`; pytest isn't required (and isn't
installed on the Macs). Run them from `Tools/coord`:

```sh
cd Tools/coord
python3 -m unittest discover -s tests -p 'test_*.py'   # everything, about 1.5-2.5 min
python3 -m unittest tests.test_budget                  # one file
python3 -m unittest tests.test_budget.RecordingTests   # one class (or .test_name)
```

`discover -s tests -t .` fails with `Start directory is not importable`: `tests` has no
`__init__.py`, so name it with `-s tests` and no `-t`.

In a Claude cloud session, `CLAUDE_CODE_REMOTE=true` switches coord to its REST-only
reads and many tests fail against fakes that expect GraphQL; run the suite with
`env -u CLAUDE_CODE_REMOTE python3 -m unittest …` there. One Linux-only
process test still errors in containers; the Macs are the reference.
