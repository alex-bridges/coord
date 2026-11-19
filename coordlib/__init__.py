"""coord: shared awareness for agents working in a GitHub repository.

The board reads GitHub state. Claims, releases and messages use comments with a
machine marker. The optional merge train manages branches, labels and CI runs,
and the conductor keeps its work queue and run history on the host.
"""

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CONFLICT = 2
EXIT_BACKEND = 3
EXIT_NOT_FOUND = 4
EXIT_BUDGET = 5  # the GitHub API budget guard refused (budget.py)
