"""Quien recibe el texto: una FOTO de la ventana en primer plano, no una consulta viva.

Por que es una foto y no `GetForegroundWindow()` en el momento de escribir:
    Entre que el usuario suelta la tecla y que el texto esta listo pasan segundos
    (ASR + nivel 0 + a veces LLM). Si preguntasemos "quien tiene el foco" justo
    antes de inyectar, escribiriamos en la ventana equivocada cuando el usuario
    ya se ha ido a otra. `app.py` captura el Target en el key-up y lo arrastra;
    las cuatro compuertas del parche optimista comparan contra esa foto.

Por que el `title` no aparece en el repr:
    Un titulo de ventana lleva datos personales (asunto de un correo, nombre de
    un cliente, ruta de un fichero). Un repr acaba siempre en un log tarde o
    temprano, y C9 del SPEC dice que los logs no llevan contenido del usuario.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import winapi


def _basename(name: str) -> str:
    """Nombre de ejecutable normalizado: sin ruta, sin espacios, en minusculas.

    Acepta tanto 'windowsterminal.exe' como 'C:\\...\\WindowsTerminal.exe' para
    que la lista de config.yaml sea indulgente con como la escriba el usuario.
    """
    if not name:
        return ""
    return str(name).strip().replace("/", "\\").rsplit("\\", 1)[-1].lower()


@dataclass(repr=False)
class Target:
    """Ventana destino de una inyeccion. Inmutable en la practica: es una foto."""

    hwnd: int = 0
    pid: int = 0
    exe: str = ""
    title: str = ""

    @property
    def valid(self) -> bool:
        return bool(self.hwnd)

    def same_window(self, other: "Target | None") -> bool:
        """Compuerta 1 del parche optimista: seguimos en la misma ventana?"""
        return bool(other) and bool(self.hwnd) and self.hwnd == other.hwnd

    def __repr__(self) -> str:  # el titulo NO entra aqui: ver docstring del modulo
        return (
            f"Target(hwnd=0x{self.hwnd:X}, pid={self.pid}, exe={self.exe!r}, "
            f"title=<{len(self.title)} chars>)"
        )


def current_target() -> Target:
    """Foto de la ventana en primer plano ahora mismo. Target() vacio si no hay."""
    hwnd = winapi.foreground_hwnd()
    if not hwnd:
        return Target()
    pid = winapi.pid_of_hwnd(hwnd)
    return Target(
        hwnd=hwnd,
        pid=pid,
        exe=winapi.exe_of_pid(pid),
        title=winapi.window_title(hwnd),
    )


def matches_app_list(exe: str, apps) -> bool:
    """True si `exe` esta en la lista, comparando basename en minusculas.

    Sirve para las dos listas de InjectionCfg: `terminal_apps` y `no_patch_apps`.
    """
    if not exe or not apps:
        return False
    name = _basename(exe)
    if not name:
        return False
    return any(name == _basename(a) for a in apps)


def is_terminal(exe: str, terminal_apps) -> bool:
    """True si la app maneja el pegado como una consola (Ctrl+V no funciona ahi).

    No es un "sistema de perfiles por aplicacion" (fuera de alcance en el SPEC):
    es la unica bifurcacion de fontaneria sin la cual no se puede dictar a Claude
    Code en Windows Terminal.
    """
    return matches_app_list(exe, terminal_apps)
