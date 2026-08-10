"""Ventana diana para probar la inyeccion de texto SIN manos humanas.

POR QUE
    La inyeccion es el punto de fallo numero 2 del proyecto y la unica forma
    honesta de verificarla es comprobar que el texto LLEGO a otra aplicacion.
    Abrir Notepad y mirar con los ojos no es automatizable y ademas el Notepad
    moderno de Windows 11 es UWP y no expone su contenido facilmente.

    Esta ventana Tkinter hace de diana: recibe la inyeccion como cualquier otra
    app (proceso separado, foco real, cola de mensajes propia) y vuelca lo que
    tiene a un fichero JSON que el test puede leer.

POR QUE UN PROCESO SEPARADO Y NO UN HILO
    Porque el caso real es CROSS-PROCESS. Inyectar en un widget del mismo
    proceso se saltaria justo la parte que puede fallar (UIPI, foco, cola de
    mensajes ajena). Si funciona aqui, funciona en Notepad.

USO
    Arrancar:   uv run python tools/target_window.py --state <ruta.json>
    Comandos:   escribir una linea en <ruta.json>.cmd  ->  "clear" | "focus" | "quit"
    Leer:       el JSON tiene {"content": "...", "chars": N, "ts": ..., "pid": N,
                               "hwnd": N, "focused": bool}

LIMITACION
    Es un Text de Tk, no un Electron ni una terminal. Que funcione aqui NO
    demuestra que funcione en Windows Terminal con Claude Code: esa comprobacion
    sigue siendo del criterio C4 y la tiene que hacer un humano.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tkinter as tk
from pathlib import Path

POLL_MS = 100


class TargetWindow:
    def __init__(self, state_path: Path, title: str = "WISPI-TARGET") -> None:
        self.state_path = state_path
        self.cmd_path = Path(str(state_path) + ".cmd")
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("900x360+120+120")
        self.root.attributes("-topmost", True)

        self.text = tk.Text(self.root, font=("Consolas", 11), wrap="word",
                            undo=True, maxundo=200)
        self.text.pack(fill="both", expand=True, padx=8, pady=8)

        self.status = tk.Label(self.root, text="", anchor="w", font=("Consolas", 9))
        self.status.pack(fill="x", padx=8, pady=(0, 6))

        self._dump_count = 0
        self.root.after(50, self._grab_focus)
        self.root.after(POLL_MS, self._tick)

    # -- foco --------------------------------------------------------------
    def _grab_focus(self) -> None:
        """Forzar el foco de verdad. Sin esto la inyeccion va a otra ventana y
        el test falla por una razon que no tiene que ver con el codigo probado."""
        try:
            self.root.lift()
            self.root.focus_force()
            self.text.focus_set()
            self.text.mark_set("insert", "end")
        except tk.TclError:
            pass

    # -- bucle -------------------------------------------------------------
    def _tick(self) -> None:
        self._handle_cmd()
        self._dump()
        self.root.after(POLL_MS, self._tick)

    def _handle_cmd(self) -> None:
        if not self.cmd_path.exists():
            return
        try:
            # utf-8-sig, no utf-8: PowerShell 5.1 escribe UTF-8 CON BOM con
            # `Set-Content -Encoding utf8`, y entonces el comando llega como
            # "﻿quit" y no matchea nunca. Costo de depurar eso: alto.
            cmd = self.cmd_path.read_text(encoding="utf-8-sig").strip().lower()
            self.cmd_path.unlink(missing_ok=True)
        except OSError:
            return
        if cmd == "clear":
            self.text.delete("1.0", "end")
        elif cmd == "focus":
            self._grab_focus()
        elif cmd == "quit":
            # root.quit() solo rompe el mainloop; el proceso puede quedarse vivo
            # si Tk aun tiene callbacks pendientes, y entonces la siguiente prueba
            # hereda una ventana zombi robando el foco. destroy() + os._exit corta
            # de raiz. Es un arnes de test: no hay estado que preservar.
            self._dump()
            try:
                self.root.destroy()
            except tk.TclError:
                pass
            os._exit(0)

    def _dump(self) -> None:
        # Tk anade siempre un \n final propio del widget: se quita para que el
        # test compare con lo que realmente se inyecto y no con un artefacto de Tk.
        content = self.text.get("1.0", "end")
        if content.endswith("\n"):
            content = content[:-1]
        try:
            hwnd = int(self.root.winfo_id())
        except tk.TclError:
            hwnd = 0
        focused = False
        try:
            focused = bool(self.root.focus_displayof())
        except (tk.TclError, KeyError):
            pass
        state = {
            "content": content,
            "chars": len(content),
            "lines": content.count("\n") + 1 if content else 0,
            "ts": time.time(),
            "pid": os.getpid(),
            "hwnd": hwnd,
            "focused": focused,
            "dumps": self._dump_count,
        }
        self._dump_count += 1
        tmp = Path(str(self.state_path) + ".tmp")
        try:
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_path)  # atomico: el lector nunca ve medio JSON
        except OSError:
            pass
        self.status.config(
            text=f"pid={state['pid']}  hwnd={hwnd}  chars={state['chars']}  "
                 f"focused={focused}  -> {self.state_path.name}"
        )

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    ap = argparse.ArgumentParser(description="Ventana diana para probar la inyeccion")
    ap.add_argument("--state", required=True, help="ruta del JSON de estado")
    ap.add_argument("--title", default="WISPI-TARGET")
    args = ap.parse_args()
    state = Path(args.state)
    state.parent.mkdir(parents=True, exist_ok=True)
    TargetWindow(state, args.title).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
