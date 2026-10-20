# Project instructions

The next release is `0.3.0`, including Codex and bounded local Docker execution.
The package owns one Claude Code subprocess attempt and optional gateway processes.
It also owns optional Kubernetes worker containers. Follow docs/kubernetes-design.md.
Keep local and remote ClaudeClient.run signatures identical.
Applications own authorization, provider selection, accounting, retries, and audit persistence.
Keep `session_id` and `resume` as separate keyword parameters.
Keep Claude and Codex APIs similar without pretending their CLI options are identical.
Keep local and remote CodexClient.run signatures identical.
Run local Docker tests sequentially, with explicit CPU, memory and PID limits.
Do not import application modules or add application identity fields to results.
Keep credentials out of representations and public fixtures.
Never publish private repository references.
Update public documentation and examples with interface changes.
Run the checks and artifact smoke in CONTRIBUTING.md before release.
Tags identify tested commits on the public main branch. Published versions are immutable.
