"""Compatibility facade for the extracted shared implementation."""
import sys as _sys
from igngbot_shared import onebot_api as _implementation
_sys.modules[__name__] = _implementation
