"""Task normalization and canonical procedure signature extraction.

Implements Phase 4 (Pattern Mining) normalization layer.
Reduces superficial parameter noise (IDs, hashes, dates, file stems) into canonical procedure intents.
"""

from __future__ import annotations

import re


class TaskNormalizer:
    """Normalizes noisy work orders and task signatures into semantic procedure categories."""

    # Patterns to strip
    _WO_PATTERN = re.compile(r"WO-\d+", re.IGNORECASE)
    _EXP_PATTERN = re.compile(r"EXP-[0-9a-f]{16}", re.IGNORECASE)
    _HEX_PATTERN = re.compile(r"\b[0-9a-f]{6,64}\b", re.IGNORECASE)
    _DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}[:\-]\d{2}[:\-]\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?\b")
    _PUNCT_PATTERN = re.compile(r"[^\w\s]")

    @classmethod
    def normalize_signature(cls, signature: str, query: str = "") -> str:
        """Normalize a task signature and optional query into a canonical intent slug."""
        combined = f"{signature} {query}".strip().lower()

        # Strip dates, work orders, node IDs, hex hashes
        text = cls._WO_PATTERN.sub("", combined)
        text = cls._EXP_PATTERN.sub("", text)
        text = cls._DATE_PATTERN.sub("", text)
        text = cls._HEX_PATTERN.sub("", text)
        text = cls._PUNCT_PATTERN.sub(" ", text)

        # Normalize whitespace
        tokens = [t for t in text.split() if len(t) > 1 and not t.isdigit()]

        # Extract domain intent prefixes if present
        domain_keywords = {
            "migration": "database_migration",
            "migrate": "database_migration",
            "alembic": "database_migration",
            "auth": "authentication_flow",
            "jwt": "authentication_flow",
            "login": "authentication_flow",
            "token": "authentication_flow",
            "redis": "cache_management",
            "cache": "cache_management",
            "pool": "connection_pool_tuning",
            "connection": "connection_pool_tuning",
            "test": "test_verification",
            "pytest": "test_verification",
            "coverage": "test_verification",
            "deploy": "service_deployment",
            "release": "service_deployment",
            "router": "api_routing",
            "endpoint": "api_routing",
            "format": "code_formatting",
            "lint": "code_formatting",
        }

        # Action subverbs that define procedure subtypes
        action_subverbs = {
            "apply", "upgrade", "downgrade", "rotate", "tune", "increase", "decrease",
            "verify", "check", "run", "deploy", "release", "patch", "fix", "clean", "format"
        }

        matched_domain = None
        matched_subverb = None

        for tok in tokens:
            # Strip trailing digits/underscores like tune_1, test_2
            clean_tok = re.sub(r"_\d+$", "", tok)
            if not matched_domain and clean_tok in domain_keywords:
                matched_domain = domain_keywords[clean_tok]
            elif not matched_subverb and clean_tok in action_subverbs:
                matched_subverb = clean_tok
            elif not matched_subverb and tok in action_subverbs:
                matched_subverb = tok

        if matched_domain:
            if matched_subverb:
                return f"{matched_domain}_{matched_subverb}"
            return matched_domain

        if tokens:
            clean_tokens = [re.sub(r"_\d+$", "", t) for t in tokens if not t.isdigit()]
            return "_".join(clean_tokens[:2]) if clean_tokens else "general_procedure"
        return "general_procedure"
