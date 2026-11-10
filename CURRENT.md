# Current signed-in CLI scope

Updated November 10, 2026.

## Implemented

- Catalog-aware GPT 6/6.1 model/effort selection and exact Manual overrides.
- Pinned official Codex client with live account, catalog and included-usage checks.
- One-turn supplied-text Claude adapter with account-bound credits-disabled evidence, explicit model/effort, disabled tools/customizations and bounded process/output handling.
- Task-boundary provider recommendation for independent review, without automatic cross-provider replay.
- Shared workspace exclusion, authorization checks, deterministic final-result checks and conservative uncertainty reporting.

## Observed on the maintainer's qualified machine

Codex 0.167.0-alpha.3.2 accepted and completed bounded checks for GPT-6 Luna/low, GPT-6.1 Terra/medium, GPT-6.1 Sol/high, GPT-6 Astra/high and GPT-6 Sol/medium. A post-fix Stop probe confirmed interruption without redispatch.

Claude Code 2.1.316 accepted and completed one-turn tool-free checks for Opus 5.5/high, Sonnet 5.5/medium and Haiku 5.5/low using the maintainer's existing personal subscription. Fresh account-matched UI observations established usage credits OFF before these calls. No paid API use or purchase was authorized. Effort was requested explicitly; independent backend effort metadata was not observed.

A real supplied-code Claude review was also run. It withheld an unconditional pass because the excerpt omitted supporting helper context. The strict exact-result check failed and was recorded as failed, not hidden or retried through another model. The context was inspected separately.

The dispatcher was then used for two actual, serial Abrams build reviews: a consequential boundary review on GPT-6 Astra/high and a finite build-correction review on GPT-6.1 Sol/high. Both were supplied-text reviews with source-context integrity checks, not native acceptance runs. Completed Codex review text is retained for the coordinator and reused without redispatch.

These are narrow integration checks, not model-quality benchmarks or universal entitlement promises.

## Requirements for any future live use

A separately qualified exact executable, authenticated authorized account, live supported route, sufficient allowance and exact user task authorization are required. Claude additionally needs account-matched credits-disabled evidence no older than five minutes. Missing, stale or null data blocks. Neither a catalog entry nor a subscription label alone proves a no-charge route.

Machine-specific pins and account proof are not distributed in this repository. Do not create a qualification flag merely to bypass the guard. The standalone checkout is useful for offline demonstration and tests without live credentials.

## Not admitted

Direct file-editing isolation, Laya routing, Fable, arbitrary tools, app-wide switching, hosted models, credential proxying, paid API fallback and unattended provider failover. This is development tooling, not the separate Abrams product router.

## References

- [Codex authentication](https://learn.chatgpt.com/docs/auth) and [noninteractive mode](https://learn.chatgpt.com/docs/non-interactive-mode).
- [Claude noninteractive use](https://code.claude.com/docs/en/headless), [authentication](https://code.claude.com/docs/en/authentication), [usage credits](https://support.claude.com/en/articles/12429409-manage-usage-credits-for-paid-claude-plans) and [model configuration](https://code.claude.com/docs/en/model-config).
