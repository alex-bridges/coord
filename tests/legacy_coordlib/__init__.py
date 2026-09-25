"""A frozen copy of coord's budget reader and writer from before #1035 (main at 3c2260424:
coordlib/cache.py and coordlib/budget.py, verbatim).

Every worktree on a Mac shares one budget.json, and worktrees on older commits keep
running this code after #1035 merges. test_budget_principals.LegacyWriterTests writes the
same file with this copy and with the current coordlib, turn about, to prove the two can
share it. Do not update these files to match coordlib: they are the old writer on purpose.
"""
