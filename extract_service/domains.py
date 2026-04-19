"""Domain registry (Appendix A)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DomainDef:
    name: str
    sources: tuple[str, ...]
    schema_version: str


REGISTRY: dict[str, DomainDef] = {
    "nav-ledger": DomainDef(
        name="nav-ledger",
        sources=("ods.nav_positions", "warehouse.gl_entries"),
        schema_version="nav-ledger/v3",
    ),
    "gl": DomainDef(
        name="gl",
        sources=("warehouse.gl_entries",),
        schema_version="gl/v2",
    ),
    "sub-ledger": DomainDef(
        name="sub-ledger",
        sources=("ods.sub_ledger",),
        schema_version="sub-ledger/v1",
    ),
    "transaction": DomainDef(
        name="transaction",
        sources=("ods.transactions",),
        schema_version="transaction/v4",
    ),
}


def get_domain(name: str) -> DomainDef | None:
    return REGISTRY.get(name)
