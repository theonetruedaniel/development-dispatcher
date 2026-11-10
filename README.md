# Development Dispatcher

An experimental Python controller that assigns bounded development tasks to an explicit model and reasoning level, then checks the result. It provides deterministic routing, manual overrides, usage-aware deferral, verification, cancellation and auditable recovery.

Originally built alongside [Abrams Intelligence](https://github.com/theonetruedaniel/abrams-intelligence). This repository is the standalone development tool, not the future product router.

## Current status

The offline demo and synthetic test suite run without an account, API key, network connection or model calls. Python 3.13+ and Git are needed for the full workspace tests.

On November 10, 2026, the maintainer's exact local official clients passed bounded supplied-text read-only checks for GPT-6 Luna, Sol and Astra; GPT-6.1 Sol and Terra; and Claude Opus, Sonnet and Haiku 5.5. That machine-specific qualification is **not transferred by cloning this repository**. Live executable pins, account evidence, grants and journals are intentionally excluded. A fresh checkout blocks live execution.

Direct model-driven file editing and Laya selection remain unqualified. The controller does not switch an existing conversation, change an app's model picker or intercept every task. No measured quality improvement, cost saving or production-readiness claim is made.

## Try the offline demo

```sh
python -m scripts.dispatch.demo
python -m unittest discover -s tests/dispatch -p test_demo.py -v
python -m unittest discover -s tests/dispatch -v
```

Expected demo result: five routing checks pass with no model dispatch. The final standalone suite ran 304 tests: 281 passed and 23 platform-specific or opt-in live tests were skipped. See [verification](VERIFICATION.md), [the demo walkthrough](DEMO.md), [evaluation boundaries](EVALUATION.md), and [current signed-in CLI scope](CURRENT.md).

For an individual preview:

```sh
python -m scripts.dispatch preview --manifest tests/dispatch/fixtures/routine.json --catalog tests/dispatch/fixtures/catalog.json
```

The supplied catalog is a fixture, not live availability or subscription entitlement.

## How routing works

- Codex Auto prefers supported current catalog IDs: Luna for routine work, GPT-6.1 Terra for implementation, GPT-6.1 Sol for analysis, and Astra for difficult or consequential work. Exact Manual model/effort pins take precedence; quality floors and allowance gates remain.
- A task-boundary selector can recommend a qualified Claude route for independent review: Haiku/low, Sonnet/medium or Opus/high according to task class. The coordinator prepares the exact authorized execution scope. There is not a single unified automatic executor across both providers.
- Normal Codex work uses the official signed-in App Server. Claude uses the unmodified official CLI and the user's own subscription. Neither path extracts or proxies credentials.
- Tasks are bounded, serial and journaled. Supplied-text work has no project file tools. The coordinator reviews the output and applies authorized edits separately.
- Unknown billing eligibility, changed executables, unsupported routes and insufficient allowance block or defer work. There is no API-key fallback, account rotation, capacity purchase or usage-limit bypass.

## Trust and safety boundaries

Authorization records bind an exact task, workspace, account, actions, attempts and expiry. A JSON file does not create user permission. Source material must already be authorized to share with the selected provider.

Claude execution requires a fresh account-matched observation that usage credits are disabled, sufficient included allowance, and a pinned official client. Fast mode, hooks, file tools, MCP, Chrome, slash commands and alternate billing environment overrides are disabled. Estimated cost reported by the CLI is not a payment receipt. Fable is disabled.

Failed or uncertain delivery is not silently replayed through another provider. Stop records uncertainty if remote completion cannot be established. Keep journals and reconcile exact outcomes rather than removing safety markers to retry.

The repository also includes offline experimental editing, verification and WSL containment modules. They are not live admitted. Passing fixtures does not prove complete native-tool mediation; changing a boolean or copying a pin cannot supply missing qualification.

## Scope and maintenance

This is a standard-library Python research/development tool. No dependencies, model weights, personal configuration, credentials or parent-project Git history are bundled. The explicit export manifest records source digests. External services and supported model IDs can change; requalification is required when the exact client/protocol changes.

The future Abrams product router remains provider-neutral and separately gated. Publishing this tool does not admit product routes or complete product phases.
