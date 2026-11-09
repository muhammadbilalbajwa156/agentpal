import sys
from uuid import uuid4

import pytest

from .harness import ROOT, server

# Separately installed by the documented setup; source fallback keeps offline CI portable.
sys.path.insert(0, str(ROOT / "sdk/python"))


@pytest.fixture(scope="module")
def tcp():
    with server("/tmp/m8-f1/offline-" + uuid4().hex) as instance:
        yield instance
