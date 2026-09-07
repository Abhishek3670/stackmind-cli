# Incident Report: Source Code Loss (2026-05-20)

**Agent:** Local-LLM  
**Role:** GitOps & Version Maintainer  
**Date:** 2026-05-20T19:14:00+05:30  
**Incident Time:** 2026-05-20T17:14:00+05:30

---

## 1. Summary

During execution of a P0 directive to purge a large file from git history, I inadvertently wiped all repository commits using `git filter-repo`, resulting in complete loss of project history.

---

## 2. Timeline of Events

### 17:00 - Received Directive

Claude requested purging `rf-detr-base.pth.os0nv4i5.tmp` (299 MB) from git history to enable push to GitHub.

### 17:14 - First Attempt: `git filter-repo --path`

```bash
git filter-repo --force --path rf-detr-base.pth.os0nv4i5.tmp --prune-empty
```

**Result:** Error - `--prune-empty` expected argument

### 17:14 - Second Attempt: Basic filter-repo

```bash
git filter-repo --force --path rf-detr-base.pth.os0nv4i5.tmp
```

**Result:** Success message, but file still appeared in `git rev-list --objects --all`

### 17:17 - Verification showed file still present

```bash
git rev-list --objects --all | Select-String rf-detr
# Output: d088a3516d626cac76bfbeb711af1ae63c8d0e9e rf-detr-base.pth.os0nv4i5.tmp
```

### 17:17 - Attempted cleanup commands

```bash
git reflog expire --expire=now --all
git gc --prune=now --aggressive
```

**Result:** File still persisted

### 17:17 - Third Attempt: `--invert-paths`

```bash
git filter-repo --force --invert-paths --path rf-detr-base.pth.os0nv4i5.tmp
```

**Output:**
```
Parsed 1 commits
Parsed 2 commits
New history written in 0.80 seconds
Repacking your repo and cleaning out old unneeded objects
Completely finished after 3.11 seconds.
```

**Critical observation missed:** Only "2 commits" were parsed, indicating history was already compromised.

### 17:18 - Verified file removed

```bash
git rev-list --objects --all | Select-String rf-detr
# Output: (empty - file removed)
```

### 17:18 - Added remote back

```bash
git remote add origin https://github.com/Abhishek3670/ai-cctv.git
```

### ~17:28 - Discovery of History Loss

When attempting `git log --oneline`:
```
fatal: your current branch 'main' does not have any commits yet
```

---

## 3. Root Cause Analysis

### What Went Wrong

1. **First `git filter-repo` run** - The initial `--path` option without `--invert-paths` may have corrupted or truncated the commit history.

2. **Missed warning signs** - The output "Parsed 2 commits" should have alerted me that history was already lost. A healthy repo would show 100+ commits.

3. **`--invert-paths` behavior** - When run on an already-corrupted repo, it finalized the damage by creating a new history with only the remaining files.

4. **No backup** - I did not create a backup of `.git/` before running destructive operations.

### Why I Missed It

- I was focused on verifying the file was removed, not on verifying total commit count
- The commands appeared to succeed (no errors)
- I assumed `filter-repo` would preserve history while removing only the specified file

---

## 4. Commands Executed (Full List)

| Time | Command | Result |
|------|---------|--------|
| 17:14 | `git filter-repo --force --path rf-detr-base.pth.os0nv4i5.tmp --prune-empty` | Error: argument expected |
| 17:14 | `git filter-repo --force --path rf-detr-base.pth.os0nv4i5.tmp` | Success (file still present) |
| 17:17 | `git rev-list --objects --all \| Select-String rf-detr` | File still in objects |
| 17:17 | `git reflog expire --expire=now --all` | Success |
| 17:17 | `git gc --prune=now --aggressive` | Success |
| 17:17 | `git filter-repo --force --invert-paths --path rf-detr-base.pth.os0nv4i5.tmp` | Success (history lost) |
| 17:18 | `git rev-list --objects --all \| Select-String rf-detr` | File removed |
| 17:18 | `git remote add origin https://github.com/Abhishek3670/ai-cctv.git` | Success |

---

## 5. Lessons Learned

### Process Failures

1. **No pre-destructive backup** - Should have copied `.git/` folder before any `filter-repo` operation
2. **No verification of commit count** - Should have run `git log --oneline | wc -l` before and after
3. **Blind trust in tool success** - Commands returning "success" doesn't mean intended outcome

### Tool Understanding Gaps

1. **`git filter-repo --path`** - Removes files but can corrupt history if not used correctly
2. **`--invert-paths`** - Means "remove these paths, keep everything else" - but on a corrupted repo, there's nothing to keep
3. **Multiple runs** - Running filter-repo multiple times compounds damage

### What I Should Have Done

```bash
# 1. Create backup
cp -r .git .git-backup

# 2. Verify current state
git log --oneline | wc -l  # Should show 100+

# 3. Run filter-repo ONCE with correct syntax
git filter-repo --invert-paths --path rf-detr-base.pth.os0nv4i5.tmp

# 4. Verify expected outcome
git log --oneline | wc -l  # Should still show 100+
git rev-list --objects --all | grep rf-detr  # Should be empty

# 5. If wrong, restore from backup
# rm -rf .git && mv .git-backup .git
```

---

## 6. Prevention Measures

### For Future Operations

1. **Always backup `.git/` before destructive git operations**
2. **Verify commit count before and after**
3. **Test on a clone first**
4. **Document the exact command before running**
5. **If multiple attempts needed, restore from backup between attempts**

### Recommended Protocol Addition

Add to `.sync/agents/local-llm.agent.md`:

```markdown
### Destructive Git Operations Protocol

Before running `git filter-repo`, `git filter-branch`, or similar:

1. Create backup: `cp -r .git .git-backup-$(date +%Y%m%d-%H%M%S)`
2. Record commit count: `git log --oneline | wc -l`
3. Run operation
4. Verify commit count matches expected
5. If mismatch, restore from backup immediately
```

---

## 7. Impact Assessment

- **Code:** All source files intact in working directory
- **History:** Complete loss of ~135 commits
- **Recovery:** Files can be re-committed as "Initial commit"
- **Data Loss:** Git blame, commit history, authorship information lost

---

**Local-LLM — GitOps & Version Maintainer**
