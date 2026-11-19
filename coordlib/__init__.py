"""coord: shared awareness for agents working in a GitHub repository.

Everything here reads GitHub state and computes a picture from it. The only
writes are ordinary GitHub comments carrying a machine marker (see marker.py):
claims, releases and messages. Nothing is locked and nothing is stored outside
GitHub except a short-lived local cache (and, in app mode, the GitHub App's tokens:
apptoken.py).
"""

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CONFLICT = 2
EXIT_BACKEND = 3
EXIT_NOT_FOUND = 4
EXIT_BUDGET = 5  # the GitHub API budget guard refused (budget.py)
