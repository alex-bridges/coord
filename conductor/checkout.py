"""Check the conductor installer's origin or update its detached checkout."""
from functools import partial
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coordlib import config
from coordlib.cli import UsageError, checkout_repo
from coordlib.conductor import refresh_checkout, run_git
from coordlib.gh import GitHubError


if __name__ == "__main__":
    args = sys.argv[1:]
    try:
        if len(args) == 3 and args[0] == "verify":
            checkout_repo(args[1], args[2])
        elif len(args) == 2 and args[0] == "update":
            checkout = args[1]
            branch = config.get("default_branch", start=checkout)
            error = refresh_checkout(partial(run_git, checkout), branch)
            if error:
                sys.exit(error)
        else:
            sys.exit("usage: checkout.py verify CHECKOUT REPO | checkout.py update CHECKOUT")
    except (GitHubError, UsageError, config.ConfigError) as error:
        sys.exit(f"conductor install: {error}")
