"""Installer preflight for the checkout that will run the conductor."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coordlib.cli import UsageError, checkout_repo
from coordlib.gh import GitHubError


if __name__ == "__main__":
    try:
        checkout_repo(sys.argv[1], sys.argv[2])
    except (GitHubError, UsageError) as error:
        sys.exit(f"conductor install: {error}")
