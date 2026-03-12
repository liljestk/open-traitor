# OpenTraitor: Autonomous LLM Trading Agent for Coinbase
import os as _os

_version_file = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "VERSION")
try:
    with open(_version_file) as _f:
        __version__ = _f.read().strip()
except FileNotFoundError:
    __version__ = "0.0.0"
