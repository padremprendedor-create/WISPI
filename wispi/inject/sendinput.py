"""Emision de input sintetico con SendInput. Todo firmado con WISPI_EXTRAINFO.

Por que SendInput y no PostMessage:
    PostMessage(WM_KEYDOWN) NO simula un teclado. Se salta la capa de estado del
    teclado del sistema (GetKeyState, modificadores, IME, aceleradores), asi que
    funciona en un Notepad y falla en todo lo demas. Confirmado por Raymond Chen
    (The Old New Thing, marzo 2025). No es un fallback: no se considera.

Por que KEYEVENTF_UNICODE y nunca scan codes de VK:
    Los VK son posicionales. Mandar VK_A produce 'a' en QWERTY y 'q' en AZERTY.
    El usuario tiene teclado espanol/latinoamericano: con VK, las tildes, la enye
    y todo lo que pase por AltGr saldria mal. KEYEVENTF_UNICODE manda el codepoint
    (wVk=0, wScan=codepoint) y es INDEPENDIENTE DEL LAYOUT.

Por que SendInput devolviendo 0 es un fallo grave y no un "no paso nada":
    Devuelve 0 cuando UIPI bloquea el input (la ventana en foco corre elevada y
    nosotros no). Tragarselo significa que el usuario dicta, no aparece nada, y
    no hay ni una linea en el log explicando por que. Se comprueba SIEMPRE contra
    el numero de eventos enviados y se registra `get_last_error()`.
"""
from __future__ import annotations

import ctypes
import struct
import time
from typing import NamedTuple

from .. import winapi
from ..winapi import (
    INPUT,
    INPUT_KEYBOARD,
    KEYBDINPUT,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    VK_CONTROL,
    VK_INSERT,
    VK_LCONTROL,
    VK_LMENU,
    VK_LSHIFT,
    VK_LWIN,
    VK_MENU,
    VK_RCONTROL,
    VK_RMENU,
    VK_RSHIFT,
    VK_RWIN,
    VK_SHIFT,
    VK_UNASSIGNED_E8,
    VK_V,
    WISPI_EXTRAINFO,
    EXTENDED_VKS,
    VK_LEFT,
)

# El bloque de edicion/navegacion vive ahora en winapi.py (EXTENDED_VKS), que es
# su sitio: VK_LEFT estaba definido aqui de forma provisional.
# A esas se suman las Win y las variantes derechas de Ctrl/Alt, que tambien
# llevan prefijo E0.
_EXTENDED = EXTENDED_VKS | {VK_RCONTROL, VK_RMENU, VK_LWIN, VK_RWIN}

# Modificadores que se vigilan antes de pegar. Los genericos (CONTROL/MENU/SHIFT)
# dan True si esta pulsada cualquiera de las dos variantes.
WATCHED_MODIFIERS = (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN)

# (vk, flags) de las variantes concretas, para soltar la que realmente este abajo.
_SPECIFIC_MODIFIERS = (
    VK_LCONTROL, VK_RCONTROL, VK_LSHIFT, VK_RSHIFT,
    VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN,
)
_GENERIC_OF = {
    VK_LCONTROL: VK_CONTROL, VK_RCONTROL: VK_CONTROL,
    VK_LSHIFT: VK_SHIFT, VK_RSHIFT: VK_SHIFT,
    VK_LMENU: VK_MENU, VK_RMENU: VK_MENU,
}
_VK_NAMES = {
    VK_CONTROL: "ctrl", VK_MENU: "alt", VK_SHIFT: "shift",
    VK_LCONTROL: "lctrl", VK_RCONTROL: "rctrl",
    VK_LSHIFT: "lshift", VK_RSHIFT: "rshift",
    VK_LMENU: "lalt", VK_RMENU: "ralt",
    VK_LWIN: "lwin", VK_RWIN: "rwin",
}

_HIGH_SURROGATE = range(0xD800, 0xDC00)


class SendOutcome(NamedTuple):
    """Resultado de una tanda de SendInput. `ok` es sent == expected."""

    ok: bool
    sent: int
    expected: int
    error: str = ""


def vk_name(vk: int) -> str:
    """Nombre legible de un modificador. Los logs nunca llevan vkCode crudo (C9.2)."""
    return _VK_NAMES.get(vk, f"vk{vk:#04x}")


def _ki(vk: int, scan: int, flags: int) -> INPUT:
    """Un INPUT de teclado firmado. dwExtraInfo == WISPI_EXTRAINFO siempre (C4.5)."""
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(
        wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=WISPI_EXTRAINFO
    )
    return inp


def _vk_down(vk: int) -> INPUT:
    return _ki(vk, 0, KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)


def _vk_up(vk: int) -> INPUT:
    flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
    return _ki(vk, 0, flags)


def _send(events: list[INPUT]) -> SendOutcome:
    """Envia la tanda. Comprueba el retorno: 0 (o parcial) es FALLO, no silencio."""
    n = len(events)
    if not n:
        return SendOutcome(True, 0, 0, "")
    arr = (INPUT * n)(*events)
    ctypes.set_last_error(0)
    sent = int(winapi.user32.SendInput(n, arr, ctypes.sizeof(INPUT)))
    if sent == n:
        return SendOutcome(True, sent, n, "")
    err = ctypes.get_last_error()
    # 5 = ERROR_ACCESS_DENIED: UIPI. La ventana en foco corre elevada y nosotros no.
    hint = " (UIPI: ventana en foco elevada?)" if err == 5 else ""
    return SendOutcome(False, sent, n, f"SendInput envio {sent}/{n}, err={err}{hint}")


# ---------------------------------------------------------------------------
# Tecla generica: es lo que convierte a WISPI en sustituto del teclado
# ---------------------------------------------------------------------------
def send_key(vk: int, modifiers: tuple[int, ...] = (), count: int = 1) -> SendOutcome:
    """Pulsa `vk` con los modificadores dados, `count` veces.

    Los modificadores se mantienen pulsados durante TODAS las repeticiones (un
    solo down y un solo up al final), no por pulsacion. Con Ctrl+Shift+Left x5
    la diferencia es real: soltando shift entre medias la seleccion se reinicia
    en cada flecha y solo queda seleccionada la ultima palabra.

    El flag de tecla extendida lo pone _vk_down/_vk_up segun EXTENDED_VKS: sin el,
    las flechas y Supr salen como teclas del numerico cuando Bloq Num esta activo.
    """
    if count <= 0:
        return SendOutcome(True, 0, 0, "")
    eventos: list[INPUT] = [_vk_down(m) for m in modifiers]
    for _ in range(count):
        eventos.append(_vk_down(vk))
        eventos.append(_vk_up(vk))
    eventos.extend(_vk_up(m) for m in reversed(modifiers))
    return _send(eventos)


# ---------------------------------------------------------------------------
# Combinaciones de pegado
# ---------------------------------------------------------------------------
def send_ctrl_v() -> SendOutcome:
    """Ctrl+V. Ruta por defecto en aplicaciones graficas normales."""
    return _send([_vk_down(VK_CONTROL), _vk_down(VK_V), _vk_up(VK_V), _vk_up(VK_CONTROL)])


def send_shift_insert() -> SendOutcome:
    """Shift+Insert. Obligatorio en consolas: ahi Ctrl+V no pega.

    VK_INSERT va con KEYEVENTF_EXTENDEDKEY en el down y en el up; sin ese flag
    Windows manda el scancode del 0 del teclado numerico.
    """
    return _send([_vk_down(VK_SHIFT), _vk_down(VK_INSERT), _vk_up(VK_INSERT), _vk_up(VK_SHIFT)])


def send_shift_left(count: int, chunk: int = 40) -> SendOutcome:
    """Selecciona `count` caracteres hacia atras con Shift+Left mantenido.

    Shift se mantiene pulsado durante toda la operacion (un solo down y un solo
    up) para que la seleccion se extienda en vez de reiniciarse en cada flecha.
    Se trocea porque una tanda de cientos de eventos la descartan algunas apps.
    """
    if count <= 0:
        return SendOutcome(True, 0, 0, "")
    total_sent = 0
    total_expected = 0
    out = _send([_vk_down(VK_SHIFT)])
    total_sent += out.sent
    total_expected += out.expected
    if not out.ok:
        return SendOutcome(False, total_sent, total_expected, out.error)
    try:
        left = count
        while left > 0:
            batch = min(chunk, left)
            events: list[INPUT] = []
            for _ in range(batch):
                events.append(_vk_down(VK_LEFT))
                events.append(_vk_up(VK_LEFT))
            out = _send(events)
            total_sent += out.sent
            total_expected += out.expected
            if not out.ok:
                return SendOutcome(False, total_sent, total_expected, out.error)
            left -= batch
    finally:
        up = _send([_vk_up(VK_SHIFT)])
        total_sent += up.sent
        total_expected += up.expected
    return SendOutcome(True, total_sent, total_expected, "")


# ---------------------------------------------------------------------------
# Texto caracter a caracter, independiente del layout
# ---------------------------------------------------------------------------
def _utf16_units(text: str) -> tuple[int, ...]:
    """Codepoints en unidades UTF-16. Los pares surrogate salen ya partidos en dos.

    Codificar a utf-16-le y leer de 2 en 2 bytes resuelve gratis el caso de los
    caracteres fuera del BMP (emoji): cada uno se convierte en DOS eventos, que
    es exactamente lo que exige KEYEVENTF_UNICODE.
    """
    data = text.encode("utf-16-le")
    return struct.unpack(f"<{len(data) // 2}H", data)


def send_unicode(text: str, chunk: int = 20, gap_s: float = 0.001) -> SendOutcome:
    """Escribe `text` con KEYEVENTF_UNICODE. Ruta de ultimo recurso de la cascada.

    No toca el portapapeles y no depende del layout del teclado. A cambio es la
    mas lenta y algunas apps con autocompletado agresivo la interfieren.
    Un par surrogate nunca se parte entre dos tandas: se manda entero o no se manda.
    """
    if not text:
        return SendOutcome(True, 0, 0, "")
    units = _utf16_units(text)
    total_sent = 0
    total_expected = 0
    i = 0
    n = len(units)
    first = True
    while i < n:
        end = min(i + max(1, chunk), n)
        # No cortar entre el surrogate alto y el bajo: se irian en tandas distintas.
        if end < n and units[end - 1] in _HIGH_SURROGATE:
            end += 1
        events: list[INPUT] = []
        for u in units[i:end]:
            events.append(_ki(0, u, KEYEVENTF_UNICODE))
            events.append(_ki(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        if not first and gap_s > 0:
            time.sleep(gap_s)
        first = False
        out = _send(events)
        total_sent += out.sent
        total_expected += out.expected
        if not out.ok:
            return SendOutcome(False, total_sent, total_expected, out.error)
        i = end
    return SendOutcome(True, total_sent, total_expected, "")


# ---------------------------------------------------------------------------
# Modificadores
# ---------------------------------------------------------------------------
def modifiers_down() -> list[int]:
    """Modificadores fisicamente pulsados ahora mismo."""
    return [vk for vk in WATCHED_MODIFIERS if winapi.is_physically_down(vk)]


def wait_modifiers_released(timeout_ms: int, poll_s: float = 0.004) -> tuple[bool, float]:
    """Espera acotada a que el usuario suelte Ctrl/Alt/Shift/Win. (ok, ms esperados).

    Por que hace falta: el usuario acaba de soltar Ctrl+Win, pero "soltar" no es
    instantaneo ni simultaneo. Si mandamos Ctrl+V con Win todavia abajo, el
    sistema ve Win+Ctrl+V (historial del portapapeles) en vez de pegar.
    """
    t0 = time.perf_counter()
    deadline = t0 + max(0, int(timeout_ms)) / 1000.0
    while True:
        if not modifiers_down():
            return True, (time.perf_counter() - t0) * 1000.0
        if time.perf_counter() >= deadline:
            return False, (time.perf_counter() - t0) * 1000.0
        time.sleep(poll_s)


def press_unassigned_e8() -> SendOutcome:
    """Pulsa y suelta VK 0xE8, que Windows deja SIN ASIGNAR.

    Windows abre el menu Inicio al soltar Win solo si entre el down y el up de Win
    no hubo ninguna otra tecla. Meter una tecla que no hace nada en ninguna
    aplicacion rompe esa condicion sin efecto secundario alguno.
    """
    return _send([_ki(VK_UNASSIGNED_E8, 0, 0), _ki(VK_UNASSIGNED_E8, 0, KEYEVENTF_KEYUP)])


# Alias: winapi.py documenta esta funcion con este nombre.
suppress_start_menu = press_unassigned_e8


def release_stuck_modifiers() -> list[int]:
    """Manda key-ups sinteticos de los modificadores que sigan abajo. Devuelve cuales.

    Ultimo recurso cuando se agota `modifier_release_timeout_ms`. Si hay una tecla
    Win pulsada, primero se cuela el 0xE8 para que el key-up de Win no abra Inicio.
    """
    down_specific = [vk for vk in _SPECIFIC_MODIFIERS if winapi.is_physically_down(vk)]
    covered = {_GENERIC_OF[vk] for vk in down_specific if vk in _GENERIC_OF}
    down_generic = [
        vk for vk in (VK_CONTROL, VK_MENU, VK_SHIFT)
        if vk not in covered and winapi.is_physically_down(vk)
    ]
    if not down_specific and not down_generic:
        return []

    events: list[INPUT] = []
    if any(vk in (VK_LWIN, VK_RWIN) for vk in down_specific):
        events.append(_ki(VK_UNASSIGNED_E8, 0, 0))
        events.append(_ki(VK_UNASSIGNED_E8, 0, KEYEVENTF_KEYUP))
    for vk in down_specific + down_generic:
        events.append(_vk_up(vk))
    _send(events)
    return down_specific + down_generic
