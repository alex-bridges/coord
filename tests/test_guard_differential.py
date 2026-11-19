"""The conductor guard against a frozen copy of itself (tests/legacy_guard, from 4f7d4a4fd).

Round six of the #1046 review deleted a heredoc rule while narrowing the one-liner check,
and wrapped `python3 - <<EOF` heredocs (`timeout 30 python3 - <<EOF`, `( python3 - <<EOF`)
went from blocked to allowed with every test green: each test listed what it meant to
block, and nothing listed what used to be blocked. This test runs the current guard and
the old one over one corpus and fails when a command the old guard blocked is allowed now,
unless it is in INTENDED_UNBLOCKS below. Round seven's heredoc parser then failed open the
same way, and round eight made the apptoken rule co-occurrence on the raw command, so a
second test checks that every command naming python and apptoken is blocked.

Round twelve rebuilt the guard as main's (59e25cc6f) with lines inserted. The structural test
in test_conductor.py pins main's lines, but an inserted line could still allow what main
blocks, so a third test runs main's guard (tests/legacy_guard/conductor-guard-59e25cc6f.py)
over the same corpus and fails when a command main blocks is allowed now.

The corpus: every command the guard tests in test_conductor.py list (read from that file,
so new cases join by themselves), the reviewers' probes from rounds five to seven, the
wrapped heredocs of round six, and round seven's shapes and fuzz grammar. Each source has
a minimum size. Do not update legacy_guard/ to match the hook: it is the old guard on
purpose.
Run: python3 -m unittest discover -s Tools/coord/tests
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import itertools
import os
import re
import sys
import unicodedata
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "..", "..", "..", ".claude", "hooks", "conductor-guard.py")
LEGACY_GUARD = os.path.join(HERE, "legacy_guard", "conductor-guard.py")
MAIN_GUARD = os.path.join(HERE, "legacy_guard", "conductor-guard-59e25cc6f.py")
MAIN_BLOB = "d0441c4ecc072255a06b189566b7afbbef524f5b"  # `git hash-object` of main's guard at 59e25cc6f


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


# Round seven's review (review7/probe_*.py and fuzz_diff.py): `<<` that is not a heredoc
# (in quotes, a comment, `$((…))`), delimiters the parser misread, nested and CRLF heredocs,
# wrappers, stdin forms, prose, and the conductor's own commands.
STDIN_SCRIPT = "python3 - <<'Y'\nimport coordlib.apptoken\nY"
STDIN_SCRIPT_NO_DASH = "python3 <<'Y'\nimport coordlib.apptoken\nY"
NOT_OPENERS = [
    'echo "<<X"\n',
    "echo '<<X'\n",
    'true # <<X\n',
    'echo $((1<<2))\n',
    'echo $(( 1 << 2 ))\n',
    'echo \\<<X\n',
    "cat <<$'EOF'\nhi\nEOF\n",
    'cat <<E\\OF\nhi\nEOF\n',
    'cat <<E"O"F\nhi\nEOF\n',
    "cat <<'E'OF\nhi\nEOF\n",
    'x=$(cat <<EOF\nhi\nEOF)\n',
    'git commit -q -m "doc: <<EOF heredocs"\n',
    "grep -n '<<EOF' Tools/coord/coordlib/*.py | head\n",
    "gh pr comment 1046 --body 'use python3 - <<EOF'\n",
    'Tools/coord/coord send --on 1046 "the <<EOF form"\n',
]
AFTER_AN_OPENER = [
    'echo "<<X"\npython3 -c \'import sys\nimport coordlib.apptoken\'',
    "python3 -c 'import sys\nimport coordlib.apptoken'",
    'echo "<<X"\necho \'import coordlib.apptoken\' | python3 -',
    'echo "<<X"\npython3 -c \'import coordlib.apptoken\'',
    'echo "<<X"\necho "import coordlib.apptoken" |\npython3 -',
]


def hidden_by_an_opener() -> list:
    commands = []
    for opener in NOT_OPENERS:
        for script in (STDIN_SCRIPT, STDIN_SCRIPT_NO_DASH):
            commands += [opener + script, opener + "cd Tools/coord && timeout 30 " + script]
    return commands + ["bash <<'X'\n" + script + "\nX" for script in (STDIN_SCRIPT, STDIN_SCRIPT_NO_DASH)] + AFTER_AN_OPENER


MORE_WRAPPERS = [
    'sudo -u x ', 'nohup ', 'doas ', 'chrt -f 10 ', 'taskpolicy -b ', 'ionice -c3 ', 'unbuffer ', 'script -q /dev/null ',
    'watch ', 'parallel ', 'gtimeout 30 ', 'builtin exec ', 'exec -a foo ', 'env -S ', 'env -u X ', 'nice -5 ', 'time -p ',
    '/usr/bin/nice ', 'sandbox-exec -n no-network ', 'arch -x86_64 ', 'xcrun --sdk macosx ', 'uv run ', 'poetry run ',
    'pipenv run ', 'npx ', 'command -p ', 'noglob ', 'nocorrect ', '! ', 'if true; then ', 'while false; do :; done; ',
    'true && ', 'true || ', 'x=1 ', 'X="a b" ', 'time nice ', '[[ 1 ]] && ', 'coproc ', 'exec 3<&0; ',
]
OTHER_SHAPES = [
    "find . -maxdepth 0 -exec python3 - <<'EOF' \\;\nimport coordlib.apptoken\nEOF",
    "find . -maxdepth 0 -exec python3 - \\; <<'EOF'\nimport coordlib.apptoken\nEOF",
    "bash -lc 'python3 - <<EOF\nimport coordlib.apptoken\nEOF'",
    "bash -c 'python3 - <<EOF\nimport coordlib.apptoken\nEOF'",
    "sh -c 'python3 - <<EOF\nimport coordlib.apptoken\nEOF'",
    "zsh -c 'python3 - <<EOF\nimport coordlib.apptoken\nEOF'",
    'bash -lc "python3 - <<EOF\nimport coordlib.apptoken\nEOF"',
    "bash -c 'python3 <<EOF\nimport coordlib.apptoken\nEOF'",
    "bash <<'X'\npython3 - <<'EOF'\nimport coordlib.apptoken\nEOF\nX",
    "bash <<'X'\npython3 <<'EOF'\nimport coordlib.apptoken\nEOF\nX",
    'eval "$(printf \'python3 - <<EOF\\nimport coordlib.apptoken\\nEOF\')"',
    "eval 'python3 - <<EOF\nimport coordlib.apptoken\nEOF'",
    'eval "python3 - <<EOF\nimport coordlib.apptoken\nEOF"',
    'eval "$(cat <<\'X\'\npython3 - <<\'EOF\'\nimport coordlib.apptoken\nEOF\nX\n)"',
    'bash -c "$(cat <<\'X\'\npython3 - <<\'EOF\'\nimport coordlib.apptoken\nEOF\nX\n)"',
    "echo x | xargs -I{} python3 - <<'EOF'\nimport coordlib.apptoken\nEOF",
    "parallel python3 - ::: 1 <<'EOF'\nimport coordlib.apptoken\nEOF",
    "watch -n1 python3 - <<'EOF'\nimport coordlib.apptoken\nEOF",
    "script -q /dev/null python3 - <<'EOF'\nimport coordlib.apptoken\nEOF",
    "script -q /dev/null python3 <<'EOF'\nimport coordlib.apptoken\nEOF",
    "unbuffer python3 <<'EOF'\nimport coordlib.apptoken\nEOF",
    "(cd Tools/coord; python3 - <<'EOF'\nimport coordlib.apptoken\nEOF\n)",
    "(cd Tools/coord; python3 <<'EOF'\nimport coordlib.apptoken\nEOF\n)",
    "{ cd Tools/coord; python3; } <<'EOF'\nimport coordlib.apptoken\nEOF",
    "(python3) <<'EOF'\nimport coordlib.apptoken\nEOF",
    "( python3 ) <<'EOF'\nimport coordlib.apptoken\nEOF",
    "{ python3; } <<'EOF'\nimport coordlib.apptoken\nEOF",
    "if true; then python3; fi <<'EOF'\nimport coordlib.apptoken\nEOF",
    "for i in 1; do python3; done <<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 -- <<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 -E -s <<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 -X utf8 <<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 -W ignore <<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 -W ignore::DeprecationWarning - <<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 -Wignore - <<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 /dev/fd/0 <<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 /dev/stdin arg <<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 - <<'EOF' | cat\nimport coordlib.apptoken\nEOF",
    "python3 - <<'EOF' && echo ok\nimport coordlib.apptoken\nEOF",
    "python3 - <<'EOF' ; echo ok\nimport coordlib.apptoken\nEOF",
    "python3 - <<'EOF' &\nimport coordlib.apptoken\nEOF",
    "python3 - <<'EOF' >/tmp/o 2>&1\nimport coordlib.apptoken\nEOF",
    "python3 - <<'EOF' 2>&1 | tail -5\nimport coordlib.apptoken\nEOF",
    "cat <<'EOF' | python3\nimport coordlib.apptoken\nEOF",
    "cat <<'EOF' | timeout 30 python3\nimport coordlib.apptoken\nEOF",
    "cat <<'EOF' | (python3)\nimport coordlib.apptoken\nEOF",
    "cat <<'EOF' |\npython3 -\nimport coordlib.apptoken\nEOF",
    "cat <<'EOF' | \\\npython3 -\nimport coordlib.apptoken\nEOF",
    "cat <<'EOF' |\\\n  python3\nimport coordlib.apptoken\nEOF",
    "python3 \\\n  - <<'EOF'\nimport coordlib.apptoken\nEOF",
    "timeout 30 \\\n  python3 - <<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 - \\\n<<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 \\\n<<'EOF'\nimport coordlib.apptoken\nEOF",
    'python3 -c "$(cat <<\'EOF\'\nimport coordlib.apptoken\nEOF\n)"',
    'python3 -c "$(cat)" <<\'EOF\'\nimport coordlib.apptoken\nEOF',
    'timeout 30 python3 -c "$(cat)" <<\'EOF\'\nimport coordlib.apptoken\nEOF',
    "python3 -c 'import sys; exec(sys.stdin.read())' <<'EOF'\nimport coordlib.apptoken\nEOF",
    "timeout 30 python3 -c 'import sys; exec(sys.stdin.read())' <<'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 <(cat <<'EOF'\nimport coordlib.apptoken\nEOF\n)",
    "python3 < <(cat <<'EOF'\nimport coordlib.apptoken\nEOF\n)",
    "exec 3<<'EOF'\nimport coordlib.apptoken\nEOF\npython3 /dev/fd/3",
    "exec 0<<'EOF'\nimport coordlib.apptoken\nEOF\npython3",
    "exec <<'EOF'\nimport coordlib.apptoken\nEOF\npython3 -",
]


def wrapped_shapes() -> list:
    commands = []
    for python in ("python3", "/usr/bin/python3"):
        commands += [w + python + " - <<'EOF'\nimport coordlib.apptoken\nEOF" for w in MORE_WRAPPERS]
    commands += [w + "python3 <<'EOF'\nimport coordlib.apptoken\nEOF" for w in MORE_WRAPPERS]
    commands += ["echo 'import coordlib.apptoken' | " + w + "python3" for w in MORE_WRAPPERS]
    return commands + OTHER_SHAPES


STDIN_TAILS = [
    'python3 - 2>&1', 'python3 - 2>/dev/null', 'python3 - >out', 'python3 - >>out', 'python3 - &>out', 'python3 - >&2',
    'python3 - 2>&1 | tail -3', 'python3 - arg', 'python3 - --flag', 'python3 - -x', 'python3 - a b c', 'python3 -- -',
    'python3 -X utf8 -', 'python3 -X importtime -', 'python3 -W ignore -', 'python3 -W ignore::DeprecationWarning -',
    'python3 -Wignore -', 'python3 -Xutf8 -', 'python3 -B -E -s -', 'python3 -BEs -', 'python3 -u - 2>&1', 'python3 -I -',
    'python3 /dev/stdin', 'python3 /dev/stdin arg', 'python3 /dev/stdin 2>&1', 'python3 -u /dev/stdin',
    'python3 -X utf8 /dev/stdin', 'python3 /dev/fd/0', 'python3 /proc/self/fd/0', 'python3 -', 'python3 -;', 'python3 - ;',
    'python3 -&', 'python3 - &', 'python3 -)', '(python3 -)', '{ python3 - ; }', 'python3 -<&0', 'python3 - <&0',
    'python3 - 0<&0', 'python3 - <>/dev/null', 'python3 2>&1', 'python3 2>/dev/null', 'python3 >out', 'python3 &>out',
    'python3 -u 2>&1', 'python3 -X utf8 2>&1', 'python3 -W ignore 2>&1', 'python3 -i', 'python3 -q', 'python3 -E',
    'python3 --', 'python3 -- 2>&1', 'python3 -X dev', 'python3 -W error', 'python3 -Werror', 'python3 -O', 'python3;',
    'python3 &', 'python3)', 'python3 >&2', 'python3 1>&2', 'python3 2>&1 >/dev/null', 'python3 <&0', 'python3 0<&0',
    'python3 -X utf8 -W ignore -', 'python3 -W ignore -X utf8 - 2>&1', 'python3 -X frozen_modules=off -',
    "python3 -X 'utf8' -", "python3 -W 'ignore' -", "python3 - 'arg with space'", 'python3 "-"', "python3 '-'",
    'python3 -\\\n', 'python3 - \\\n  arg', 'python3 \\\n -',
]
PROSE = [
    "coord: don't let apptoken run from python3 stdin",
    "The guard's apptoken check now covers `python3 - <<EOF` and `timeout 30 python3 -`.",
    "It's the conductor's job; apptoken isn't reachable from python3 -c one-liners.",
    'python3 - reads apptoken? no: the guard blocks it',
    "Round 7: `echo '...' | python3 -` with apptoken is blocked; `python3 -m unittest test_apptoken` isn't.",
    "The hook's security check: `security find-generic-password` is blocked, and so is `coord token exec`.",
    "AppTokens: don't cache when python3 exits early",
    "can't run python3 <<EOF with apptoken in it",
    'python3 <<EOF with apptoken',
    '  python3 - # apptoken',
    "Tested: python3 -m unittest discover -s Tools/coord/tests (423 tests); apptoken's lock test isn't flaky.",
    "The guard's P3-b fix: a commit message's apostrophe no longer trips the python/apptoken check.",
    "- `python3 -c 'import coordlib.apptoken'` is still blocked",
    "echo 'x' | python3 - is a stdin form; apptoken can't be read that way",
    "Alex's review: python3 2>&1 is a stdin form too (apptoken).",
]
PROSE_FORMS = [
    lambda m: f"git commit -q -F - <<'EOF'\n{m}\n\nCo-Authored-By: x\nEOF",
    lambda m: f"git commit -q -F - <<EOF\n{m}\nEOF",
    lambda m: f"git commit -q -m \"$(cat <<'EOF'\n{m}\nEOF\n)\"",
    lambda m: f"gh pr create --title 'coord: guard' --body-file - <<'EOF'\n## Summary\n{m}\nEOF",
    lambda m: f"gh pr comment 1046 --body \"$(cat <<'EOF'\n{m}\nEOF\n)\"",
    lambda m: f"gh pr comment 1046 --body-file - <<-'EOF'\n\t{m}\n\tEOF",
    lambda m: f"cd /tmp/home && git add -A && git commit -q -F - <<'EOF'\n{m}\nEOF\ngit push -q origin HEAD",
    lambda m: f"python3 -m unittest discover -s Tools/coord/tests 2>&1 | tail -3 && git commit -q -F - <<'EOF'\n{m}\nEOF",
    lambda m: "Tools/coord/coord send --on 1046 '" + m.replace("'", "'\\''") + "'",
    lambda m: 'Tools/coord/coord send --on 1046 "' + m.replace('"', '\\"').replace("`", "\\`") + '"',
    lambda m: 'Tools/coord/coord conductor escalate "' + m.replace('"', '\\"').replace("`", "\\`") + '"',
    lambda m: f"git commit -q -F - <<'EOF'\n{m}\nEOF\ngh pr comment 1046 --body-file - <<'EOF'\n{m}\nEOF",
]
HEREDOC_SHAPES = [
    'python3 - <<-EOF\n\timport coordlib.apptoken\n\tEOF',
    "python3 - <<-'EOF'\n\t\timport coordlib.apptoken\n\t\tEOF",
    'python3 - <<"EOF"\nimport coordlib.apptoken\nEOF',
    'python3 - << EOF\nimport coordlib.apptoken\nEOF',
    "python3 - << 'EOF'\nimport coordlib.apptoken\nEOF",
    'python3 - <<\\EOF\nimport coordlib.apptoken\nEOF',
    'python3 - <<EOF\nimport coordlib.apptoken\nEOF',
    "python3 - <<''\nimport coordlib.apptoken\n\n",
    'cat <<A; python3 - <<B\nhi\nA\nimport coordlib.apptoken\nB',
    'python3 - <<A <<B\nprint(1)\nA\nimport coordlib.apptoken\nB',
    'cat <<A\nhi\nA\npython3 - <<B\nimport coordlib.apptoken\nB',
    'cat <<A\nEOF\nA\npython3 - <<B\nimport coordlib.apptoken\nB',
    "x=$(python3 - <<'EOF'\nimport coordlib.apptoken\nEOF\n)",
    'echo "$(python3 - <<\'EOF\'\nimport coordlib.apptoken\nEOF\n)"',
    "python3 - <<'EOF'\nx = 'EOF'\nimport coordlib.apptoken\nEOF",
    "python3 - <<'EOF'\nprint(1)\n EOF\nimport coordlib.apptoken\nEOF",
    "python3 - <<'EOF'\nimport coordlib.apptoken",
    "python3 - <<'EOF'\r\nimport coordlib.apptoken\r\nEOF\r\n",
    'python3 - <<EOF\r\nimport coordlib.apptoken\r\nEOF\r\n',
    "python3 - <<'EOF'\nimport coordlib.apptoken\nEOF \n",
    'python3 - <<-EOF\n  import coordlib.apptoken\n  EOF',
    "cat <<'EOF'\nit's fine\nEOF\ngit log -- Tools/coord/coordlib/apptoken.py",
    "python3 - <<'EOF'\nprint(1)\nEOF\ngit log -- Tools/coord/coordlib/apptoken.py",
    "python3 - <<-'EOF'\n\tprint(1)\n\tEOF\ngit log -- Tools/coord/coordlib/apptoken.py",
    'python3 - << "EOF"\nprint("it\'s")\nEOF\ngit log -- Tools/coord/coordlib/apptoken.py',
    'python3 - <<EOF\nprint(1)\nEOF\ngrep -n AppTokens Tools/coord/coordlib/apptoken.py',
    "git commit -q -F - <<'EOF'\ndon't\nEOF\ngrep -c apptoken x | python3 -c 'import sys; print(sys.stdin.read())'",
    "python3 - <<'EOF'\r\nprint(1)\r\nEOF\r\ngit log -- Tools/coord/coordlib/apptoken.py",
    "git commit -q -F - <<'EOF'\ncoord: it's done\nEOF\npython3 -m unittest test_apptoken",
]
CONDUCTOR_COMMANDS = [
    'cat "$CONDUCTOR_STATE_DIR/notes.md"', 'Tools/coord/coord inbox', 'Tools/coord/coord board',
    'Tools/coord/coord board --brief', 'Tools/coord/coord board --json | python3 -m json.tool | head -50',
    "Tools/coord/coord send --on 1046 'Round 7 is green; the apptoken tests pass under python3.9 and 3.14.'",
    'Tools/coord/coord send --to \'conductor@*\' "it\'s done: apptoken retry works"',
    "Tools/coord/coord send --to all 'CI broke: the python3 lane cannot import apptoken'",
    'Tools/coord/coord claim 991', 'Tools/coord/coord train add 1046', 'Tools/coord/coord train status',
    'Tools/coord/coord conductor escalate "#1046: apptoken tests fail under python3.9; it\'s the cached line"',
    'Tools/coord/coord conductor pause --for 2h', 'Tools/coord/coord conductor resume', 'Tools/coord/coord conductor status',
    'Tools/coord/coord token status', 'Tools/coord/coord outage', 'du -xhd1 /Users/dev 2>/dev/null | sort -h | tail -20',
    'tail -50 ~/Library/Logs/inisa/ci-health.log', 'gh pr checks 1046', 'gh run view 123456 --log-failed | tail -80',
    'gh run list --workflow ci.yml --limit 5',
    'gh pr view 1046 --json reviews,comments | python3 -c \'import json,sys; d=json.load(sys.stdin);'
    ' print(len(d["comments"]))\'',
    'gh pr diff 1046 -- Tools/coord/coordlib/apptoken.py | head -100',
    'git -C ~/Projects/INISA-worktrees/conductor status --short', 'git fetch -q origin && git log --oneline origin/main -5',
    'cd Tools/coord && python3 -m unittest test_apptoken 2>&1 | tail -3',
    'cd Tools/coord && /usr/bin/python3 -m unittest discover -s tests 2>&1 | tail -3',
    'git worktree add ../w-fix -b fix/apptoken origin/main && cd ../w-fix && git status',
    "git commit -q -F - <<'EOF'\ncoord: retry the apptoken mint once when python3's clock skews\n\n"
    "The conductor's run can't see it otherwise.\nEOF",
    "gh pr create --title 'coord: apptoken retry' --body-file - <<'EOF'\n## Summary\n"
    "It's a one-line fix in apptoken; python3 tests pass.\nEOF",
    'gh pr comment 1046 --body "$(cat <<\'EOF\'\nThe guard\'s apptoken check doesn\'t block python3 -m unittest.\nEOF\n)"',
    "grep -n 'def token' Tools/coord/coordlib/apptoken.py",
    "Tools/coord/coord send --on 1046 'the apptoken test'\\''s python3 run is green'",
    "Tools/coord/coord send --on 1046 'it'\\''s fixed: apptoken | python3 check'",
    "Tools/coord/coord send --on 1046 'it'\\''s fixed; python3 - is blocked with apptoken'",
    # after a `<<` that is not a heredoc: the other rules
    'echo "<<X"\ngh pr merge 5', 'echo "<<X"\ngit push --force origin HEAD',
    'echo "<<X"\nTools/coord/coord token exec -- gh api user', 'echo "<<X"\nsecurity find-generic-password -s x -w',
    'echo "<<X"\ncat ~/Library/Caches/inisa-coord/token.json', 'echo "<<X"\nTools/coord/coord train approve 5',
    "echo '<<X'\necho x > .claude/settings.json", 'echo "<<X"\npython3 -c "import coordlib.apptoken"',
    'echo "<<X"\necho "import coordlib.apptoken" | python3 -',
]

# fuzz_diff.py's grammar, every prefix × source × mode × interpreter once (the other parts rotate).
FUZZ_PREFIXES = ["", "cd Tools/coord && ", "true; ", "echo hi\n", "git status\n", "echo \"it's\"\n", "echo 'a' \"b\"\n",
                 "ls | head\n", "cat <<'Z'\nhi\nZ\n", "git commit -q -F - <<'Z'\ndon't\nZ\n", "x=1 ", "(", "{ ",
                 "timeout 30 ", "nice ", "env ", "exec ", "echo $((2<<1))\n", "echo '<<Q'\n", "# note <<Q\n",
                 "gh pr view 5 | python3 -m json.tool\n"]
FUZZ_SOURCES = ["heredoc'", "heredoc", "heredoc-", "pipe", "herestr"]
FUZZ_MODES = [" -", " - 2>&1", " - arg", " /dev/stdin", "", " -c 'import sys; exec(sys.stdin.read())'"]
FUZZ_PYTHONS = ["python3", "/usr/bin/python3", "python3.14", "python"]
FUZZ_OPTIONS = ["", " -B", " -u", " -X utf8", " -W ignore", " -I", " -E -s"]
# The last payload names only `AppTokens`, never a lowercase `apptoken` (round eight's blind spot).
FUZZ_PAYLOADS = ["import coordlib.apptoken", "from coordlib.apptoken import AppTokens", "from coordlib import apptoken",
                 "from coordlib.cli import AppTokens"]
FUZZ_AFTER = ["", " | tail -3", " && echo ok", "; echo done", " >/tmp/o"]


def fuzz_grammar() -> list:
    commands = []
    for i, (prefix, source, mode, python) in enumerate(itertools.product(FUZZ_PREFIXES, FUZZ_SOURCES, FUZZ_MODES,
                                                                          FUZZ_PYTHONS)):
        run = python + FUZZ_OPTIONS[i % 7] + mode
        payload, after = FUZZ_PAYLOADS[(i // 7) % len(FUZZ_PAYLOADS)], FUZZ_AFTER[(i // 3) % 5]
        if source == "pipe":
            command = prefix + f"echo '{payload}' | " + run + after
        elif source == "herestr":
            command = prefix + run + f" <<< '{payload}'" + after
        else:
            opener = {"heredoc'": "<<'EOF'", "heredoc": "<<EOF", "heredoc-": "<<-EOF"}[source]
            tab = "\t" if source == "heredoc-" else ""
            command = prefix + run + " " + opener + after + "\n" + tab + payload + "\n" + tab + "EOF"
        commands.append(command + {"(": "\n)", "{ ": "\n}"}.get(prefix, ""))
    return commands

# Round eight's review (review8/probe_rule.py, probe_other.py): the exemption, python's names,
# apptoken's spellings (AppTokens, PYTHONCASEOK, NFKC), the other rules, and ordinary work.
EXEMPTION_AND_SPELLINGS = [
    'python3 -m unittest test_apptokenX', 'python3 -m unittest xtest_apptoken', "python3 -c 'import test_apptoken.apptoken'",
    'python3 -m unittest test_apptoken; python3 - <<Y\napptoken\nY',
    "python3 -m unittest test_apptoken; python3 - <<'Y'\nimport coordlib.apptoken\nY",
    "python3 -c 'import coordlib.apptest_apptokentoken'", "python3 -c 'import coordlib.apptoken' # test_apptoken",
    'python3 -m test_apptoken', "python3 -c 'from test_apptoken import *'",
    "python3 -c 'import test_apptoken as t; print(t.AppTokens)'",
    "python3 -c 'print(AppTokens)'", "python3 -c 'from coordlib.cli import *; print(AppTokens)'",
    "python3 -c 'import coordlib.Apptoken'", "PYTHONCASEOK=1 python3 -c 'import coordlib.APPTOKEN as t'",
    "python3 -c 'import coordlib.ａpptoken'",
    "python3 -c 'import coordlib.\U0001d41a\U0001d429\U0001d429\U0001d42d\U0001d428\U0001d424\U0001d41e\U0001d427'",
    'python3 Tools/coord/coordlib/app*.py', "python3 -c 'import coordlib.app\\\ntoken'",
    "python3 -c 'import coordlib.xtest_apptoken'",
    'git push origin main;echo ok', 'git push origin main&& echo', '(git push origin main)',
]
PYTHON_NAMES = ["python", "python3", "python3.14", "/usr/bin/python3", "Python3", "PYTHON", "python3t", "python3.14t",
                "pythonw", "pythonw3", "ipython", "py", "python3-intel64", "python3.14-intel64", "python_3", "python3_14",
                "/opt/x/bin/Python3", "PyThOn3", "python3.", "python-3", "python3-config", "cpython3", "./python3",
                "~/.venv/bin/python", "$HOME/.pyenv/shims/python3", "Python", "python3m", "python3.9m", "python3dm",
                "python2", "python310"]
OTHER_RULES_AND_WORK = [
    'Tools/coord/coord token exec -- gh api user', 'coord --repo o/r token exec -- env', 'x; Tools/coord/coord token exec -- sh',
    'security find-generic-password -s x -w', '(security dump-keychain)', 'cat ~/Library/Caches/inisa-coord/t.json',
    'ls $HOME/Library/Caches/inisa-coord',
    'python3 -c "import subprocess; subprocess.run([\'security\',\'find-generic-password\',\'-s\',\'example-coord-github-app\'])"',
    'Tools/coord/coord train approve 5', './Tools/coord/coord --repo o/r train approve 5',
    "bash -c 'Tools/coord/coord train approve 5'", 'git push origin --force HEAD', 'git push -f', 'git push origin +HEAD:x',
    'git push origin HEAD:main', 'git push origin main', 'gh pr merge 5 --squash', 'gh api -X PUT repos/o/r/pulls/5/merge',
    'gh api --method POST repos/o/r/git/refs -f ref=x', 'gh secret list', 'gh variable set X', 'gh auth token', 'sudo ls',
    'launchctl list', "eval 'gh pr merge 5'", 'echo x > .claude/settings.json', 'rm -rf Tools/coord',
    "sed -i '' s/a/b/ CLAUDE.md", 'cp x .claude/hooks/conductor-guard.py',
    'cd /tmp/home && echo x > .claude/agents/conductor.md', 'tee ~/.claude/settings.json < x',
    'python3 -m unittest test_apptoken; gh pr merge 5', 'echo test_apptoken; security find-generic-password -s a',
    'Tools/coord/coord send --on 1046 "$(cat /tmp/note.txt)"',
    'Tools/coord/coord send --on 1046 "$(cat "$CONDUCTOR_STATE_DIR/note.md")"',
    'Tools/coord/coord send --on 1046 "$(< /tmp/note.txt)"', 'gh pr create --title x --body-file /tmp/body.md',
    'gh pr comment 5 -F /tmp/b.md', 'git commit -q -F /tmp/message.txt', 'gh pr edit 5 --body-file /tmp/b.md',
    'gh issue comment 5 --body-file "$CONDUCTOR_STATE_DIR/b.md"', 'Tools/coord/coord conductor escalate "$(cat /tmp/e.txt)"',
    'cd Tools/coord && python3 -m unittest discover -s tests 2>&1 | tail -3',
    '/usr/bin/python3 -m unittest discover -s Tools/coord/tests',
    'cd Tools/coord/tests && python3 -m unittest test_apptoken test_conductor -v 2>&1 | tail -5',
    'python3 -m unittest tests.test_apptoken.JWTTests', 'python3 Tools/coord/tests/test_apptoken.py',
    'git log --oneline -5 -- Tools/coord', "git grep -n 'def token' -- Tools/coord", 'grep -rn apptoken Tools/coord/coordlib',
    'git log -p -- Tools/coord/coordlib/apptoken.py | head -80', 'Tools/coord/coord board', 'Tools/coord/coord inbox',
    'Tools/coord/coord train status', "Tools/coord/coord send --on 1046 'apptoken retry landed'",
    "Tools/coord/coord send --to all 'python3.14 lane is red'",
    "Tools/coord/coord board --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))'",
    "gh pr view 1046 --json files -q '.files[].path' | grep apptoken", 'Tools/coord/coord token status',
    'cd Tools/coord && python3 -m unittest discover -s tests && git add coordlib/apptoken.py && git commit -q -F /tmp/m.txt',
    'python3 -m unittest discover -s Tools/coord/tests 2>&1 | tail -3; git diff --stat -- Tools/coord/coordlib/apptoken.py',
    'git log --oneline --grep apptoken | head; python3 --version',
    'gh pr view 1046 --json body | python3 -c \'import json,sys; print(json.load(sys.stdin)["body"])\' | grep -n apptoken',
    "cat > /tmp/body.md <<'EOF'\nThe apptoken fix; python3 tests pass.\nEOF",
    "printf '%s\\n' 'apptoken retry: python3 tests green' > /tmp/body.md",
    "Tools/coord/coord send --on 1046 'apptoken ok on Python 3.9'",
    'ls ~/.pyenv/versions/3.14/lib/python3.14/site-packages | grep apptoken',
]


def round_eight() -> list:
    return (EXEMPTION_AND_SPELLINGS + [f"echo 'import coordlib.apptoken' | {name} -" for name in PYTHON_NAMES]
            + OTHER_RULES_AND_WORK)


# Round twelve's review (review12/probe12.py), verbatim: the new rules' blocked and allowed
# lists and the spellings it tried against them.
ROUND_TWELVE = [
    "Tools/coord/coord token exec -- env", "coord token exec", "./coord token exec -- gh api user",
    "Tools/coord/coord --repo o/r token exec -- env", "Tools/coord/coord --repo=o/r token exec -- env",
    "(coord token exec -- env)", "`coord token exec`", "$(coord token exec -- env)", "x;coord token exec",
    "a&&coord token exec", "a|coord token exec", "sh -c 'coord token exec'", 'sh -c "coord token exec"',
    "coord\ttoken\texec", "coord token exec\n",
    "Tools/coord/coord --repo o/r train approve 5 --note x", "coord --repo=o/r train approve 5",
    "coord  --repo   o/r  train  approve 5",
    "security find-generic-password -s x -w", "/usr/bin/security find-generic-password -w -s x", "security find-g -s x -w",
    "security find-internet-password -s x -w", "security find-i -w", "security dump-keychain -d", "security dump-k -d",
    "security export -k login.keychain -t privKeys", "security -v find-generic-password -s x", "security -i find-g",
    "echo | security find-generic-password", "env security find-generic-password", "'security' find-generic-password",
    "cat ~/Library/Caches/inisa-coord/tokens.json", "ls $HOME/Library/Caches/inisa-coord",
    "cat ~/library/caches/INISA-COORD/x", "cat ~/Library/Caches/Inisa-Coord/x",
    "cp /Users/dev/Library/Caches/inisa-coord/t /tmp",
    "security find-generic-password -s example-coord-github-app -w", "echo EXAMPLE-COORD-GITHUB-APP",
    "echo Example-Coord-GitHub-App",
    "python3 -c 'import subprocess; subprocess.run([\"security\",\"find-generic-password\",\"-s\",\"example-coord-github-app\"])'",
    "python3 -c 'import coordlib.apptoken'", "echo 'import coordlib.apptoken' | python3 -",
    "Python3 -c 'import coordlib.APPTOKEN'", "PYTHON3 - <<'E'\nfrom coordlib import apptoken\nE",
    "python3 -c 'import coordlib.\uff41pptoken'", "/usr/bin/python3 x.py apptoken",
    "python3 -m test_apptoken; python3 -c 'import coordlib.apptoken'", "python3 -c 'import test_apptoken as t; t.apptoken'",
    "python3 -m unittest test_apptoken_x_apptoken", "python3 -c 'import coordlib._apptoken'",
    "python3 -c 'import coordlib.test_apptoken, coordlib.apptoken'", "python3.14 -c 'from coordlib.apptoken import *'",
    "python3 -c 'import coordlib.\uff21\uff30\uff30\uff34\uff2f\uff2b\uff25\uff2e'",
    "Tools/coord/coord token status", "Tools/coord/coord board", "Tools/coord/coord --repo o/r board", "coord train status",
    "python3 -m unittest test_apptoken", "cd Tools/coord/tests && python3 -m unittest test_apptoken -v",
    "python3 -m unittest discover -s Tools/coord/tests", "git log -- Tools/coord/coordlib/apptoken.py",
    "grep -rn apptoken Tools/coord", "security list-keychains", "security find-certificate -a",
    "security verify-cert -c x.pem", "security show-keychain-info", "echo securityexport", "ls ~/Library/Caches",
    "gh pr view 1046", "git status", "python3 --version", "python3 Tools/coord/tests/test_apptoken.py",
    "python3 -m unittest tests.test_apptoken.JWTTests",
    "Tools/coord/coord --rep o/r token exec -- env", "Tools/coord/coord --re=o/r token exec -- env",
    "Tools/coord/coord --r o/r token exec -- env", "Tools/coord/coord --repo a/b --repo o/r token exec -- env",
    "Tools/coord/coord --repo= token exec -- env", "Tools/coord/coord -- token exec -- env",
    "Tools/coord/coord --rep o/r train approve 5 --note x",
    "Tools/coord/coord --repo a/b --repo o/r train approve 5 --note x", "Tools/coord/coord -- train approve 5 --note x",
    "Tools/coord/COORD token exec -- env", "Tools/Coord/Coord token exec -- env", "SECURITY dump-keychain -d",
    "Security find-generic-password -s x -w",
    "/usr/bin/SECURITY export -k login.keychain -t privKeys -f pkcs12 -P x -o /tmp/k.p12",
    "security f -s x -w", "security fi -s x -w", "security find -s x -w", "security find- -s x -w", "security du -d",
    "security dump -d", "security dump- -d", "security e -k login.keychain -t privKeys -o /tmp/k",
    "security expor -t privKeys", "python3 -m test_apptoken", "cd /tmp/x && python3 -m test_apptoken",
    "python3 -c 'from test_apptoken import *; print(AppTokens)'", "cat ~/Library//Caches/inisa-coord/x",
    "cat ~/Library/Caches/./inisa-coord/x", "cat ~/Library/Caches/inisa-coor?/x", "cd ~/Library/Caches && cat inisa-coord/x",
    "cat ~/Library/Caches/inisa-c*/x", "security find-generic-password -s inisa-coord-github-'app' -w",
    "security f -s inisa-coord-github-'app' -w",
]
# Its insert-only mutant (`if command.endswith(" #r12"): return ""` in bash_verdict) kept every
# line of main's guard and every test green. Commands main blocks, with trailing comments:
MAIN_BLOCKS = ["gh pr merge 5 --squash", "git push origin HEAD:main", "git push origin --force HEAD",
               "gh api -X PUT repos/o/r/pulls/1/merge", "Tools/coord/coord train approve 1", "gh secret list",
               "gh auth token", "sudo ls", "launchctl list", "bash -c 'gh pr merge 5'", "echo x > CLAUDE.md",
               "rm -rf Tools/coord"]
TRAILING_COMMENTS = [command + suffix for command in MAIN_BLOCKS for suffix in (" #", " # note", " #r12")]


def corpus_sources() -> dict:
    return {
        "test_conductor.py": conductor_test_commands(),
        "rounds five and six": PROBES,
        "round seven's list": ROUND_SEVEN,
        "wrapped heredocs": wrapped_heredocs(),
        "not heredoc openers": hidden_by_an_opener(),
        "wrappers and shapes": wrapped_shapes(),
        "stdin forms": ["echo 'import coordlib.apptoken' | " + tail for tail in STDIN_TAILS],
        "prose": [form(message) for form in PROSE_FORMS for message in PROSE],
        "heredoc shapes": HEREDOC_SHAPES,
        "conductor commands": CONDUCTOR_COMMANDS,
        "fuzz grammar": fuzz_grammar(),
        "round eight's review": round_eight(),
        "round twelve's review": ROUND_TWELVE,
        "trailing comments": TRAILING_COMMENTS,
    }


# Each source's DISTINCT commands when it was last grown: emptying or cutting one fails the test.
MINIMUM = {"test_conductor.py": 245, "rounds five and six": 151, "round seven's list": 10, "wrapped heredocs": 138,
           "not heredoc openers": 67, "wrappers and shapes": 216, "stdin forms": 76, "prose": 180, "heredoc shapes": 29,
           "conductor commands": 45, "fuzz grammar": 2520, "round eight's review": 120,
           "round twelve's review": 106, "trailing comments": 36}
LEGACY_BLOB = "c7a05e3547d96008cf3c59885576616519c9e08d"  # `git hash-object` of 4f7d4a4fd's guard

# Commands the old guard blocked that this one allows on purpose. Each needs a reason. None since
# round eight, which blocks python and apptoken anywhere in one command.
INTENDED_UNBLOCKS = {}


def names_python_and_apptoken(command: str) -> bool:
    """The apptoken rule, restated here so the guard is checked against it, not against itself:
    on the command's NFKC case-folded form, a python token and `apptoken`, the test module's
    name (with no word character before it) aside."""
    folded = unicodedata.normalize("NFKC", command).casefold()
    return (bool(re.search(r"\bpython[0-9.]*\b|/python", folded))
            and "apptoken" in re.sub(r"(?<!\w)test_apptoken", "", folded))


class GuardDifferentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = load(GUARD, "conductor_guard_now")
        cls.legacy = load(LEGACY_GUARD, "conductor_guard_4f7d4a4fd")
        cls.main = load(MAIN_GUARD, "conductor_guard_59e25cc6f")
        cls.sources = corpus_sources()
        cls.corpus = list(dict.fromkeys(command for source in cls.sources.values() for command in source))

    @staticmethod
    def blocked(guard, command: str) -> bool:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp/home"}
        return bool(guard.verdict(payload, {"CLAUDE_PROJECT_DIR": "/tmp/home"}))

    def test_every_source_is_there(self):
        self.assertEqual(set(self.sources), set(MINIMUM))
        for name, commands in self.sources.items():
            self.assertGreaterEqual(len(set(commands)), MINIMUM[name], name)
        # Round eight's blind spot: fuzz payloads that name only `AppTokens`.
        self.assertTrue(any("AppTokens" in command and "apptoken" not in command
                            for command in self.sources["fuzz grammar"]))

    def test_the_old_guard_is_4f7d4a4fds(self):
        with open(LEGACY_GUARD, "rb") as handle:
            data = handle.read()
        self.assertEqual(hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(), LEGACY_BLOB)
        self.assertTrue(all(self.blocked(self.legacy, command) for command in wrapped_heredocs()))
        self.assertFalse(self.blocked(self.legacy, "python3 -c \"print(1)\"; git log -- Tools/coord/coordlib/apptoken.py"))

    def test_mains_guard_is_mains(self):
        with open(MAIN_GUARD, "rb") as handle:
            data = handle.read()
        self.assertEqual(hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(), MAIN_BLOB)

    def test_nothing_mains_guard_blocks_is_allowed_now(self):
        # Main's lines are pinned (test_conductor's structural test); this shows, over the corpus,
        # that the lines #1046 inserts only add refusals.
        main_blocks = [command for command in self.corpus if self.blocked(self.main, command)]
        self.assertGreater(len(main_blocks), 100)
        self.assertEqual([command for command in main_blocks if not self.blocked(self.guard, command)], [],
                         "blocked by main's guard, allowed now")

    def test_nothing_the_old_guard_blocked_is_allowed_now(self):
        regressions = [command for command in self.corpus
                       if command not in INTENDED_UNBLOCKS and self.blocked(self.legacy, command)
                       and not self.blocked(self.guard, command)]
        self.assertEqual(regressions, [], "blocked at 4f7d4a4fd, allowed now")

    def test_every_command_that_names_python_and_apptoken_is_blocked(self):
        named = [command for command in self.corpus if names_python_and_apptoken(command)]
        self.assertGreater(len(named), 2500)
        self.assertEqual([command for command in named if not self.blocked(self.guard, command)], [])

    def test_each_intended_unblock_was_blocked_and_is_allowed_now(self):
        for command in INTENDED_UNBLOCKS:
            self.assertIn(command, self.corpus)
            self.assertTrue(self.blocked(self.legacy, command), command)
            self.assertFalse(self.blocked(self.guard, command), command)


_INISA_CONFIG_PATH = None


def setUpModule():
    global _INISA_CONFIG_PATH
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import conductor_config_fixture
    _INISA_CONFIG_PATH = conductor_config_fixture.use_inisa_config()


def tearDownModule():
    import conductor_config_fixture
    conductor_config_fixture.restore(_INISA_CONFIG_PATH)


if __name__ == "__main__":
    unittest.main()
