"""Contract file hashing utilities for evidence packets.

Provides stable SHA256 hashing of contract artifacts so retries can verify
the contract is unchanged between attempts.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

try:
    from cli.contract import find_contract
except ImportError:
    find_contract = None


def _contract_file_sha256(project_path: Path, work_order_id: str | None) -> str | None:
    """Hash the raw contract artifact so retries can verify the contract is unchanged.

    Returns the SHA256 hex digest of the contract file, or None if the work_order_id
    is missing or the contract file cannot be found/read.
    """
    if not work_order_id:
        return None
    try:
        if find_contract is not None:
            path = find_contract(work_order_id, project_path)
            if path is not None and path.is_file():
                return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        pass
    return None