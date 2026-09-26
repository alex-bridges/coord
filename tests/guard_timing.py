"""Time every rule of .claude/hooks/conductor-guard.py on adversarial inputs built from its own
literal keywords and punctuation, repeated to just under the length cap.

Each rule runs in its own child process with a timeout, so a slow rule is a reported failure,
not a hang. The keywords come from the rule's pattern with optional groups, character classes
and escapes (with their quantifiers: `\\s+` is not `+`) taken out, in order and with repeats;
all of them joined is the rule's witness, which the rule must match, so a harness that builds
inputs the rule never engages fails too.

Usage: python3 guard_timing.py <guard path> [<rule name>]. With a rule name it prints that
rule's measurement as JSON; without, the names of the rules.
"""

import importlib.util
import json
import re
import subprocess
import sys
import time

LENGTH = 19900  # just under the guard's 20,000-character cap
BUDGET = 2.0  # seconds, for one whole verdict or one rule on one input


def load(path: str):
    spec = importlib.util.spec_from_file_location("conductor_guard_timed", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def without_optional_groups(pattern: str) -> str:
    """The pattern with every `(…)?`, `(…)*` and `(…)??` group removed."""
    out, starts, i = [], [], 0
    while i < len(pattern):
        character = pattern[i]
        if character == "\\":
            out.append(pattern[i:i + 2])
            i += 2
        elif character == "[":
            end = i + 1 + (pattern[i + 1:i + 2] == "^")
            end += pattern[end:end + 1] == "]"
            while end < len(pattern) and pattern[end] != "]":
                end += 2 if pattern[end] == "\\" else 1
            out.append(pattern[i:end + 1])
            i = end + 1
        elif character == "(":
            starts.append(len(out))
            out.append(character)
            i += 1
        elif character == ")":
            start = starts.pop() if starts else None
            out.append(character)
            i += 1
            if start is not None and pattern[i:i + 1] in ("?", "*") and pattern[i:i + 1]:
                del out[start:]
                i += 1 + (pattern[i + 1:i + 2] == "?")
        else:
            out.append(character)
            i += 1
    return "".join(out)


def keywords(pattern: str) -> list:
    """The literal words of a regex, in order and with repeats."""
    pattern = without_optional_groups(pattern)
    pattern = re.sub(r"\\S\+", " x ", pattern)  # a word: `--repo(=|\s+)\S+` needs one
    pattern = re.sub(r"\[[^\]]*\][+*?]?", " ", pattern)  # character classes
    pattern = re.sub(r"\\.[+*?]?", " ", pattern)  # escapes, with their quantifier
    pattern = re.sub(r"\(\?(?:[a-zA-Z]+:|<?[=!]|:)", " ", pattern)  # group syntax: (?i: (?= (?<! (?:
    return re.findall(r"[-:/]*[A-Za-z][\w/-]*", pattern)


def inputs(pattern: str) -> list:
    """Repeated to LENGTH: each prefix of the keywords, ending in a space or in `X ` (so a `\\b`
    after the last keyword fails and the rule keeps searching); and each punctuation character
    of the pattern: alone, before the first keyword, and before it and a space (`((((`,
    `(coord(coord`); and each keyword alone and as a flag (`-exec -exec …`, where every
    `exec` is a new start)."""
    words = keywords(pattern)
    units = [" ".join(words[:count]) + tail for count in range(1, len(words) + 1) for tail in (" ", "X ")]
    for character in sorted(set(re.findall(r"[^\w\s\\]", pattern))):
        first = words[0] if words else ""
        units += [character, character + first, character + first + " "]
    # Each keyword alone, and as a flag (`-exec -exec …`: a word inside every flag).
    units += [word + " " for word in words] + ["-" + word + " " for word in words]
    return [(unit * (LENGTH // len(unit) + 1))[:LENGTH] for unit in dict.fromkeys(units)]


def rules(guard) -> dict:
    """name -> (the rule's pattern text, a call that runs the rule alone on what the guard gives
    it, whether a witness must match). Main's RULES and the write check read executable_text;
    RAW_RULES and the apptoken check read the raw command. The helpers every verdict runs
    (script extraction, `cd`) are timed too."""
    found = {}
    for index, (pattern, _) in enumerate(guard.RULES):
        rule = re.compile(pattern)
        found[f"RULES[{index}]"] = (pattern, lambda c, rule=rule: rule.search(guard.executable_text(c)), True)
    for index, (rule, _) in enumerate(guard.RAW_RULES):
        found[f"RAW_RULES[{index}]"] = (rule.pattern, rule.search, True)
    found["apptoken"] = (guard._PYTHON.pattern + " apptoken test_apptoken", guard._python_apptoken, True)
    found["writes"] = (guard._WRITES.pattern,
                       lambda c: guard._WRITES.search(guard._HARMLESS_REDIRECT.sub(" ", guard.executable_text(c))), True)
    found["script"] = (guard._SCRIPT.pattern, lambda c: list(guard._SCRIPT.finditer(c)), False)
    found["cd"] = (guard._CD.pattern, guard._CD.match, False)
    return found


def measure(guard, name: str) -> dict:
    pattern, alone, needs_witness = rules(guard)[name]
    witness = " ".join(keywords(pattern))
    commands = inputs(pattern)
    slowest_verdict = slowest_alone = 0.0
    for command in commands:
        start = time.monotonic()
        guard.bash_verdict(command, "/tmp/home", "/tmp/home")
        slowest_verdict = max(slowest_verdict, time.monotonic() - start)
        start = time.monotonic()
        alone(command)
        slowest_alone = max(slowest_alone, time.monotonic() - start)
    return {"rule": name, "inputs": len(commands), "witness": witness,
            "witness matches": bool(alone(witness)) if needs_witness else None,
            "verdict": round(slowest_verdict, 3), "alone": round(slowest_alone, 3)}


def measure_in_children(guard_path: str, timeout: float = 10.0, only=None, parallel: int = 4) -> tuple:
    """(the measurement of each rule, the failures): each rule in its own child, `parallel` at a
    time; a child still running after `timeout` seconds is killed and reported as too slow."""
    names = only or json.loads(subprocess.run([sys.executable, __file__, guard_path], capture_output=True,
                                              text=True, check=True, timeout=60).stdout)
    pending, running, results, failures = list(names), {}, {}, []
    while pending or running:
        while pending and len(running) < parallel:
            name = pending.pop(0)
            running[name] = (subprocess.Popen([sys.executable, __file__, guard_path, name], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, text=True), time.monotonic())
        for name, (child, started) in list(running.items()):
            if child.poll() is None:
                if time.monotonic() - started > timeout:
                    child.kill()
                    child.communicate()
                    del running[name]
                    failures.append(f"{name}: slower than {timeout:g} s on its own inputs")
                continue
            out, err = child.communicate()
            del running[name]
            if child.returncode:
                failures.append(f"{name}: the measurement failed: {err[-500:]}")
                continue
            rule = results[name] = json.loads(out)
            if rule["witness matches"] is False:
                failures.append(f"{name}: its keywords ({rule['witness']!r}) do not match it, so its inputs miss it")
            if max(rule["verdict"], rule["alone"]) >= BUDGET:
                failures.append(f"{name}: {max(rule['verdict'], rule['alone'])} s, over the {BUDGET:g} s budget")
        time.sleep(0.02)
    return [results[name] for name in names if name in results], failures


if __name__ == "__main__":
    guard = load(sys.argv[1])
    if len(sys.argv) > 2:
        print(json.dumps(measure(guard, sys.argv[2])))
    else:
        print(json.dumps(list(rules(guard))))
