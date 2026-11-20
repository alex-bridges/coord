# TLA+ contract checks

Run the models with Python 3.9+ and Java 21. Download the official
[TLA+ tools v1.7.4 release](https://github.com/tlaplus/tlaplus/releases/tag/v1.7.4),
then run this command from the repository root.

```sh
python3 formal/check.py /path/to/tla2tools.jar --logs /tmp/coord-tlc-logs
```

The runner explores every reachable state in each finite model. It also runs faulty
variants and requires TLC to return a counterexample for the named invariant. Separate
reachability checks use the normal model and ask TLC for a trace to a specific outcome.
Full traces are available in the optional logs directory and the `tlc-logs` CI artifact.
CI pins the tool version and verifies its SHA-256 before execution.

### Merge train

The train may update the default branch only to the immutable commit it checked. The
model checks the trusted build record, CI result, first-parent chain, merge trees, batch
size, PR eligibility at the final read, and whether the ref update can fast-forward.
The candidate branch and the default branch can move between observation and update.
The eligibility read and ref update are separate operations in GitHub. PR labels and
heads can change after that read.

The fixtures include valid one-, two- and three-member batches, an empty batch, a
four-member batch, a bad intermediate tree, a parent with the right tree but the
wrong SHA, and an inconsistent member count. Disjoint file additions make set union
the expected merge tree. The Python tests use a local Git remote and the real
`git merge-tree` command. CI dispatch, retry scheduling, conflict resolution and
eventual delivery are outside the model. [Model](MergeTrain.tla) ·
[Implementation](../coordlib/train_step.py) · [Tests](../tests/test_train.py)

### macOS Keychain

Two imports address the same Keychain, service and account. The model varies the
target and an occupied neighboring slot across two Keychains, two services and two
accounts. It checks creation, replacement, exact readback, preservation of other
items and failure before a write. An unusable native reference can send creation to
the default Keychain. Removing the status check exposes that behavior.

The target's availability can change between calls and stays fixed during each
call. The model assumes atomic native writes and abstracts payload bytes as two
values. macOS tests use a real temporary Keychain and an independent reader. The
invalid-target test intercepts item creation to protect the user's default Keychain.
Concurrent file removal, access prompts, ACLs and encryption are outside the model.
[Model](CredentialStore.tla) · [Implementation](../coordlib/macos_keychain.py) ·
[Tests](../tests/test_macos_keychain.py)

### Starter guard

The guard receives one of 20 requests under a conductor, reviewer or other identity,
each with the wake flag on or off. It checks all 120 decisions against an
independent table. Examples include malformed requests, role-specific Bash decisions,
protected files and directory descendants, similar filenames, and edits inside and
outside the review directory.

The model begins with a classified command and resolved path components. The Python
tests run the shipped hook on concrete JSON through stdin and assert the exit status.
Shell parsing, filesystem resolution and the behavior of tools after the hook returns
are outside the model. [Model](StarterGuard.tla) · [Implementation](../examples/conductor-guard.py) ·
[Tests](../tests/test_starter_guard.py)
