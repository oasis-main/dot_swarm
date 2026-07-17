# State — dot_swarm

**Last touched**: 2026-07-17T15:43Z by claude-code
**Current focus**: Per-agent Ed25519 identity — the real "message signing" primitive
**Active items**: SWC-048
**Blockers**: PyPI Trusted Publishing not yet configured (manual step for SWC-003)
**Ready for pickup**: SWC-003, SWC-004, SWC-005, SWC-006, SWC-007, SWC-008, SWC-009, SWC-021, SWC-024, SWC-032, SWC-033, SWC-043, SWC-045

---

## Handoff Note

**2026-04-25 (claude-code — README reorg + logo refresh):**

Single commit on `main`: `fd1a657 Reorganize README: add orchestrator bottleneck
insight, move command reference after quick start, improve multi-agent and
staleness docs`.

Changes:
- `README.md` (−49 / +36): reordered so quick-start leads, command reference
  follows; added an "orchestrator bottleneck" insight paragraph explaining
  why single-thread orchestration is the limiting factor for multi-agent
  workflows; tightened the staleness / drift discussion.
- `docs/index.md` (1 line): minor copy nudge to match README.
- Logo assets regenerated from `oasis-x/portfolio/fluid-swarm-sim/gen_logo.py`
  (that repo is the canonical source — see its `.swarm/context.md`). Dropped
  the black/white variants entirely; kept a single `icon.gif` (340 KB → 486 KB,
  higher frame count) and `logo.gif` (8.9 MB → 353 KB, re-encoded with better
  compression). Removed `icon_black.gif`, `icon_white.gif`, `logo_black.gif`,
  `logo_white.gif` from tracking.
- `.gitignore` (−4): pruned rules that matched the removed variants.

**No queue changes this session.** SWC-033 (append-only claims), SWC-045 (v1
readiness audit), and the distribution chain (SWC-003→006) remain the top of
the backlog.

**Forward note**: fluid-swarm-sim is now an oasis-x portfolio project with a
larger roadmap (self-serve swarm programmer + traffic simulation tool).
dot_swarm still consumes the generated GIFs, but the engine code is
definitively not a dot_swarm subproject anymore. Track its `.swarm/` there.

---

## Handoff Note

**Task Integration & v1.0 Foundation (2026-04-20)**:
- Mapped v1.0 targets (SWC-043, SWC-044, SWC-045) in queue.md.
- Explored `oasis-cloud` directory structure for upcoming todo list integration (SWC-043).
- Refined Mermaid protocol diagrams in README.
