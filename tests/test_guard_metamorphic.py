"""Meaning-preserving shell rewrites of every command the conductor guard blocks.

Round nine of the #1046 review found one class three times: the guard matched a spelling, and
the shell accepted another spelling of the same command. `git push origin \\<newline>main`,
`git push origin main>/dev/null` and `GIT push --force` all ran and were all allowed. This test
takes every command the guard blocks (the differential corpus, which includes every command in
test_conductor.py's guard tests) and rewrites it the ways the shell does not care about. Every
variant must be blocked too:

(a) `\\<newline>` after one space or tab, with and without indentation after it;
(b) the case of a program name, and of a path's directories (macOS resolves both without
    case): upper, Title and mixed;
(c) a redirection right after a word: `>/dev/null`, `</dev/null`, `>&2`, and (after a space)
    `2>&1`, `> /dev/null`, `2> /dev/null`, `< /dev/null` and `<<< x`;
(d) the command wrapped in `( … )` and in `$( … )`;
(e) something before the command: a line (`true`), indentation, `{ … }`, or a redirection.

Rewrites that would change the meaning are not made (SKIPPED below says which and why).
Run: python3 -m unittest discover -s Tools/coord/tests
"""

from __future__ import annotations

import collections
import os
import re
import unittest

import test_guard_differential as differential

GUARD = differential.GUARD

# Programs the rules key on, and the ones that run the command after them. Builtins are not
# here: the shell matches `exec`, `eval` and `cd` with case (`CD` runs /usr/bin/cd, a no-op).
PROGRAMS = {"git", "gh", "coord", "security", "sudo", "launchctl", "env", "xargs", "nohup", "time", "sed", "perl",
            "tee", "rm", "mv", "cp", "ln", "chmod", "chown", "truncate", "install", "dd", "patch", "rsync", "bash", "sh",
            "zsh", "dash"}
# A path whose directories the rewrite may recase: a file path. Not an API path (`repos/o/r/…`),
# a git ref (`refs/heads/main`) or a variable (`$HOME` itself).
FILE_PATH = re.compile(r"^(~|\.{1,2}/|/|\$HOME/|\.claude/|Tools/|Library/)")
WORD = re.compile(r"[^\s;&|()<>`'\"]+")
SKIPPED = {
    # rewrite: why it is not made
    "(b) builtins": "exec, eval and cd are shell builtins, matched with case",
    "(b) the final path component": "the case of a file name is left alone (the review's scope)",
    "(b) non-file paths": "API paths, git refs and $HOME are case-sensitive",
    "(c) after a word ending in a backslash": "the backslash would escape the redirection (or end a continuation)",
    "(c) after a redirection operator": "`gh > /dev/null` would become `gh >>/dev/null /dev/null`",
}


def mixed(text: str) -> str:
    return "".join(c.upper() if i % 2 else c.lower() for i, c in enumerate(text))


CASES = (str.upper, str.title, mixed)


def continuations(command: str):
    for match in re.finditer(r"[ \t]", command):
        for indent in ("", "  "):
            yield command[:match.end()] + "\\\n" + indent + command[match.end():]


def recased(command: str):
    """A program's name, and a file path's directories, in each case."""
    for match in WORD.finditer(command):
        word = match.group(0)
        directory, slash, name = word.rpartition("/")
        is_program = name.casefold() in PROGRAMS or bool(re.fullmatch(r"python[0-9.]*", name.casefold()))
        is_path = bool(slash) and bool(FILE_PATH.match(word))
        if not (is_program or is_path):
            continue
        for case in CASES:
            parts = directory.split("/") if slash else []
            if is_path:
                parts = [part if part in ("", "~", ".", "..") or part.startswith("$") else case(part) for part in parts]
            new_word = "/".join(parts + [case(name) if is_program else name]) if slash else case(name)
            if new_word != word:
                yield command[:match.start()] + new_word + command[match.end():]


REDIRECTIONS = (">/dev/null", "</dev/null", ">&2", " 2>&1", " > /dev/null", " 2> /dev/null", " < /dev/null", " <<< x")


def redirected(command: str):
    for match in re.finditer(r"\S+", command):
        if match.group(0).endswith("\\"):
            continue  # SKIPPED: the backslash would escape the redirection instead
        if match.group(0)[-1] in "<>|&" and re.search(r"[<>]", match.group(0)):
            continue  # SKIPPED: after a redirection operator, the insertion would become its target
        for redirection in REDIRECTIONS:
            yield command[:match.end()] + redirection + command[match.end():]


def wrapped(command: str):
    yield "(" + command + "\n)"
    yield "$(" + command + "\n)"


def prefixed(command: str):
    """(`{ …\n}`, not `{ …; }`: a trailing comment or heredoc would swallow `; }`.)"""
    yield "true\n" + command
    yield "  " + command
    yield "{ " + command + "\n}"
    yield ">/dev/null " + command


REWRITES = {"(a) continuation": continuations, "(b) case": recased, "(c) redirection": redirected,
            "(d) subshell": wrapped, "(e) prefix": prefixed}
# Each kind makes at least this many variants of the blocked commands (so dropping one fails).
MINIMUM_VARIANTS = {"(a) continuation": 25000, "(b) case": 15000, "(c) redirection": 130000, "(d) subshell": 6000,
                    "(e) prefix": 12000}


def blocked(guard, command: str) -> bool:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "/tmp/home"}
    return bool(guard.verdict(payload, {"CLAUDE_PROJECT_DIR": "/tmp/home"}))


# The rewrites made at every position, (a) and (c), take every hand-written command but one
# fuzz-grammar command in four: a deterministic sample, as the grammar repeats its shapes.
EVERY_POSITION = ("(a) continuation", "(c) redirection")
FUZZ_STRIDE = 4


def escapes(guard, commands, fuzz=frozenset()) -> dict:
    """For each rewrite: the number of variants made, and the variants the guard allows."""
    report = {}
    sampled = [command for index, command in enumerate(commands) if command not in fuzz or index % FUZZ_STRIDE == 0]
    for kind, rewrite in REWRITES.items():
        made, allowed = 0, []
        for command in (sampled if kind in EVERY_POSITION else commands):
            for variant in rewrite(command):
                made += 1
                if not blocked(guard, variant):
                    allowed.append((command, variant))
        report[kind] = (made, allowed)
    return report


class GuardMetamorphicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guard = differential.load(GUARD, "conductor_guard_metamorphic")
        corpus = list(dict.fromkeys(command for source in differential.corpus_sources().values() for command in source))
        cls.blocked_commands = [command for command in corpus if blocked(cls.guard, command)]
        cls.report = escapes(cls.guard, cls.blocked_commands, frozenset(differential.fuzz_grammar()))

    def test_the_rewrites_are_the_shells(self):
        self.assertIn("git push origin \\\nmain", list(continuations("git push origin main")))
        self.assertIn("GIT push --force", list(recased("git push --force")))
        self.assertIn("cat ~/LIBRARY/CACHES/INISA-COORD/ops.json", list(recased("cat ~/Library/Caches/inisa-coord/ops.json")))
        self.assertNotIn("gh api -X PUT REPOS/o/r/pulls/5/merge", list(recased("gh api -X PUT repos/o/r/pulls/5/merge")))
        self.assertIn("git push origin main>/dev/null", list(redirected("git push origin main")))
        self.assertIn("git push origin main 2>&1", list(redirected("git push origin main")))
        self.assertEqual(list(wrapped("sudo ls")), ["(sudo ls\n)", "$(sudo ls\n)"])
        self.assertIn("gh pr merge 5 > /dev/null", list(redirected("gh pr merge 5")))
        self.assertEqual(list(prefixed("sudo ls")), ["true\nsudo ls", "  sudo ls", "{ sudo ls\n}", ">/dev/null sudo ls"])

    def test_every_variant_of_a_blocked_command_is_blocked(self):
        self.assertGreater(len(self.blocked_commands), 3000)
        self.assertEqual(set(self.report), set(MINIMUM_VARIANTS), "every kind of rewrite")
        for kind, minimum in MINIMUM_VARIANTS.items():
            made, allowed = self.report[kind]
            self.assertGreaterEqual(made, minimum, kind)
            self.assertEqual(allowed[:10], [], f"{kind}: {len(allowed)} of {made} variants allowed")


if __name__ == "__main__":
    unittest.main()
