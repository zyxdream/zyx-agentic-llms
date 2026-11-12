from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def fake_db():
    return SimpleNamespace(session=Mock())
