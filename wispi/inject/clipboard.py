"""Portapapeles Win32 con reintentos y limpieza garantizada.

Por que reintentos y no una sola llamada:
    El portapapeles es un recurso GLOBAL con un unico dueno a la vez.
    `OpenClipboard` falla si cualquier otra app lo tiene abierto en ese instante
    -y siempre hay alguna: gestores de portapapeles, Office, el propio Explorer-.
    El fallo es transitorio y dura milisegundos, asi que se reintenta ~10 veces
    con 15 ms de espera antes de rendirse. Sin esto, un dictado de cada tantos
    se perderia sin motivo aparente.

Por que CloseClipboard va SIEMPRE en un finally:
    Si nos dejamos el portapapeles abierto, TODA aplicacion de Windows que
    intente copiar o pegar se queda colgada hasta que nuestro proceso muera.
    Es el fallo mas caro que puede cometer este modulo.

Propiedad de la memoria (fuente clasica de corrupcion de heap):
    Tras un `SetClipboardData` EXITOSO el sistema pasa a ser DUENO del HGLOBAL:
    liberarlo nosotros es un use-after-free. Si `SetClipboardData` FALLA, el
    bloque sigue siendo nuestro y hay que liberarlo o se fuga.

LIMITACIONES CONOCIDAS de la restauracion (no son bugs arreglables):
    1. Los gestores de portapapeles (Win+V, Ditto, Clipdiary) capturan el texto
       transitorio del dictado igualmente: se enteran en cuanto se publica, mucho
       antes de que lo restauremos.
    2. Solo se restaura CF_UNICODETEXT. Si el portapapeles llevaba una imagen,
       ficheros, HTML enriquecido o un formato privado con punteros a memoria de
       otro proceso, esos formatos NO se pueden reproducir fielmente y se pierden.
       Se prefiere dejar el texto dictado antes que vaciar el portapapeles y
       destruir lo que el usuario tenia.
    La salida de escape para quien le moleste cualquiera de las dos es
    `injection.route: unicode` en config.yaml, que no toca el portapapeles.
"""
from __future__ import annotations

import contextlib
import ctypes
import time
from dataclasses import dataclass

from .. import winapi

# 10 intentos x 15 ms = 150 ms de paciencia. Mas que eso y el usuario nota el
# retardo; menos y perdemos dictados contra gestores de portapapeles lentos.
OPEN_RETRIES = 10
OPEN_WAIT_S = 0.015


class ClipboardBusy(RuntimeError):
    """Nadie solto el portapapeles a tiempo. Es recuperable: se cae a otra ruta."""


@dataclass
class Snapshot:
    """Lo que habia en el portapapeles antes de que lo pisaramos."""

    text: str | None = None
    had_text: bool = False
    ok: bool = False          # se pudo abrir y leer el portapapeles
    seq: int = 0              # numero de secuencia en el momento de la foto

    def __repr__(self) -> str:  # el contenido copiado NO entra en ningun log
        n = len(self.text) if self.text is not None else 0
        return f"Snapshot(ok={self.ok}, had_text={self.had_text}, len={n}, seq={self.seq})"


@contextlib.contextmanager
def _open(retries: int = OPEN_RETRIES, wait_s: float = OPEN_WAIT_S):
    """Abre el portapapeles con reintentos y lo cierra pase lo que pase."""
    opened = False
    err = 0
    for _ in range(max(1, retries)):
        ctypes.set_last_error(0)
        if winapi.user32.OpenClipboard(None):
            opened = True
            break
        err = ctypes.get_last_error()
        time.sleep(wait_s)
    if not opened:
        raise ClipboardBusy(f"OpenClipboard fallo tras {retries} intentos (err={err})")
    try:
        yield
    finally:
        # Sin este finally, cualquier excepcion de arriba cuelga el portapapeles
        # de todo el sistema hasta que muera el proceso.
        winapi.user32.CloseClipboard()


def sequence_number() -> int:
    """Contador global que Windows incrementa en cada cambio del portapapeles.

    No requiere abrirlo. Sirve para detectar que alguien lo modifico entre que
    pegamos y que ibamos a restaurar: en ese caso NO se restaura, porque
    pisariamos algo que el usuario acaba de copiar a mano.
    """
    try:
        return int(winapi.user32.GetClipboardSequenceNumber())
    except Exception:
        return 0


def get_text() -> str | None:
    """Texto Unicode del portapapeles, o None si no hay texto o no se pudo leer."""
    try:
        with _open():
            if not winapi.user32.IsClipboardFormatAvailable(winapi.CF_UNICODETEXT):
                return None
            h = winapi.user32.GetClipboardData(winapi.CF_UNICODETEXT)
            if not h:
                return None
            p = winapi.kernel32.GlobalLock(h)
            if not p:
                return None
            try:
                nbytes = int(winapi.kernel32.GlobalSize(h) or 0)
                nchars = nbytes // 2
                if nchars <= 0:
                    return ""
                raw = ctypes.wstring_at(p, nchars)
            finally:
                # El handle es del portapapeles: solo se desbloquea, NUNCA se libera.
                winapi.kernel32.GlobalUnlock(h)
            return raw.split("\x00", 1)[0]
    except ClipboardBusy:
        return None
    except Exception:
        return None


def set_text(s: str) -> bool:
    """Publica `s` como CF_UNICODETEXT. True si el sistema acepto los datos."""
    if s is None:
        return False
    buf = ctypes.create_unicode_buffer(s)      # create_unicode_buffer ya anade el NUL
    nbytes = ctypes.sizeof(buf)
    try:
        with _open():
            h = winapi.kernel32.GlobalAlloc(winapi.GMEM_MOVEABLE, nbytes)
            if not h:
                return False
            p = winapi.kernel32.GlobalLock(h)
            if not p:
                winapi.kernel32.GlobalFree(h)
                return False
            ctypes.memmove(p, buf, nbytes)
            winapi.kernel32.GlobalUnlock(h)

            # EmptyClipboard nos hace duenos; es obligatorio antes de publicar.
            if not winapi.user32.EmptyClipboard():
                winapi.kernel32.GlobalFree(h)
                return False
            if not winapi.user32.SetClipboardData(winapi.CF_UNICODETEXT, h):
                # Fallo: la memoria sigue siendo NUESTRA. Si no la liberamos, se fuga.
                winapi.kernel32.GlobalFree(h)
                return False
            # Exito: el sistema es DUENO de h. Liberarlo aqui seria use-after-free.
            return True
    except ClipboardBusy:
        return False
    except Exception:
        return False


def snapshot() -> Snapshot:
    """Foto del portapapeles antes de pisarlo. Nunca lanza."""
    seq = sequence_number()
    try:
        with _open():
            available = bool(winapi.user32.IsClipboardFormatAvailable(winapi.CF_UNICODETEXT))
            if not available:
                return Snapshot(text=None, had_text=False, ok=True, seq=seq)
            h = winapi.user32.GetClipboardData(winapi.CF_UNICODETEXT)
            if not h:
                return Snapshot(text=None, had_text=False, ok=True, seq=seq)
            p = winapi.kernel32.GlobalLock(h)
            if not p:
                return Snapshot(text=None, had_text=False, ok=False, seq=seq)
            try:
                nbytes = int(winapi.kernel32.GlobalSize(h) or 0)
                nchars = nbytes // 2
                raw = ctypes.wstring_at(p, nchars) if nchars > 0 else ""
            finally:
                winapi.kernel32.GlobalUnlock(h)
            return Snapshot(text=raw.split("\x00", 1)[0], had_text=True, ok=True, seq=seq)
    except ClipboardBusy:
        return Snapshot(ok=False, seq=seq)
    except Exception:
        return Snapshot(ok=False, seq=seq)


def restore(snap: Snapshot | None) -> bool:
    """Devuelve al portapapeles lo que hubiera antes. Ver limitaciones del modulo.

    Si la foto no tenia texto NO se vacia el portapapeles: preferimos dejar ahi
    el texto dictado antes que destruir una imagen o unos ficheros del usuario.
    """
    if snap is None or not snap.had_text or snap.text is None:
        return False
    return set_text(snap.text)
