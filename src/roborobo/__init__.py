"""RoboRobo: long research programs run by scheduled agent sessions.

The framework's files live at the repository root (``template/``,
``schemas/``); this package is the ``program`` command that creates program
repositories from the template, keeps them in step with it, records the
scientist's approvals, and checks that the rules held.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "template"
SCHEMAS = ROOT / "schemas"
