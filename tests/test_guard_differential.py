"""The conductor guard against a frozen copy of itself (tests/legacy_guard, from 4f7d4a4fd).

Round six of the #1046 review deleted a heredoc rule while narrowing the one-liner check,
and wrapped `python3 - <<EOF` heredocs (`timeout 30 python3 - <<EOF`, `( python3 - <<EOF`)
went from blocked to allowed with every test green: each test listed what it meant to
block, and nothing listed what used to be blocked. This test runs the current guard and
the old one over one corpus and fails when a command the old guard blocked is allowed now,
unless it is in INTENDED_UNBLOCKS below.

The corpus: every command the guard tests in test_conductor.py list (read from that file,
so new cases join by themselves; its lists have only grown since 4f7d4a4fd, so this is
both versions' cases), the reviewers' probe lists from rounds five to seven, and the
wrapped heredoc forms of round six. Do not update legacy_guard/ to match the hook: it is
the old guard on purpose.
Run: python3 -m unittest discover -s Tools/coord/tests
"""

from __future__ import annotations

import ast
import importlib.util
import itertools
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "..", "..", "..", ".claude", "hooks", "conductor-guard.py")
LEGACY_GUARD = os.path.join(HERE, "legacy_guard", "conductor-guard.py")


def load(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def conductor_test_commands() -> list:
    """The commands in every `for command in (…)` of test_conductor.py."""
    with open(os.path.join(HERE, "test_conductor.py"), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    commands = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == "command"
                and isinstance(node.iter, ast.Tuple)):
            commands += [item.value for item in node.iter.elts
                         if isinstance(item, ast.Constant) and isinstance(item.value, str)]
    return commands


# The reviewers' probes, verbatim: review5/guard_probe.py, review6/probe.py, review6/probe2.py.
PROBES = [
    # review5/guard_probe.py CASES
    "echo 'from coordlib import apptoken' | python3 -",
    "printf 'import coordlib.apptoken\\n' | python3 -",
    'cat <<EOF | python3 -\nfrom coordlib import apptoken\nEOF',
    "python3 - < <(echo 'import coordlib.apptoken')",
    "echo 'import coordlib.apptoken' | python3",
    'python3 <<EOF\nimport coordlib.apptoken\nEOF',
    'python3 -c \'print("it\'\\\'\'s"); import coordlib.apptoken\'',
    "python3 -c $'x\\'y; import coordlib.apptoken'",
    'python3 -c "print(\\"a\\"); import coordlib.apptoken"',
    "python3 -c 'import sys; import coordlib.apptoken",
    'python3 -c "import sys; import coordlib.apptoken',
    "python3 -c 'import sys\nimport coordlib.apptoken'",
    "python3 -c 'import sys' ; python3 -c 'import coordlib.apptoken'",
    "python3 -c 'print(1)' & python3 -c 'import coordlib.apptoken'",
    'python3 -c "print(1)"; git log -- Tools/coord/coordlib/apptoken.py',
    "python3 -c 'print(1)' && grep -n AppTokens Tools/coord/coordlib/apptoken.py",
    "python3 -c 'print(1)' | grep -c apptoken",
    "python3 - <<'EOF'\nprint(1)\nEOF\ngit log -- Tools/coord/coordlib/apptoken.py",
    'python3 -m unittest discover -s Tools/coord/tests',
    'Tools/coord/coord token status',
    "Tools/coord/coord board --brief | python3 -c 'import sys; print(sys.stdin.read())'",
    'git log -S example-coord-github-app',
    'Tools/coord/coord send --on 12 "the key (example-coord-github-app) is fine"',
    # review6/probe.py BYPASS
    "echo 'import coordlib.apptoken' | python3 -B -",
    "echo 'import coordlib.apptoken' | python3 -u",
    "echo 'import coordlib.apptoken' | python3 -u -",
    "echo 'import coordlib.apptoken' | env python3 -",
    "echo 'import coordlib.apptoken' | exec python3 -",
    "echo 'import coordlib.apptoken' | nice python3 -",
    "echo 'import coordlib.apptoken' | nice -n 5 python3 -",
    "echo 'import coordlib.apptoken' | nohup python3 -",
    "echo 'import coordlib.apptoken' | time python3 -",
    "echo 'import coordlib.apptoken' | command python3 -",
    "echo 'import coordlib.apptoken' | builtin exec python3 -",
    "echo 'import coordlib.apptoken' | caffeinate python3 -",
    "echo 'import coordlib.apptoken' | timeout 5 python3 -",
    "echo 'import coordlib.apptoken' | xargs -0 python3 -c",
    "echo 'import coordlib.apptoken' | /usr/bin/python3 -",
    "echo 'import coordlib.apptoken' | /opt/homebrew/bin/python3.14 -",
    "echo 'import coordlib.apptoken' | python3.14 -",
    "echo 'import coordlib.apptoken' | py -",
    "echo 'import coordlib.apptoken' | pypy3 -",
    "echo 'import coordlib.apptoken' | uv run python -",
    "echo 'import coordlib.apptoken' | /usr/bin/env python3 -",
    "echo 'import coordlib.apptoken' | env -i python3 -",
    "echo 'import coordlib.apptoken' | env PYTHONPATH=Tools/coord python3 -",
    "echo 'import coordlib.apptoken' | PYTHONPATH=Tools/coord python3 -",
    "echo 'import coordlib.apptoken' | PYTHONPATH='Tools/coord' python3 -",
    "echo 'import coordlib.apptoken' | python3 -X utf8 -",
    "echo 'import coordlib.apptoken' | python3 -W ignore -",
    "echo 'import coordlib.apptoken' | python3 -- -",
    "echo 'import coordlib.apptoken' | python3 /dev/stdin",
    "echo 'import coordlib.apptoken' | python3 -i",
    'echo \'import coordlib.apptoken\' | "python3" -',
    "echo 'import coordlib.apptoken' | 'python3' -",
    "echo 'import coordlib.apptoken' | p\\ython3 -",
    "echo 'import coordlib.apptoken' | (python3 -)",
    "echo 'import coordlib.apptoken' | { python3 -; }",
    "echo 'import coordlib.apptoken' |& python3 -",
    "echo 'import coordlib.apptoken' | python3 - 2>&1",
    "echo 'import coordlib.apptoken' | python3 - >out.txt",
    "echo 'import coordlib.apptoken' | python3 - arg1",
    "echo 'import coordlib.apptoken' > /tmp/x.py; python3 /tmp/x.py",
    "python3 <(echo 'import coordlib.apptoken')",
    "python3 < <(echo 'import coordlib.apptoken')",
    'python3 -c "$(echo \'import coordlib.apptoken\')"',
    'python3 -c "$(printf \'import coordlib.apptoken\')"',
    "python3 <<< 'import coordlib.apptoken'",
    "python3 - <<< 'import coordlib.apptoken'",
    "python3 -B <<< 'import coordlib.apptoken'",
    "python3 <<'EOF'\nimport coordlib.apptoken\nEOF",
    'python3 <<-EOF\n\timport coordlib.apptoken\n\tEOF',
    'python3 -u <<EOF\nimport coordlib.apptoken\nEOF',
    'env python3 - <<EOF\nimport coordlib.apptoken\nEOF',
    'true; python3 <<EOF\nimport coordlib.apptoken\nEOF',
    'cd Tools/coord && python3 <<EOF\nimport coordlib.apptoken\nEOF',
    'cd Tools/coord && python3 - <<EOF\nfrom coordlib import apptoken\nEOF',
    '(python3 -) <<EOF\nimport coordlib.apptoken\nEOF',
    'cat <<EOF | python3\nimport coordlib.apptoken\nEOF',
    'cat <<EOF | tr a a | python3 -\nimport coordlib.apptoken\nEOF',
    "python3 -c 'import coordlib.apptoken' &",
    "python3 -c 'import coordlib.apptoken' & echo",
    "python3 -Bc 'import coordlib.apptoken'",
    "python3 -Ic 'import coordlib.apptoken'",
    "python3 -c'import coordlib.apptoken'",
    'python3 -m coordlib.apptoken',
    'python3 -c \'import importlib; importlib.import_module("coordlib.app" + "token")\'',
    "echo 'import coordlib.apptoken' | $(which python3) -",
    "echo 'import coordlib.apptoken' | ${PY:-python3} -",
    'echo "import coordlib.apptoken" | python3',
    'echo import coordlib.apptoken | python3',
    "echo 'import coordlib.apptoken' | tee /dev/null | python3",
    "echo 'import coordlib.apptoken'|python3",
    "echo 'import coordlib.apptoken'| python3 -",
    "echo 'import coordlib.apptoken' | python -",
    "echo 'import coordlib.apptoken' | python2.7 -",
    "echo 'import coordlib.apptoken' | Python3 -",
    "echo 'import coordlib.apptoken' | python3t -",
    "echo 'import coordlib.apptoken' | python3.14t -",
    "echo 'import coordlib.apptoken' | ipython",
    "echo 'import coordlib.apptoken' | python3 -q",
    "echo 'import coordlib.apptoken' | python3 -O -",
    "echo 'import coordlib.apptoken' | python3 -OO -",
    "echo 'import coordlib.apptoken' | python3 -E -s -",
    "echo 'import coordlib.apptoken' | python3 --isolated -",
    "echo 'import coordlib.apptoken' | python3 -X dev",
    'x=\'import coordlib.apptoken\'; echo "$x" | python3 -',
    "echo 'import coordlib.apptoken' | python3 -c 'import sys; exec(sys.stdin.read())'",
    'echo \'import coordlib.apptoken\' | python3 -c "exec(input())"',
    # review6/probe.py ALLOW
    'grep -rn apptoken Tools/coord | grep python3',
    'echo apptoken; python3 -',
    'cd Tools/coord && python3 -m unittest test_apptoken',
    'git diff origin/main -- Tools/coord/coordlib/apptoken.py | head -50',
    'git log --oneline -- Tools/coord/coordlib/apptoken.py | head',
    "gh pr view 1046 --json files | python3 -c 'import json,sys; print(json.load(sys.stdin))'",
    'gh pr view 1046 --json body -q .body | grep -n apptoken',
    "Tools/coord/coord board --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d))'",
    'Tools/coord/coord send --on 1046 "apptoken fix looks right; python3 tests pass"',
    "Tools/coord/coord send --on 1046 'apptoken: python3 - reads stdin'",
    'Tools/coord/coord send --on 1046 "it\'s the apptoken module, run python3 -m unittest"',
    "gh pr comment 1046 --body 'The apptoken change: python3 now...'",
    'gh pr view 1046 --json body | python3 -c \'import json,sys; print(json.load(sys.stdin)["body"])\''
    ' | grep apptoken',
    "gh pr diff 1046 | grep -n apptoken | python3 -c 'import sys; print(sum(1 for _ in sys.stdin))'",
    'gh api repos/o/r/pulls/1046/files | python3 -c \'import json,sys; [print(f["filename"]) for f in'
    ' json.load(sys.stdin)]\' | grep apptoken',
    "gh pr list --json title | python3 -c 'import json,sys; print(json.load(sys.stdin))' | grep -i apptoken",
    'python3 -m json.tool < board.json | grep apptoken',
    'cat notes.txt | grep apptoken | python3 -m json.tool',
    'Tools/coord/coord inbox | grep -i apptoken',
    'Tools/coord/coord send --on 1046 "won\'t touch apptoken; python3 - is fine"',
    'echo "doesn\'t" apptoken python3',
    'git log --grep apptoken --oneline | head -5',
    'gh pr view 1046 --comments | grep -c apptoken',
    # review6/probe2.py BY
    "python3 - 2>&1 <<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 - arg <<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 -X utf8 - <<'EOF'\nimport coordlib.apptoken\nEOF",
    "/usr/bin/env python3 - <<'EOF'\nimport coordlib.apptoken\nEOF",
    "nice python3 - <<'EOF'\nimport coordlib.apptoken\nEOF",
    "cd Tools/coord && timeout 30 python3 - <<'EOF'\nfrom coordlib.apptoken import AppTokens\nEOF",
    "python3 /dev/stdin <<'EOF'\nimport coordlib.apptoken\nEOF",
    "echo 'from coordlib import apptoken' | python3 - 2>&1",
    "echo 'from coordlib import apptoken' | python3 - 2>/dev/null",
    "echo 'from coordlib import apptoken' | python3 - | head",
    # review6/probe2.py FP
    "git commit -q -F - <<'EOF'\ncoord: don't let apptoken run from python3 stdin\nEOF",
    "gh pr create --title 'coord: apptoken guard' --body-file - <<'EOF'\nThe guard's python3 check now covers"
    " stdin.\nEOF",
    "gh pr comment 1046 --body-file - <<'EOF'\nThe conductor's run can't reach apptoken from python.\nEOF",
    'git commit -m "$(cat <<\'EOF\'\ncoord: the guard\'s apptoken check for python3\nEOF\n)"',
    "Tools/coord/coord send --on 1046 'the apptoken test'\\''s python3 run is green'",
    'Tools/coord/coord conductor escalate "#1046: apptoken tests fail under python3.9; it\'s the \\"cached\\" line"',
    "python3 - <<'EOF'\nprint(1)\nEOF\ngit log --oneline -- Tools/coord/coordlib/apptoken.py",
    "gh pr diff 1046 | grep -n apptoken | python3 -c 'import sys; print(len(sys.stdin.readlines()))'",
    'gh pr view 1046 --json files | python3 -c \'import json,sys; print([f for f in json.load(sys.stdin)["files"]'
    ' if "apptoken" in f["path"]])\'',
]

# Round seven's list: stdin forms the round-six anchor missed, and prose it refused.
ROUND_SEVEN = [
    "echo 'import coordlib.apptoken' | python3 - arg",
    "echo 'import coordlib.apptoken' | python3 - >out",
    "echo 'import coordlib.apptoken' | python3 -W ignore -",
    "python3 -W ignore - <<'EOF'\nimport coordlib.apptoken\nEOF",
    "( python3 - <<'EOF'\nimport coordlib.apptoken\nEOF\n)",
    "{ python3 - <<'EOF'\nimport coordlib.apptoken\nEOF\n}",
    "cd Tools/coord && timeout 60 python3 - <<'EOF'\nimport coordlib.apptoken\nEOF",
    "git commit -q -F - <<'EOF'\ncoord: don't let apptoken run from python3 stdin\nEOF",
    "gh pr create --title x --body-file - <<'EOF'\nThe guard's apptoken check for python3…\nEOF",
    "coord send --on 1046 'the apptoken test'\\''s python3 run is green'",
]

# Round six's wrapped heredocs (review6/probe3.py): every wrapper, three pythons, two scripts.
WRAPPERS = ("", "env ", "exec ", "nohup ", "time ", "nice ", "nice -n 5 ", "timeout 30 ", "/usr/bin/env ", "env -i ",
            "command ", "caffeinate -i ", "(", "{ ", "cd Tools/coord && ", "cd Tools/coord && timeout 60 ",
            "PYTHONPATH=Tools/coord ", "env PYTHONPATH=Tools/coord ", "time -p ", "stdbuf -o0 ", "arch -arm64 ",
            "xcrun ", "/usr/bin/time ")
HEREDOCS = ("{} - <<'EOF'\nimport coordlib.apptoken\nEOF", "{} -B - <<'EOF'\nfrom coordlib.apptoken import AppTokens\nEOF")


def wrapped_heredocs() -> list:
    commands = []
    for wrapper, python, heredoc in itertools.product(WRAPPERS, ("python3", "/usr/bin/python3", "python3.14"), HEREDOCS):
        command = wrapper + heredoc.format(python)
        commands.append(command + {"(": "\n)", "{ ": "\n}"}.get(wrapper, ""))
    return commands


# Commands the old guard blocked that this one allows on purpose. Each needs a reason. None since
# round eight, which blocks python and apptoken anywhere in one command.
INTENDED_UNBLOCKS = {}


class GuardDifferentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = load(GUARD, "conductor_guard_now")
        cls.legacy = load(LEGACY_GUARD, "conductor_guard_4f7d4a4fd")
        cls.corpus = list(dict.fromkeys(conductor_test_commands() + PROBES + ROUND_SEVEN + wrapped_heredocs()))

    @staticmethod
    def blocked(guard, command: str) -> bool:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp/home"}
        return bool(guard.verdict(payload, {"CLAUDE_PROJECT_DIR": "/tmp/home"}))

    def test_the_corpus_has_every_source(self):
        self.assertGreater(len(conductor_test_commands()), 100, "test_conductor.py's command lists were not found")
        self.assertEqual(len(wrapped_heredocs()), 138)
        self.assertGreater(len(self.corpus), 400)

    def test_nothing_the_old_guard_blocked_is_allowed_now(self):
        regressions = [command for command in self.corpus
                       if command not in INTENDED_UNBLOCKS and self.blocked(self.legacy, command)
                       and not self.blocked(self.guard, command)]
        self.assertEqual(regressions, [], "blocked at 4f7d4a4fd, allowed now")

    def test_each_intended_unblock_was_blocked_and_is_allowed_now(self):
        for command in INTENDED_UNBLOCKS:
            self.assertIn(command, self.corpus)
            self.assertTrue(self.blocked(self.legacy, command), command)
            self.assertFalse(self.blocked(self.guard, command), command)


if __name__ == "__main__":
    unittest.main()
