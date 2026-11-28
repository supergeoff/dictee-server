"""Chaque module de dictee s'importe seul, dans un interpréteur neuf.

pytest importe les modules dans un ordre qui masque les imports circulaires : seul un premier import isolé les révèle.
"""

import pkgutil
import subprocess
import sys

import pytest

import dictee


def _modules() -> list[str]:
    """Tous les modules du paquet, sous-paquets compris."""
    return sorted(info.name for info in pkgutil.walk_packages(dictee.__path__, "dictee."))


@pytest.mark.parametrize("module", _modules())
def test_module_imports_first(module: str) -> None:
    result = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-800:]
