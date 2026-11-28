"""Chaque module de dictee s'importe seul, dans un interpréteur neuf.

pytest importe les modules dans un ordre qui masque les imports circulaires : seul un premier import isolé les révèle.
"""

import pkgutil
import subprocess
import sys

import pytest

import dictee
import dictee.engines


def _modules() -> list[str]:
    names = [info.name for info in pkgutil.iter_modules(dictee.__path__, "dictee.")]
    names += [info.name for info in pkgutil.iter_modules(dictee.engines.__path__, "dictee.engines.")]
    return sorted(names)


@pytest.mark.parametrize("module", _modules())
def test_module_imports_first(module: str) -> None:
    result = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-800:]
