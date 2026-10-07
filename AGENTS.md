# Project instructions

Keep releases on `0.2.*` until the user changes this constraint.
The package owns one Claude Code subprocess attempt and optional gateway processes.
It also owns optional Kubernetes worker containers. Follow docs/kubernetes-design.md.
Keep local and remote ClaudeClient.run signatures identical.
Applications own authorization, provider selection, accounting, retries, and audit persistence.
Keep `session_id` and `resume` as separate keyword parameters.
Codex and other agents are future features. Do not add a shared agent abstraction until requested.
Do not import application modules or add application identity fields to results.
Keep credentials out of representations and public fixtures.
Never publish private repository references.
Update public documentation and examples with interface changes.
Run the checks and artifact smoke in CONTRIBUTING.md before release.
Tags identify tested commits on the public main branch. Published versions are immutable.
