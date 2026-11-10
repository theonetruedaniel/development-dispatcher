# Two-minute offline walkthrough

Use Python 3.13 or later. Open a terminal at the exported repository root and run:

```powershell
python -m scripts.dispatch.demo
```

The command creates synthetic manifests in a temporary directory, feeds each
through the existing `preview` CLI, checks the result, and removes its temporary
fixtures. It does not read account details, generate task answers, start a model
or leave a background process. Model names come from a synthetic catalog, not
live account discovery. Real usage remains unknown.

| Show | Explain |
|---|---|
| Routine task: Luna / low | The task class has a modest default route. Classification is explicitly supplied, not inferred from task wording. |
| Consequential review: Astra / high | The policy preserves a stronger review floor. This is a routing rule, not proof of model quality. |
| Next routine task: Luna / low | A prior difficult task does not permanently raise the next task's route. |
| Manual selection: Sol / high | A deliberate operator selection takes precedence. |
| Same Manual route absent: blocked | The controller explains an unavailable selection rather than silently substituting. |

Expected final result: `5/5 checks passed`. A policy action named `dispatch`
means that the route would be eligible at this preview stage. Every result must
still contain `dispatched: false`; no synthetic task is actually performed.

For the raw result behind the first example:

```powershell
python -m scripts.dispatch preview --manifest tests/dispatch/fixtures/routine.json --catalog tests/dispatch/fixtures/catalog.json
```

For a short verification:

```powershell
python -m unittest discover -s tests/dispatch -p test_demo.py -v
```

These tests check the five expected decisions, reject incorrect preview results,
and make network/socket or child-process creation fail during the demo test.
That guard establishes this test's scope; it is not live tool-isolation admission.

A concise project explanation:

> I'm building Abrams Intelligence as a general-purpose personal intelligence
> platform. This working slice makes automation decisions visible: routine work
> gets a lighter route, consequential work gets a stronger review, and explicit
> choices and limits remain in control. I used deterministic checks before
> enabling live execution. The demo proves the routing workflow; it doesn't
> claim production savings or a finished platform.

For an operations discussion, connect it to explicit handoffs, exception queues
and repeatable acceptance checks. A future business workflow would still need
its own quality baseline, permissions and measured outcomes.

If Python is missing, do not improvise an installation during a demonstration.
Keep a previously captured transcript available and label it as recorded evidence.
An unexpected failure should remain visible; do not change admission flags to
make a live call. The canonical roadmap and its security/resource gates govern
what comes next.
