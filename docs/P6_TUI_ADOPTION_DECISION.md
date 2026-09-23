# P6 TUI adoption decision

**Decision:** Select OpenCode as the reference TUI foundation and use **Strategy C**: reuse compatible presentation concepts/components behind the StackMind adapter.

StackMind does not fork, embed, or execute OpenCode's agent runtime. Its native tools, provider calls, workspace handling, and permissions stay unmanaged and are excluded from governed sessions. The implemented Python TUI surface is replaceable, dependency-light, and talks exclusively to the existing LocalDaemon.

This preserves OpenCode's MIT-friendly adoption path while retaining StackMind as the sole authority for sessions, contracts, policy, sandboxing, verification, evidence, and learning eligibility. Any upstream component adoption requires dependency-license review and attribution at the time it is vendored or linked.
