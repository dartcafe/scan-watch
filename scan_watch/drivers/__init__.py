"""Scanner drivers.

Every scanner is driven by the generic eSCL driver (escl.Escl) unless a driver
for its model exists. Such a driver is a module in this package with a class
derived from Escl that sets MODELS and overrides only what the scanner does
differently, see canon_dr_s150.py. Modules here are found by themselves:
adding a scanner means adding a module, nothing needs to be registered.

The driver is chosen by the model the scanner reports in ScannerCapabilities,
so scan-watch starts with the generic one and switches once the scanner answered.
"""

import importlib
import inspect
import pkgutil

from .escl import Escl, ScannerBusy, WakeUnavailable

__all__ = ["Escl", "ScannerBusy", "WakeUnavailable", "drivers", "scanner_for"]

_drivers = None


def drivers():
    """All driver classes besides the generic one, by module name."""
    global _drivers
    if _drivers is None:
        found = []
        for module in pkgutil.iter_modules(__path__):
            mod = importlib.import_module(f"{__name__}.{module.name}")
            found += [cls for _name, cls in inspect.getmembers(mod, inspect.isclass)
                      if issubclass(cls, Escl) and cls.MODELS and cls.__module__ == mod.__name__]
        _drivers = found
    return _drivers


def scanner_for(host, model=None):
    """A driver instance for the scanner at host: the one for its model, else the generic one."""
    for cls in drivers() if model else ():
        if cls.matches(model):
            return cls(host)
    return Escl(host)
