"""Run TLC on each contract, its fault controls, and its reachability checks."""

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


FORMAL = Path(__file__).resolve().parent
NORMAL = 'CONSTANT Mode = "Normal"'

# Each fault changes one modeled decision. Its counterexample must violate the
# named safety invariant. Witnesses run the normal model with one extra invariant
# denying that a required outcome is reachable.
CHECKS = {
    "MergeTrain": (
        dict.fromkeys(
            ("SkipTree", "SkipParents", "SkipCount", "SkipCap", "SkipTrust", "IgnoreReceiptTip",
             "IgnoreCITip", "SkipGreen", "SkipEligibility", "StaleEligibility", "LiveBranch",
             "ForceUpdate"), "SafeAdvance"
        ),
        ("NeverAdvanceOne", "NeverAdvanceTwo", "NeverAdvanceThree",
         "NeverAdvanceAfterBranchMove", "NeverAdvanceAfterPRChange"),
    ),
    "CredentialStore": (
        {"SkipStatus": "UnavailableStoreIsRejected", "IgnoreAccount": "StoreMatchesRequest"},
        ("NeverReplacedWithNeighbor",),
    ),
    "StarterGuard": (
        {"AlwaysAllow": "RequiredDenials", "AlwaysDeny": "RequiredAllows"},
        ("NeverWakeDeniesMerge",),
    ),
}


def check(args, module, mode="Normal", invariant=None):
    case = invariant if mode == "Normal" and invariant else mode
    label = f"{module}-{case}"
    with tempfile.TemporaryDirectory(prefix="coord-tlc-") as directory:
        work = Path(directory)
        shutil.copyfile(FORMAL / f"{module}.tla", work / f"{module}.tla")
        config = (FORMAL / f"{module}.cfg").read_text().splitlines()
        if config.count(NORMAL) != 1:
            raise SystemExit(f"{module}.cfg must declare {NORMAL} once")
        config[config.index(NORMAL)] = f'CONSTANT Mode = "{mode}"'
        if mode == "Normal" and invariant:
            config.append(f"INVARIANT {invariant}")
        (work / "check.cfg").write_text("\n".join(config) + "\n")
        result = subprocess.run(
            [args.java, "-Xmx512m", "-XX:+UseParallelGC", f"-Djava.io.tmpdir={work}",
             "-cp", str(args.jar), "tlc2.TLC",
             "-workers", "1", "-config", "check.cfg", module],
            cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    output = result.stdout
    if args.logs:
        (args.logs / f"{label}.log").write_text(output)
    if invariant:
        passed = result.returncode == 12 and f"Invariant {invariant} is violated." in output
        outcome = f"counterexample to {invariant}"
    else:
        passed = result.returncode == 0 and "Model checking completed. No error has been found." in output
        outcome = "all invariants hold"
    if not passed:
        raise SystemExit(f"{label} expected {outcome}, got exit {result.returncode}\n{output}")
    states = next(line for line in output.splitlines() if "distinct states found" in line)
    print(f"{label}  {outcome}\n  {states}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jar", type=Path, help="path to tla2tools.jar")
    parser.add_argument("--java", default="java", help="Java executable")
    parser.add_argument("--logs", type=Path, help="save full TLC output and counterexamples here")
    args = parser.parse_args()
    args.jar = args.jar.resolve(strict=True)
    if args.logs:
        args.logs.mkdir(parents=True, exist_ok=True)
    for module, (faults, witnesses) in CHECKS.items():
        check(args, module)
        for mode, invariant in faults.items():
            check(args, module, mode, invariant)
        for witness in witnesses:
            check(args, module, invariant=witness)


if __name__ == "__main__":
    main()
