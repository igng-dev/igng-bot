"""Compatibility facade for the extracted shared implementation."""
import sys as _sys
from igngbot_shared import call_log_db as _implementation
_sys.modules[__name__] = _implementation
