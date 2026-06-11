# Claude Code instructions — burnrate (Tourniquet)

Public Anthropic-API spend-cap proxy, **deployed at tourniquet.dev** — this is production code with real users. Read [README.md](README.md) for architecture; [HANDOFF.md](HANDOFF.md) for live session state.

## Rules

- Workspace rules apply ([../CLAUDE.md](../CLAUDE.md)): model selection per step, token discipline, ERRORS.md / FEATURE_REQUESTS.md logs.
- Model overlay: this project touches a security boundary (API-key proxying) and production data — escalate to **Opus 4.7** for proxy/router/auth changes; **Sonnet 4.6** for everything else; Haiku only for docs/formatting.
- **Commit before claiming shipped.** This project's ERRORS.md twice recorded fixes as "shipped" while the files sat uncommitted (see 2026-06-09 entry). A fix is shipped when it is committed AND deployed to tourniquet.dev, not before.
- Never commit `.env` (gitignored; contains live keys). Deployment must reach the target system, not stop at local artifacts.
