"""Cascada de inyeccion de texto: portapapeles+Ctrl+V, Shift+Insert y Unicode."""
from .clipboard import ClipboardBusy, Snapshot
from .injector import MAX_PATCH_CHARS, ROUTES, InjectResult, Injector
from .sendinput import SendOutcome
from .target import Target, current_target, is_terminal, matches_app_list

__all__ = [
    "ClipboardBusy",
    "InjectResult",
    "Injector",
    "MAX_PATCH_CHARS",
    "ROUTES",
    "SendOutcome",
    "Snapshot",
    "Target",
    "current_target",
    "is_terminal",
    "matches_app_list",
]
