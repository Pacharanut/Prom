"""Architecture test: domain/application must stay framework-free, and dependency direction
must follow Channel -> Application -> Domain <- Adapters/Infrastructure, enforced via
import-linter against the contracts declared in pyproject.toml.
"""

from __future__ import annotations

import shutil
import subprocess


def test_import_linter_contracts_hold() -> None:
    lint_imports = shutil.which("lint-imports")
    assert lint_imports is not None, "lint-imports executable not found on PATH"

    result = subprocess.run(
        [lint_imports],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"import-linter contracts violated:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
