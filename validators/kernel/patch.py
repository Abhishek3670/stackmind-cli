"""Unified diff patch parsing and application engine (zero-dependency)."""

from __future__ import annotations

import re


class PatchError(ValueError):
    """Raised when a patch cannot be applied cleanly."""
    pass


def extract_patch_paths(patch_text: str) -> list[str]:
    """Extract all file paths declared in unified diff headers (---, +++, diff --git)."""
    paths: list[str] = []
    for line in patch_text.splitlines():
        line = line.strip()
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                for p in (parts[2], parts[3]):
                    clean = re.sub(r"^[ab]/", "", p).strip()
                    if clean and clean != "/dev/null":
                        paths.append(clean)
        elif line.startswith(("--- ", "+++ ")):
            raw = line[4:].strip().split("\t")[0]
            clean = re.sub(r"^[ab]/", "", raw).strip()
            if clean and clean != "/dev/null":
                paths.append(clean)
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        norm = p.replace("\\", "/").strip().lstrip("/")
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def apply_unified_diff(original_text: str, patch_text: str) -> str:
    """Apply a unified diff patch to original_text and return the modified text.

    Supports standard unified diff format (with or without file headers):
    @@ -start,count +start,count @@
     context line
    -removed line
    +added line
     context line
    """
    if not patch_text.strip():
        return original_text

    original_lines = original_text.splitlines(keepends=True)
    # Ensure all lines end with newline for consistent processing
    has_trailing_newline = original_text.endswith(("\n", "\r\n")) if original_text else True
    
    # Clean split for processing
    orig = [line.rstrip("\r\n") for line in original_lines]

    patch_lines = patch_text.splitlines()
    
    # Split into hunks
    hunk_regex = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")
    
    hunks: list[tuple[int, int, int, int, list[str]]] = []
    current_hunk_lines: list[str] = []
    current_header: tuple[int, int, int, int] | None = None

    for line in patch_lines:
        match = hunk_regex.match(line)
        if match:
            if current_header is not None:
                hunks.append((*current_header, current_hunk_lines))
                current_hunk_lines = []
            old_start = int(match.group(1))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_start = int(match.group(3))
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            current_header = (old_start, old_count, new_start, new_count)
        elif current_header is not None:
            # Header lines like --- or +++ before first @@ are ignored
            if line.startswith(("+", "-", " ", "\\")):
                current_hunk_lines.append(line)

    if current_header is not None:
        hunks.append((*current_header, current_hunk_lines))

    if not hunks:
        # Check if the patch is a search-and-replace block or direct replacement
        raise PatchError("No unified diff hunks found (missing @@ header)")

    # Apply hunks in order
    result: list[str] = []
    orig_idx = 0

    for old_start, old_count, new_start, new_count, hunk_lines in hunks:
        # 1-indexed to 0-indexed target start
        target_idx = max(0, old_start - 1)

        # Extract expected context & deleted lines from hunk
        expected_context: list[str] = []
        for hline in hunk_lines:
            if hline.startswith((" ", "-")):
                expected_context.append(hline[1:])

        # Locate match position with fallback search if exact line shifted
        match_idx = None
        if target_idx <= len(orig) and _matches_at(orig, target_idx, expected_context):
            match_idx = target_idx
        else:
            # Search within a window around target_idx
            window = 50
            start_search = max(orig_idx, target_idx - window)
            end_search = min(len(orig), target_idx + window)
            for cand_idx in range(start_search, end_search + 1):
                if _matches_at(orig, cand_idx, expected_context):
                    match_idx = cand_idx
                    break

        if match_idx is None:
            # Full scan fallback
            for cand_idx in range(orig_idx, len(orig) + 1):
                if _matches_at(orig, cand_idx, expected_context):
                    match_idx = cand_idx
                    break

        if match_idx is None:
            snippet = "\n".join(expected_context[:3])
            raise PatchError(f"Hunk at line {old_start} failed to apply. Context not found:\n{snippet}")

        # Copy unmodified lines before this hunk
        result.extend(orig[orig_idx:match_idx])
        orig_idx = match_idx

        # Apply hunk edits
        for hline in hunk_lines:
            prefix = hline[:1] if hline else ""
            content = hline[1:] if len(hline) > 1 else ""

            if prefix == " ":
                # Context line
                if orig_idx < len(orig):
                    result.append(orig[orig_idx])
                    orig_idx += 1
            elif prefix == "-":
                # Deleted line: skip in original
                orig_idx += 1
            elif prefix == "+":
                # Added line: insert into result
                result.append(content)
            elif prefix == "\\":
                # "\ No newline at end of file"
                pass

    # Copy any remaining unmodified lines
    result.extend(orig[orig_idx:])

    output = "\n".join(result)
    if has_trailing_newline and not output.endswith("\n"):
        output += "\n"
    return output


def _matches_at(orig: list[str], idx: int, expected: list[str]) -> bool:
    """Check if expected lines match orig starting at idx (ignoring trailing whitespace)."""
    if idx + len(expected) > len(orig):
        return False
    for i, exp in enumerate(expected):
        if orig[idx + i].rstrip() != exp.rstrip():
            return False
    return True
