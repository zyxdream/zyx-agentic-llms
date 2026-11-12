"""Compatibility alias for the shared Hallucination Guard backend engine.

The standalone-hg branch exposed backend logic from ``backend.standalone_hg``.
The shared source of truth now lives in ``backend.hallucination_guard``; alias
the module object so existing tests and monkeypatch paths keep affecting the
real implementation.
"""
import sys

from backend import hallucination_guard as _hallucination_guard

sys.modules[__name__] = _hallucination_guard
