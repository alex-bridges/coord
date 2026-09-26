"""Time every rule of .claude/hooks/conductor-guard.py on adversarial inputs built from its own
literal keywords, repeated to just under the length cap. Run by test_conductor in a child
process, so a slow rule fails that test instead of hanging the suite.

Usage: python3 guard_timing.py <guard path>. Prints one JSON object: for each rule, its name,
the number of inputs, and the slowest time for the whole verdict and for the rule alone.
"""

import importlib.util
import json
import re
import sys
import time

LENGTH = 19900  # just under the guard's 20,000-character cap


def load(path: str):
    spec = importlib.util.spec_from_file_location("conductor_guard_timed", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def keywords(pattern: str) -> list:
    """The literal words of a regex, in order: escapes and character classes removed."""
    pattern = re.sub(r"\\.", " ", re.sub(r"\[[^\]]*\]", " ", pattern))
    return list(dict.fromkeys(re.findall(r"[-:/+]*[A-Za-z][\w/-]*", pattern)))


def inputs(pattern: str) -> list:
    """Repeated to LENGTH: each prefix of the pattern's keywords, ending in a space or in `X `
    (so a `\\b` after the last keyword fails and the rule keeps searching); and each punctuation
    character in the pattern, alone and before the first keyword (`((((`, `(coord(coord`)."""
    words = keywords(pattern)
    units = [" ".join(words[:count]) + tail for count in range(1, len(words) + 1) for tail in (" ", "X ")]
    for character in sorted(set(re.findall(r"[^\w\s\\]", pattern))):
        units += [character, character + (words[0] if words else "")]
    return [(unit * (LENGTH // len(unit) + 1))[:LENGTH] for unit in units]


def rules(guard) -> list:
    """(name, the rule's pattern text, a call that runs the rule alone) for every rule."""
    found = []
    for index, (rule, _) in enumerate(guard.RULES):
        found.append((f"RULES[{index}]", rule.pattern, lambda c, rule=rule: rule.search(guard.executable_text(c))))
    for index, (rule, _) in enumerate(guard.RAW_RULES):
        found.append((f"RAW_RULES[{index}]", rule.pattern, lambda c, rule=rule: rule.search(c)))
    found.append(("apptoken", guard._PYTHON.pattern + " apptoken test_apptoken", guard._python_apptoken))
    found.append(("writes", guard._WRITES.pattern,
                  lambda c: guard._WRITES.search(guard._HARMLESS_REDIRECT.sub(" ", guard.executable_text(c)))))
    return found


def measure(guard) -> dict:
    report = {"rules": len(guard.RULES) + len(guard.RAW_RULES), "measured": []}
    for name, pattern, alone in rules(guard):
        commands = inputs(pattern)
        slowest_verdict = slowest_alone = 0.0
        for command in commands:
            start = time.monotonic()
            guard.bash_verdict(command, "/tmp/home", "/tmp/home")
            slowest_verdict = max(slowest_verdict, time.monotonic() - start)
            start = time.monotonic()
            alone(command)
            slowest_alone = max(slowest_alone, time.monotonic() - start)
        report["measured"].append({"rule": name, "inputs": len(commands), "verdict": round(slowest_verdict, 3),
                                   "alone": round(slowest_alone, 3)})
    return report


if __name__ == "__main__":
    print(json.dumps(measure(load(sys.argv[1]))))
