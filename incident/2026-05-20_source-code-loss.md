# INCIDENT REPORT — Source Code Loss

**Date:** 2026-05-20  
**Severity:** CRITICAL  
**Status:** RECOVERY IN PROGRESS

---

## Summary

All source code files (`.py`, `.tsx`, `.ts`) were accidentally deleted from the AI-CCTV repository during a git cleanup operation.

## Timeline

| Time | Event |
|------|-------|
| 15:17 | WO-018 created for maintenance fixes |
| 15:40 | WO-018 completed by Gemini, approved by Gemma |
| 15:50 | Docker rebuild completed |
| 16:18 | CEO requested git cleanup before push |
| 16:28 | Claude sent cleanup directive to Local-LLM |
| 16:50 | Local-LLM completed initial cleanup |
| 16:59 | Push failed — `rf-detr-base.pth.os0nv4i5.tmp` (299MB) in git history |
| 17:17 | Local-LLM ran `git filter-repo` to purge large file |
| 17:28 | **INCIDENT:** All commits wiped, source files deleted |
| 17:34 | Claude attempted to rebuild repo — discovered source files missing |
| 17:39 | Confirmed: only `.pyc` files remain, no `.py` source |
| 18:43 | Recovery attempt from Docker containers — containers also have only `.pyc` |
| 18:45 | CEO requested Docker image backup |
| 19:06 | Docker backup in progress |

## Root Cause

The `git filter-repo --force --invert-paths --path <file>` command was used to remove a large file from git history. This command:
1. Rewrote all commits
2. Removed the specified file from all commits
3. **Unintended:** Also removed all other files that were not in the current working tree

The source files were likely already unstaged or in a state where `filter-repo` excluded them.

## Impact

- **Lost:** All Python source code (`api_gateway/`, `inference_worker/`, `training_worker/`, `camera_feed_extractor/`)
- **Lost:** All TypeScript/React source code (`frontend/src/`)
- **Lost:** Configuration files, Dockerfiles, scripts
- **Preserved:** Docker images (running containers)
- **Preserved:** `.pyc` compiled bytecode files
- **Preserved:** `.sync/` agent coordination files (separate repo)

## Recovery Options

1. **Decompile `.pyc` files** — Lossy, loses comments and some formatting
2. **Extract from Docker images** — Images only contain `.pyc`, not source
3. **External backups** — None available per CEO
4. **IDE local history** — Potential source if available
5. **Cloud sync** — OneDrive/Dropbox version history if enabled

## Lessons Learned

1. **Never run `git filter-repo` without a backup**
2. **Verify working tree state before destructive git operations**
3. **Use `--dry-run` flags when available**
4. **Maintain off-site backups of source code**

## Action Items

- [ ] Complete Docker image backup
- [ ] Attempt `.pyc` decompilation
- [ ] Check for any other backup sources
- [ ] Rebuild source if decompilation successful

---

**Report By:** Claude (Senior Architecture & Agent Manager)  
**Date:** 2026-05-20T19:09:00+05:30
