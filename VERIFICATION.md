# Current standalone verification

November 10, 2026: the final publication candidate ran **304 tests: 281 passed, 23 expected skips**, with zero failures/errors in 34.564 seconds. The earlier source and clean export each passed 303 tests; one further regression verifies completed review text is retained and reused without redispatch. The same reviewed source was used for two real bounded Abrams build reviews, without native build execution.

A targeted static review verified conservative post-launch uncertainty, provider-history blocking, exact-model admission, malformed usage rejection and the Claude/Codex reconciliation separation. These checks are not a full security audit.

See [current live scope](CURRENT.md) for the narrow machine-specific official-client checks. The distributed export omits live pins and account evidence, so it does not grant live admission to a clone. No live WSL or file-editing qualification was run. No quality or usage-saving benchmark is claimed.

## Historical offline record

# Offline verification, October 4, 2026

Tested on Windows with Python 3.13.14, using the standard library and the
already-installed Git for synthetic workspace tests. No dependencies were
installed. No live qualification opt-in was enabled.

| Check | Result |
|---|---|
| `python -m scripts.dispatch.demo` | Five expected routing scenarios passed; every result reports no dispatch. |
| `python -m unittest discover -s tests/dispatch -p test_demo.py -v` | Three tests passed, including no-network/no-child-process guards and a wrong-result failure check. |
| `python -m unittest discover -s tests/dispatch -v` | 250 tests discovered: 227 passed, 23 skipped, zero failures/errors in the source checkout. |
| Standalone export smoke | The demo and its three focused tests also passed from the exported directory. |

The full dispatcher run took about 33 seconds on this host. Skips include
Linux-only and explicit Windows/WSL live qualification tests. They are not
passed tests or containment evidence. This is the full default dispatcher
suite, not full-platform coverage or a production/model evaluation.

The three demo tests were first run without the new module and failed on its
missing import, then passed after implementation. A deliberately incorrect
preview is independently verified to make the demo return failure.

The export manifest lists the exact included files and SHA-256 digests. It
excludes the parent project's Git history, account/configuration files, grants,
journals and admission pins. The exporter uses an explicit file selection and
a limited marker scan, not a complete secret detector or security audit.

Current-build live dispatch, complete tool mediation, Laya admission, measured
business outcomes and platform phase advancement remain unproven by these tests.
