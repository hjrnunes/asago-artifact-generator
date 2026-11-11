"""The case builders moved to the injection-spec library's suite; this shim keeps the
adapter's remaining tests importing them from their old module name until the package
goes. The adapter-local constants below name fixture directories this package alone
reads; they move with it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

# The library's builders, loaded under a private name so this module can keep theirs.
_LIBRARY_TESTS = (Path(__file__).resolve().parents[2] / "asago-injection-spec" / "tests").resolve()
_LIBRARY = _LIBRARY_TESTS / "injection_spec_cases.py"
_spec = importlib.util.spec_from_file_location("_injection_spec_cases_library", _LIBRARY)
assert _spec is not None and _spec.loader is not None
_library = importlib.util.module_from_spec(_spec)
sys.modules.setdefault(_spec.name, _library)
_spec.loader.exec_module(_library)

CASES = _library.CASES
CASE_OPERATIONS = _library.CASE_OPERATIONS
CONDITION = _library.CONDITION
DISPOSITIONS = _library.DISPOSITIONS
JUDGE = _library.JUDGE
PIGGYBANK = _library.PIGGYBANK
PLANTED = _library.PLANTED
READ_SLOT = _library.READ_SLOT
REPO = _library.REPO
SEQUENTIAL_TURNS = _library.SEQUENTIAL_TURNS
SINGLE_TURNS = _library.SINGLE_TURNS
SLOTS = _library.SLOTS
SLOT = _library.SLOT
SPEC = _library.SPEC
TARGET = _library.TARGET
TOOLS = _library.TOOLS
history_binding_options = _library.history_binding_options
planted_stimulus = _library.planted_stimulus
runtime_contract = _library.runtime_contract
seed_document = _library.seed_document
seed_item = _library.seed_item
target_variant = _library.target_variant
write_case_package = _library.write_case_package
write_planted_package = _library.write_planted_package

# The adapter-local fixture roots; the library's builders hold no fixture of their own.
FIXTURES = Path(__file__).parent / "fixtures"
EPISODES = FIXTURES / "episode"
# Sealed by MiDojo at a5f81e9: call records without ``structured_content`` or
# ``injection_outcomes``.
EPISODES_A5F81E9 = FIXTURES / "episode-a5f81e9"
# The runs of ``fixtures/episode/`` as MiDojo at 13d5487 reads them: the snapshot in its form
# (each outcome with its delivery facts, unreported), its verdict with the attack outcome
# fields, and the delivery document a runner writes; ``reported/`` one run whose facts are
# reported (``regrade.py`` says how each was made).
REGRADED = FIXTURES / "regraded-13d5487"
# What ``bundle-midojo parse`` wrote for ``episode/command-single/detected`` at consumer
# fd0de5a, kept as the reference a parse with no runtime flags must still reproduce.
RECEIPTS = FIXTURES / "receipts"


def compile_case(root: Path, case: str, out: Path) -> dict[str, Any]:
    """Compile ``case``'s package for the fixture target into ``out``, as the adapter's
    fixture generators read it."""

    from asago_injection_spec.injection_spec_compile import compile_injection_spec

    return compile_injection_spec(
        write_case_package(root, case),
        out,
        target=TARGET,
        write_disposition=DISPOSITIONS.get(case),
    )
