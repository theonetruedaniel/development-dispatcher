# Synthetic controller qualification fixtures

Run from the repository or a newly generated standalone export:

```powershell
python -m scripts.dispatch qualify-writes --output write-report.json
```

The output must not exist. The command retains `write-report.json.fixtures/`
with an aggregate report, exclusive per-probe receipts, nine separate fixture
directories and their controller journals. Existing reports and fixture directories are never reused
or overwritten. Interrupted pending reports are incomplete, not passing;
inspect them before any new run. No cleanup or replay command is provided.

`cases.json` freezes the expected operation statuses, file contents and retained-lock state
before execution. The runner compares actual controller observations with those
expectations. All nine must pass for exit code 0. Failure or uncertainty returns
2. A successful report still says `live_writes: false`, `dispatched: false` and
`adoption_qualified: false`.

The cases exercise integration plus inspection-only recovery, verification
denial, conflicting user edits, Stop before application, interrupted partial
application, user edits before recovery, missing command termination evidence,
manifest scope rejection and the unchanged live admission denial.

Only controller-owned synthetic bytes are touched. Copies and verifier receipts
are constructed fixtures, not the output of Git preparation, an executing model
or a contained verifier. There are no subprocess or network calls. Protected
sentinels prove preservation during these controller probes, not denial of
actual worker reads. Descendant termination, current-build tool mediation,
network containment, full production lifecycle and accepted model routes remain
explicitly unrun live probes. This is not completion of all Task 5 requirements.

The function `qualify_writes({'mode': 'synthetic'}, storage)` creates a previously
absent storage directory and persists `storage/result.json`. The CLI additionally
reserves the requested output before starting any fixture. Each result update
is flushed and fsynced. Each completed observation is first saved and fsynced
to its own exclusively created file under `receipts/`; later aggregate updates
never rewrite those receipts. An abrupt failure during an aggregate rewrite can
leave that report incomplete, while prior completed receipts remain intact.
Inspect retained receipts, journals and bytes; an incomplete run is not passed.
