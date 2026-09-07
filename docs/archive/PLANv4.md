# StackMind PLANv4: The Dual-Repo Architecture

## 1. Executive Summary

As StackMind agents generate increasingly complex Work Orders, Contracts, and Knowledge Graphs, the `.sync/` directory becomes a dense, high-frequency ledger. In a traditional single-repo setup, this creates noise in the project's Git history and clutters Pull Requests with agent metadata.

**PLANv4 proposes a Dual-Repo Architecture.** 
The main repository will strictly track application source code, while the `.sync/` folder will be managed as an independent, parallel Git repository. A cryptographic "Bridge" (storing the Code Repo's commit ID inside the Agent Repo's state) will link the two timelines.

---

## 2. The Architecture

### A. The Code Repo (Public/Main)
- **Contents**: Pure application source code + `AGENTS.md`.
- **Visibility**: Pushed to GitHub/GitLab. Reviewed by humans.
- **Rule**: `.sync/` is explicitly added to `.gitignore`.
- **The Constitution**: `AGENTS.md` remains in the root to ensure humans and agents always read the governance rules immediately upon cloning.

### B. The Agent Repo (Private/Ledger)
- **Contents**: The `.sync/` directory (Work Orders, Contracts, Runtime Snapshots, Knowledge Graph).
- **Visibility**: Maintained as an independent Git repository (`.sync/.git`). Can be pushed to a separate remote or kept entirely local.
- **Rule**: Never pollutes the main Code Repo.

### C. The Bridge Mechanism
To maintain the relationship between a specific Work Order and the code it produced, StackMind will enforce a pointer system.
When a Work Order is marked as `COMPLETED`:
1. The GitOps agent commits the code to the Code Repo and captures the new `Commit ID` (e.g., `abc1234`).
2. The agent writes this `Commit ID` into the Work Order's metadata (e.g., `target_commit: abc1234`) inside `.sync/`.
3. The agent commits the updated `.sync/` state to the Agent Repo.

---

## 3. Tooling Requirements

To prevent UX friction, standard Git commands can no longer be used for time-travel, as `git checkout` in the Code Repo would leave the Agent Repo stranded in the future.

We will introduce **Stateful Orchestration Commands**:
- `stackmind checkout <commit>`: 
  1. Checks out the target commit in the Code Repo.
  2. Parses the Agent Repo history to find the state where `target_commit == <commit>`.
  3. Checks out the Agent Repo to that exact synchronized state.

---

## 4. Trade-offs and Risks

While this architecture guarantees a pristine Code Repo, it introduces several complex risks that must be managed:

### Risk 1: Transactional Safety (Split-Brain)
In a single repo, code and state are committed in the same millisecond. In a dual-repo setup, commits happen sequentially. If a system crash or network failure occurs between the Code Repo commit and the Agent Repo commit, the system enters a "split-brain" state.
*Mitigation*: The GitOps agent must implement robust rollback logic to `git revert` the Code Repo if the Agent Repo commit fails.

### Risk 2: Breaking the PR Audit Trail
Currently, when an agent opens a Pull Request on GitHub, reviewers see the code changes alongside the Work Order and the Contract that governed it. If `.sync/` is in a separate repo (or kept local), human reviewers lose the context of *why* the AI made the change and what scope it was given.
*Mitigation*: The GitOps agent could be configured to post a summary of the Work Order as a PR comment on GitHub, restoring visibility without cluttering the Git tree.

### Risk 3: Developer Onboarding Friction
If the Agent Repo is pushed to a remote server so the team can share AI memory, new developers will have to clone *two* repositories and link them correctly before they can start working.
*Mitigation*: Update `stackmind init` to handle cloning and linking remote `.sync/` repositories automatically.
