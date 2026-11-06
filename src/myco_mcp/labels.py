"""Action labels: ``source:entity:operation``.

Every curated tool carries exactly one label, for example ``zendesk:ticket:read``.
Permissions (role grants and deny rules) are label *patterns*: each of the three
segments may be a glob (``*``, ``support_*``), the source segment may name a domain
group from ``domains.yaml``, and the operation ``write`` is shorthand for
``create`` plus ``update``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase

OPERATIONS: frozenset[str] = frozenset({"read", "list", "create", "update", "delete", "send", "export", "admin"})
#: Shorthand operations, expanded when a pattern is matched.
OPERATION_ALIASES: Mapping[str, frozenset[str]] = {"write": frozenset({"create", "update"})}
#: Operations that never change anything upstream (drives MCP ``readOnlyHint``).
READ_ONLY_OPERATIONS: frozenset[str] = frozenset({"read", "list", "export"})
#: Operations that remove or irreversibly publish data (drives MCP ``destructiveHint``).
DESTRUCTIVE_OPERATIONS: frozenset[str] = frozenset({"delete", "send", "admin"})

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SEGMENT_RE = re.compile(r"^[a-z0-9_*?\[\]-]+$")


class LabelError(ValueError):
    """A label or label pattern is malformed."""


@dataclass(frozen=True, order=True)
class Label:
    source: str
    entity: str
    operation: str

    @classmethod
    def parse(cls, text: str) -> Label:
        parts = text.strip().split(":")
        if len(parts) != 3:
            raise LabelError(f"label {text!r} must have the form source:entity:operation")
        source, entity, operation = parts
        if not NAME_RE.match(source):
            raise LabelError(f"label {text!r}: invalid source {source!r}")
        if not NAME_RE.match(entity):
            raise LabelError(f"label {text!r}: invalid entity {entity!r}")
        if operation not in OPERATIONS:
            raise LabelError(f"label {text!r}: operation must be one of {', '.join(sorted(OPERATIONS))}")
        return cls(source, entity, operation)

    @property
    def read_only(self) -> bool:
        return self.operation in READ_ONLY_OPERATIONS

    @property
    def destructive(self) -> bool:
        return self.operation in DESTRUCTIVE_OPERATIONS

    def __str__(self) -> str:
        return f"{self.source}:{self.entity}:{self.operation}"


@dataclass(frozen=True)
class LabelPattern:
    """A label with globs, domain names and the ``write`` shorthand allowed."""

    source: str
    entity: str
    operation: str

    @classmethod
    def parse(cls, text: str) -> LabelPattern:
        parts = str(text).strip().split(":")
        if len(parts) != 3 or not all(parts):
            raise LabelError(f"pattern {text!r} must have the form source:entity:operation (globs allowed)")
        for part in parts:
            if not _SEGMENT_RE.match(part):
                raise LabelError(f"pattern {text!r}: invalid segment {part!r}")
        operation = parts[2]
        if not _has_glob(operation) and operation not in OPERATIONS | OPERATION_ALIASES.keys():
            raise LabelError(
                f"pattern {text!r}: unknown operation {operation!r} "
                f"(use one of {', '.join(sorted(OPERATIONS))}, write or *)"
            )
        return cls(*parts)

    def matches(self, label: Label, domains: Mapping[str, frozenset[str]] | None = None) -> bool:
        if domains and self.source in domains:
            if label.source not in domains[self.source]:
                return False
        elif not fnmatchcase(label.source, self.source):
            return False
        if not fnmatchcase(label.entity, self.entity):
            return False
        alias = OPERATION_ALIASES.get(self.operation)
        if alias is not None:
            return label.operation in alias
        return fnmatchcase(label.operation, self.operation)

    def __str__(self) -> str:
        return f"{self.source}:{self.entity}:{self.operation}"


def _has_glob(text: str) -> bool:
    return any(ch in text for ch in "*?[")
