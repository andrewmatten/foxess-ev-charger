"""C11: README documents fallback safety caveats, observed ~60 s revert to max,
3.x limit staging/persistence, 3.1.4 upgrade/rollback reset, and 3.2 features."""
from __future__ import annotations

import pathlib

README = pathlib.Path(__file__).resolve().parent.parent / "README.md"


def test_readme_covers_c11_safety_and_feature_notes():
    text = README.read_text(encoding="utf-8")

    # (a) Warn that user-configured Default Current / validity runs if HA stops.
    assert "Warning:" in text and "Default Current" in text and "offline" in text

    # (b) Reconcile Fallback settings and ~60 s revert to rated maximum.
    assert "own rated maximum" in text
    assert "the charger drops to its **Default Current** once" not in text

    # (c) 3.x staging replaces stale 2.x "reset to the charger's max after each session".
    assert "reset to the charger's max after each session ends" not in text
    assert "stages" in text and "confirmed" in text

    # (d) Upgrading from before 3.1.4 or rolling back resets the two settings once.
    assert "3.1.4" in text and "resets" in text

    # (e) 3.2 features documented: Reconfigure, categories, HA 2024.12.0 floor.
    assert "Reconfigure" in text
    assert "2024.12.0" in text
    assert "Configuration" in text and "Diagnostic" in text
