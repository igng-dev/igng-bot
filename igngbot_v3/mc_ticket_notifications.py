"""Compatibility facade for the extracted shared implementation."""
import sys as _sys
from igngbot_shared import mc_ticket_notifications as _implementation
_sys.modules[__name__] = _implementation
