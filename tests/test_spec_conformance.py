"""Spec conformance: every numbered spec section must be referenced by a test.

This is the `ContextSubstrate` discipline applied to the project's own
reference implementation — if someone adds a section to the spec and
forgets to write a conformance test, `pytest` fails with an explicit
pointer to the orphaned section, not a silent drift.

A test "references" a spec section if its file contains the literal
token `§X.Y[.Z]` (with the same numbering as the spec heading). Any
section in `requirements/bulk-extract-distribution-spec.md` that has no
such reference anywhere under `tests/` is a failure.

Deliberate exceptions live in `_EXEMPT_SECTIONS` below — use sparingly
and always with a comment explaining why a runtime test cannot cover
the section (e.g. purely narrative sections like Glossary).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "requirements" / "bulk-extract-distribution-spec.md"
TESTS_DIR = REPO_ROOT / "tests"

# Sections that are narrative-only or refer to production-only concerns
# the reference implementation cannot exercise (e.g. SKIP LOCKED scale-out,
# BYOK). Keep this list short; a growing exemption list is a code smell.
_EXEMPT_SECTIONS = {
    "1.1",  # In Scope — prose
    "1.2",  # Out of Scope — prose
    "1.3",  # Design Principles — prose (enforced by other tests)
    "2.1",  # Actors — prose
    "2.2",  # Communication Flow Summary — prose
    "3.1",  # Base Path — covered implicitly by every api_client test
    "3.2",  # Parent heading for §3.2.1–5; each child has its own tests
    "4.7.1",  # HMAC secret rotation — governance/operational, no runtime code
    "5.2",  # File sizing guidelines — advisory, not enforced in code
    "6.5",  # Key management / BYOK — production topology
    "8.1",  # SLI definitions — narrative
    "8.2",  # SLO targets — measurement framework, not runtime
    "8.4",  # Peak-window capacity planning — narrative
    "8.4.1",  # Worker scale-out mechanism — production topology
    "9.3",  # Alerts — rule definitions for an external monitoring stack
    "10.1",  # API versioning — governance
    "10.2",  # Event schema versioning — governance
    "10.3",  # Output schema versioning — governance
    "10.4",  # Versioning interaction matrix — governance
    "11.1",  # Onboarding prerequisites — operational
    "11.2",  # Sandbox — the /sandbox prefix exists; contract parity tested via §3.2.*
    "11.3",  # Consumer responsibilities — prose
    "12.2",  # Event bus outage — hard to simulate in contract emulator
    "12.3",  # Storage outage — hard to simulate against local FS
    "12.4",  # Peak window — load-test territory
}


_HEADING_RE = re.compile(r"^#+\s+(\d+(?:\.\d+){0,2})\s+", re.MULTILINE)


def _spec_sections() -> set[str]:
    text = SPEC_PATH.read_text(encoding="utf-8")
    return set(_HEADING_RE.findall(text))


def _referenced_sections() -> set[str]:
    referenced: set[str] = set()
    ref_re = re.compile(r"§(\d+(?:\.\d+){0,2})")
    for path in TESTS_DIR.rglob("*.py"):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        referenced.update(ref_re.findall(text))
    return referenced


def test_spec_exists_and_is_readable() -> None:
    assert SPEC_PATH.is_file(), f"spec missing at {SPEC_PATH}"
    sections = _spec_sections()
    assert sections, "could not parse any numbered sections from spec"


def test_every_spec_section_has_a_test_reference() -> None:
    declared = _spec_sections()
    referenced = _referenced_sections()
    expected = declared - _EXEMPT_SECTIONS
    missing = sorted(expected - referenced, key=_sort_key)
    if missing:
        pytest.fail(
            "The following spec sections have no test referencing them "
            "(add `§X.Y` in a test docstring or string literal, or add the "
            "section to _EXEMPT_SECTIONS with a justifying comment):\n  - " + "\n  - ".join(missing)
        )


def test_no_stale_exemptions() -> None:
    declared = _spec_sections()
    stale = sorted(_EXEMPT_SECTIONS - declared, key=_sort_key)
    assert not stale, (
        "_EXEMPT_SECTIONS contains entries that no longer exist in the spec — "
        f"remove them: {stale}"
    )


def _sort_key(section: str) -> tuple[int, ...]:
    return tuple(int(p) for p in section.split("."))
