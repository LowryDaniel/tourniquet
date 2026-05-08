## Summary

<!-- One or two sentences on what this changes and why. The "why" is
what reviewers care about — the diff already shows the "what". -->

## Test plan

- [ ] `pytest -q` passes locally
- [ ] If touching the proxy hot path: smoke-tested with a real `/v1/messages` call
- [ ] If touching the dashboard: opened the affected page and confirmed it renders + behaves
- [ ] If adding a new alert channel or callback: ran `tourniquet test-alerts --key <real-key>` end-to-end

## Risk

<!-- What could break, and what's the rollback story?
"None — the change is additive" is a fine answer when true. -->

## Linked issue

Closes #
