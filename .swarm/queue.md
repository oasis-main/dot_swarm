# Queue — dot_swarm (Division Level)

Items are listed in priority order within each section.
Item IDs: `<DIVISION-CODE>-<3-digit-number>` — assigned sequentially, never reused.

---

## Active

## Pending

- [ ] [SWC-048] [OPEN] Per-agent Ed25519 identity — the real "message signing" primitive
      priority: critical | project: misc
      notes: Current signing.py is HMAC-SHA256 with ONE SHARED KEY PER SWARM
      depends: SWC-047

- [ ] [SWC-049] [OPEN] MCP server authentication — bind identity to the process, not the call
      priority: critical | project: misc
      notes: call_tool() currently trusts whatever `agent_id` string the CALLER
      depends: SWC-048

- [ ] [SWC-013] [OPEN] Phase 2: schedules.md + workflow composition (cron-based)
      priority: high | project: misc
      notes: scheduler.py: Schedule dataclass, add_schedule(), is_due(), _cron_is_due() (stdlib, no croniter),

- [ ] [SWC-007] [OPEN] Write paper: stigmergy for AI agent coordination → AAMAS / LLM-agents workshop
      priority: high | project: misc
      notes: Novel claim — first filesystem-native stigmergy protocol for multi-agent AI dev teams.

- [ ] [SWC-008] [OPEN] Write paper: seamless swarm looping via trajectory tracking + Hermite splines → SIGGRAPH / Eurographics
      priority: high | project: misc
      notes: Novel pipeline for perfectly seamless animated swarm GIFs / real-time loops:

- [x] [SWC-016] [DONE · 2026-04-18T00:00Z] Fix mkdocs.yml stale SwarmCity → dot_swarm URLs
      priority: high | project: misc
      notes: site_url, repo_url, repo_name all updated; gh-pages branch already exists on MikeHLee/dot_swarm

- [ ] [SWC-017] [OPEN] swarm ready — dependency-aware work discovery (like bd ready)
      priority: high | project: misc
      notes: DONE in this session — lists OPEN pending items with all deps completed.

- [ ] [SWC-018] [OPEN] Role system infrastructure: roles.py + swarm role enable/disable/show/list
      priority: high | project: misc
      notes: DONE in this session — roles.py: RoleConfig, enable_role, disable_role, load_role,

- [ ] [SWC-019] [OPEN] Inspector role — proof-of-work gate, swarm inspect --pass/--fail
      priority: high | project: misc
      notes: DONE in this session — WorkItem gains proof: + inspect_fails: fields.

- [x] [SWC-023] [DONE · 2026-04-19T00:00Z] tmux worker spawning — swarm spawn <id> [--agent opencode|claude|ollama]
      priority: high | project: misc
      notes: Implemented. swarm spawn SWC-042 --agent opencode|claude|ollama|bedrock.

- [x] [SWC-025] [DONE · 2026-04-19T00:00Z] Task-level max_retries + swarm crawl command
      priority: high | project: misc
      notes: WorkItem.max_retries field (0=use role default, >0=task override).

- [x] [SWC-026] [DONE · 2026-07-17T15:32Z] swarm trail visible/invisible — gitignore-based .swarm sharing toggle
      priority: high | project: misc
      notes: Implemented as `swarm trail` group with status/invisible/visible subcommands.

- [x] [SWC-028] [DONE · 2026-07-17T15:32Z] Ollama AI backend integration (alongside Bedrock)
      priority: high | project: misc
      notes: swarm spawn --agent ollama already launches ollama in tmux as a worker tool,

- [x] [SWC-029] [DONE · 2026-07-17T15:32Z] Document opencode+tmux multi-agent workflow end-to-end
      priority: high | project: misc
      notes: Full spawn→claim→implement→proof→inspect→merge flow is implemented but

- [x] [SWC-030] [DONE · 2026-07-17T15:32Z] README + docs: security section overhaul with benefits AND vulnerabilities
      priority: high | project: misc
      notes: Current security section is thin. Expand with:

- [x] [SWC-033] [DONE · 2026-07-17T15:32Z] Implement Conflict-Free Concurrency Mechanism (claims/ directory)
      priority: high | project: misc
      notes: .swarm/claims/ is now a true append-only trail. Every lifecycle op

- [x] [SWC-034] [DONE · 2026-07-17T15:32Z] Mandate MCP Server for Agent Interactions
      priority: high | project: misc
      notes: Expand MCP server implementation. Enforce agent interaction via tools rather than

- [x] [SWC-035] [DONE · 2026-07-17T15:32Z] Add Open Source License (MIT or Apache 2.0) Add Open Source License (MIT or Apache 2.0)
      priority: high | project: misc
      notes: Add LICENSE file to root to unblock corporate and widespread adoption. [REPO-001]

- [x] [SWC-036] [DONE · 2026-07-17T15:32Z] Establish Governance & Contribution Guidelines Establish Governance & Contribution Guidelines
      priority: high | project: misc
      notes: Create CONTRIBUTING.md and .github/ISSUE_TEMPLATE files. [REPO-002]

- [x] [SWC-037] [DONE · 2026-07-17T15:32Z] Expand CI/CD Matrix (Linux, macOS, Windows) Expand CI/CD Matrix (Linux, macOS, Windows)
      priority: high | project: misc
      notes: Ensure GitHub Actions run tests across all major OSes for path handling. [REPO-003]

- [x] [SWC-042] [DONE · 2026-07-17T15:32Z] Fix CI/CD failures and matrix
      priority: high | project: misc
      notes: Remove Python 3.10 (unsupported), add anyio to tests, fix MCP test collection, fix Windows compatibility, and fix state consistency.

- [ ] [SWC-050] [OPEN] Atomic claim() — close the TOCTOU window
      priority: high | project: misc
      notes: claim_item() (operations.py:408) does read_queue() -> check state ->

- [ ] [SWC-051] [OPEN] Comments — threaded discussion on a work item, signed
      priority: high | project: misc
      notes: No structured comment/discussion mechanism exists today — coordination
      depends: SWC-048

- [ ] [SWC-052] [OPEN] Mailbox — direct agent-to-agent messaging within one swarm
      priority: high | project: misc
      notes: federation.py's inbox/outbox is for CROSS-swarm (cross-repo) exchange
      depends: SWC-048

- [ ] [SWC-014] [OPEN] Phase 3: OGP-lite federation layer
      priority: medium | project: misc
      notes: federation.py implemented: trust_peer(), doorman_check() (3-layer),

- [ ] [SWC-015] [OPEN] Phase 4b: StigmergicSwarm contribution to swarms.ai
      priority: medium | project: misc
      notes: swarms_provider.py extended with DotSwarmWorkflow (from_markdown, from_swarm_dir,

- [ ] [SWC-009] [OPEN] Launch BoidRunner: technical blog post → browser simulator / game
      priority: medium | project: misc
      notes: Phased plan —

- [x] [SWC-020] [DONE · 2026-04-19T00:00Z] Watchdog role — escalate stuck items to human when worker+inspector loop
      priority: medium | project: misc
      notes: Subsumed into inspector retry loop. reopen_item() now auto-BLOCKs when inspect_fails

- [ ] [SWC-021] [OPEN] Supervisor role — holistic progress view + human-director briefs
      priority: medium | project: misc
      notes: swarm supervisor report (all active items + phase progress across queue sections),

- [x] [SWC-022] [DONE · 2026-04-19T00:00Z] Librarian role — catalog directory tree into .swarm/ context + queue
      priority: medium | project: misc
      notes: Subsumed into swarm crawl command. crawl_directory() in operations.py walks tree,

- [x] [SWC-027] [DONE · 2026-07-17T15:32Z] Tighten init/crawl/explore coupling
      priority: medium | project: misc
      notes: No redundancy to remove — init creates blank .swarm/, crawl populates context.md

- [ ] [SWC-031] [OPEN] Replace oasis-x specific examples in docs with rich generic examples
      priority: medium | project: misc
      notes: docs/index.md + README.md: claim pattern block → API-042/043/041 (rate limiter,

- [ ] [SWC-032] [OPEN] Move collaboration/integration notes from docs into .swarm trail; make invisible
      priority: medium | project: misc
      notes: SWARMS_AI_PR_GUIDE.md, INTEGRATION_PLAN.md, PLATFORM_SETUP.md contain

- [x] [SWC-038] [DONE · 2026-07-17T15:32Z] Native CI/CD Integration: GitHub Action for audit/heal Native CI/CD Integration: GitHub Action for audit/heal
      priority: medium | project: misc
      notes: Create/publish official Action to ensure protocol files haven't drifted.

- [x] [SWC-039] [DONE · 2026-07-17T15:32Z] Competitive Task Resolution (Parallel Execution)
      priority: medium | project: misc
      notes: Implement intentional duplicate claims [COMPETING] or [REVIEW].

- [x] [SWC-040] [DONE · 2026-07-17T15:32Z] Visual Protocol Diagrams (Mermaid.js)
      priority: medium | project: misc
      notes: Add diagrams to README demonstrating stigmergy feedback loop. [DOCS-001]

- [x] [SWC-041] [DONE · 2026-07-17T15:32Z] Explicitly list system-level prerequisites (Git, tmux) Explicitly list system-level prerequisites (Git, tmux)
      priority: medium | project: misc
      notes: Add to Quick Start/Installation guide to prevent command-not-found errors. [DOCS-002]

- [ ] [SWC-003] [OPEN] Configure Trusted Publishing (OIDC) on PyPI
      priority: medium | project: misc

- [ ] [SWC-004] [OPEN] Tag and publish v0.3.0 to PyPI
      priority: medium | project: misc

- [x] [SWC-005] [DONE · 2026-07-17T15:32Z] Update Homebrew formula for dot-swarm v1.0.0
      priority: medium | project: misc

- [x] [SWC-006] [DONE · 2026-07-17T15:32Z] Submit Homebrew formula to tap
      priority: medium | project: misc

- [ ] [SWC-024] [OPEN] Merge queue (lightweight Refinery) — serialize concurrent branch merges
      priority: low | project: misc
      notes: Gastown's Refinery role manages merge queue to prevent parallel worker collisions.

## Done

- [x] [SWC-012] [DONE · 2026-04-06T14:00Z] Phase 1c: docs/CLI_REFERENCE.md — heal, audit --full, ai --chain, security model, swarms.ai integration
      priority: medium | project: misc

- [x] [SWC-011] [DONE · 2026-04-06T14:00Z] Phase 1b: swarm heal + swarm audit + swarm ai --chain + init identity
      priority: medium | project: misc

- [x] [SWC-010] [DONE · 2026-04-06T14:00Z] Phase 1a: signing.py (HMAC-SHA256) + security.py (18-pattern scanner) + swarms_provider.py (DotSwarmStateProvider + StigmergicSwarm)
      priority: medium | project: misc

- [x] [SWC-001] [DONE · 2026-03-31T15:00Z] GUI for visualizing swarm trails in a GitHub repo
      priority: medium | project: misc

- [x] [SWC-002] [DONE · 2026-03-31T14:30Z] CLI commands `up` and `down` to manage alignment/relation of work items
      priority: medium | project: misc

- [ ] [SWC-043] [OPEN] Integrate dot_swarm into human todo lists (oasis-x)
      priority: high | project: misc
      notes: Integrate with ~/Documents/Runes/oasis-x/oasis-cloud/src/data.

- [x] [SWC-044] [DONE · 2026-07-17T15:32Z] Fix Docs Website (Jekyll rendering issue)
      priority: high | project: misc
      notes: Website is currently falling back to a raw README clone.

- [ ] [SWC-045] [OPEN] v1.0 Readiness Audit: API Stability + Migration Tooling
      priority: high | project: misc
      notes: Ensure stable v0-to-v1 migration path (detect/migrate .swarm/).

- [ ] [SWC-046] [OPEN] Swarm Key: AEAD-encrypted trail + nestmate recognition
      priority: high | project: misc
      notes: Phase 1 SHIPPED — vault.py (ChaCha20-Poly1305 envelopes), .swarm/.swarm_key

- [x] [SWC-047] [DONE · 2026-07-17T15:37Z] Fix broken MCP server — heal ImportError + zero test coverage
      priority: critical | project: misc
      notes: dot_swarm_mcp/server.py:47 does `from dot_swarm.ai_ops import heal` but | Extracted heal logic into ai_ops.heal() (pure fn, no click.echo); fixed the MCP server's real ImportError; added swarm_handoff to list_tools(); fixed test_mcp.py's masking bug so a broken server.py fails loudly instead of silently skipping. 234 tests pass (was 231 passed + 3 masked-skipped).
