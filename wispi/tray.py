"""Icono de bandeja. Corre en el hilo principal porque pystray necesita su
propio bucle de mensajes de Windows, igual que el hook.

Los iconos se DIBUJAN con Pillow en vez de embarcar PNGs: son cuatro circulos de
colores, y un fichero binario en el repo por eso no se justifica.

El color es toda la interfaz que tiene esta app. Tiene que leerse de un vistazo:
  gris   = inactivo      rojo  = grabando
  ambar  = procesando    azul  = pausado       rojo oscuro = error
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from . import logging_setup
from .events import State

log = logging_setup.get("tray")
ROOT = Path(__file__).resolve().parent.parent

_COLORS = {
    State.IDLE:         (110, 118, 129),
    State.RECORDING:    (220, 60, 60),
    State.HANDS_FREE:   (235, 100, 60),
    State.TRANSCRIBING: (222, 160, 40),
    State.POLISHING:    (222, 190, 60),
    State.PAUSED:       (70, 120, 200),
    State.ERROR:        (140, 30, 30),
}


def _make_icon(rgb: tuple[int, int, int], size: int = 64):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = size // 8
    d.ellipse([m, m, size - m, size - m], fill=rgb + (255,))
    # Punto interior claro: da forma de "microfono encendido" y hace el icono
    # distinguible en la bandeja aun a 16x16, que es como se ve de verdad.
    c, r = size // 2, size // 7
    d.ellipse([c - r, c - r, c + r, c + r], fill=(250, 250, 250, 235))
    return img


class Tray:
    def __init__(self, app) -> None:
        self.app = app
        self.icon = None
        self._icons = {}

    def _icon_for(self, state: State):
        if state not in self._icons:
            self._icons[state] = _make_icon(_COLORS.get(state, _COLORS[State.IDLE]))
        return self._icons[state]

    def on_state_change(self, state: State) -> None:
        if not self.icon:
            return
        try:
            self.icon.icon = self._icon_for(state)
            self.icon.title = f"WISPI - {state.value}"
        except Exception:
            pass  # la bandeja nunca puede tumbar el dictado

    # -- acciones del menu -------------------------------------------------
    def _toggle_pause(self, icon, item) -> None:
        self.app.toggle_pause()

    def _reload(self, icon, item) -> None:
        changed, warns = self.app.cfg.maybe_reload()
        self.app.post.reload_dictionary()
        log.info("recarga manual: cambios=%s avisos=%s", changed, warns)

    def _open_logs(self, icon, item) -> None:
        try:
            os.startfile(str(ROOT / "logs"))          # noqa: S606
        except Exception as e:
            log.warning("no pude abrir logs: %s", e)

    def _open_config(self, icon, item) -> None:
        try:
            os.startfile(str(ROOT / "config.yaml"))   # noqa: S606
        except Exception as e:
            log.warning("no pude abrir config: %s", e)

    def _show_status(self, icon, item) -> None:
        st = self.app.status()
        log.info("estado: %s", st)
        try:
            icon.notify(
                f"{st['state']} | {st['asr']['model'] if st.get('asr') else '?'} | "
                f"hook p99 {st['hook'].get('p99_us', '?')} us",
                "WISPI",
            )
        except Exception:
            pass

    def _quit(self, icon, item) -> None:
        try:
            self.app.stop()
        finally:
            icon.stop()

    def _build(self):
        import pystray
        from pystray import Menu, MenuItem

        menu = Menu(
            MenuItem(lambda i: "Reanudar" if self.app._paused else "Pausar",
                     self._toggle_pause, default=True),
            Menu.SEPARATOR,
            MenuItem("Estado", self._show_status),
            MenuItem("Recargar configuracion", self._reload),
            MenuItem("Abrir config.yaml", self._open_config),
            MenuItem("Abrir carpeta de logs", self._open_logs),
            Menu.SEPARATOR,
            MenuItem("Salir", self._quit),
        )
        self.icon = pystray.Icon("wispi", self._icon_for(State.IDLE), "WISPI", menu)
        self.app.on_state_change = self.on_state_change
        return self.icon

    def run(self) -> int:
        """Bloquea el hilo actual con el bucle de mensajes del icono."""
        self._build().run()
        return 0

    def run_detached(self) -> None:
        """Arranca el icono en su propio hilo.

        Hace falta cuando el boton flotante ya tiene el hilo principal: Tk y
        pystray necesitan cada uno un bucle de mensajes de Windows y solo uno
        puede quedarse con el MainThread.
        """
        self._build().run_detached()


def run_tray(app) -> int:
    tray = Tray(app)
    # La app arranca ANTES del icon.run() porque este bloquea el hilo principal
    # con su propio bucle de mensajes y ya no vuelve.
    app.start()
    return tray.run()
