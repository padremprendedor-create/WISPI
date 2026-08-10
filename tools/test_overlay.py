"""Comprueba la propiedad que hace usable el boton flotante: NO robar el foco.

POR QUE ES *LA* PRUEBA DE ESTE MODULO
    Si al pulsar el boton la ventana activa pasa a ser el propio boton, el
    dictado se pega dentro de WISPI en vez de en el documento del usuario. El
    boton seguiria "funcionando" -se ve, cambia de color, dispara el dictado- y
    aun asi seria inutil. Es un fallo que no se ve mirando la pantalla.

    Por eso aqui no se comprueba que la ventana aparezca, sino que la ventana en
    PRIMER PLANO sigue siendo otra despues de crearla y despues de clicarla.

Corre sin arrancar el hook de teclado ni el motor de voz: usa una app de mentira.

    uv run python tools/test_overlay.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wispi import logging_setup, winapi           # noqa: E402
from wispi.config import Config                   # noqa: E402
from wispi.events import State                    # noqa: E402


class AppFalsa:
    """Lo minimo que Overlay necesita. Registra los toggles en vez de dictar."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.state = State.IDLE
        self.toggles = 0
        self.cancels = 0
        self.on_state_change = None

    def ui_toggle(self):
        self.toggles += 1
        self.state = (State.HANDS_FREE if self.state == State.IDLE else State.IDLE)

    def ui_cancel(self):
        self.cancels += 1
        self.state = State.IDLE

    def toggle_pause(self):
        return False

    def status(self):
        return {"state": self.state.value, "paused": False, "asr": {}, "hook": {},
                "audio": {}, "asr_warnings": []}

    def stop(self):
        pass


def main() -> int:
    logging_setup.setup("WARNING", console=True)
    cfg = Config.load()
    cfg.ui.overlay_x, cfg.ui.overlay_y = 240, 240  # sitio fijo, sin tocar la config real

    antes = winapi.foreground_hwnd()
    exe_antes = winapi.exe_of_pid(winapi.pid_of_hwnd(antes))
    print(f"ventana en primer plano ANTES: {exe_antes!r} (hwnd {antes})")

    from wispi.overlay import Overlay
    ov = Overlay(AppFalsa(cfg))
    ov.root.update()
    time.sleep(0.4)
    ov.root.update()

    fallos = []

    # 1. La ventana existe y esta colocada.
    if not ov.root.winfo_viewable():
        fallos.append("la ventana del boton no es visible")
    print(f"boton en {ov.root.winfo_x()},{ov.root.winfo_y()}  "
          f"{ov.root.winfo_width()}x{ov.root.winfo_height()}")

    # 2. El estilo extendido lleva NOACTIVATE. Es la comprobacion que importa.
    hwnd = ov._hwnd
    estilo = winapi._GetWindowLong(hwnd, winapi.GWL_EXSTYLE)
    noact = bool(estilo & winapi.WS_EX_NOACTIVATE)
    tool = bool(estilo & winapi.WS_EX_TOOLWINDOW)
    print(f"hwnd del boton: {hwnd}  ex_style=0x{estilo & 0xFFFFFFFF:08X}  "
          f"NOACTIVATE={noact}  TOOLWINDOW={tool}")
    if not noact:
        fallos.append("falta WS_EX_NOACTIVATE: el boton robaria el foco y el dictado "
                      "acabaria dentro de WISPI")
    if not tool:
        fallos.append("falta WS_EX_TOOLWINDOW: el boton apareceria en Alt-Tab")

    # 3. El foreground NO ha cambiado al crear el boton.
    despues = winapi.foreground_hwnd()
    exe_despues = winapi.exe_of_pid(winapi.pid_of_hwnd(despues))
    print(f"ventana en primer plano DESPUES: {exe_despues!r} (hwnd {despues})")
    if despues == hwnd:
        fallos.append("crear el boton se llevo el primer plano")

    # 4. El clic dispara el dictado y cambia el color.
    app = ov.app
    ov._on_press(type("E", (), {"x_root": 0, "y_root": 0})())
    ov._on_release(type("E", (), {"x_root": 0, "y_root": 0})())
    ov.root.update()
    if app.toggles != 1:
        fallos.append(f"un clic deberia disparar 1 toggle, disparo {app.toggles}")
    ov._tick()
    if ov._estado != State.HANDS_FREE:
        fallos.append(f"el boton no reflejo el estado: {ov._estado}")
    else:
        print(f"clic -> toggles={app.toggles}, color de estado -> {ov._estado.value}")

    # 5. Arrastrar mueve, y por encima del umbral NO cuenta como clic.
    x0, y0 = ov.root.winfo_x(), ov.root.winfo_y()
    ov._on_press(type("E", (), {"x_root": 500, "y_root": 500})())
    ov._on_motion(type("E", (), {"x_root": 560, "y_root": 540})())
    ov.root.update()
    x1, y1 = ov.root.winfo_x(), ov.root.winfo_y()
    antes_toggles = app.toggles
    ov._on_release(type("E", (), {"x_root": 560, "y_root": 540})())
    if (x1 - x0, y1 - y0) != (60, 40):
        fallos.append(f"arrastrar movio ({x1-x0},{y1-y0}), se esperaba (60,40)")
    if app.toggles != antes_toggles:
        fallos.append("arrastrar disparo un dictado; solo deberia hacerlo un clic")
    else:
        print(f"arrastre: movio ({x1-x0},{y1-y0}) sin disparar dictado")

    # --- TACTIL --------------------------------------------------------
    print()
    # 6. El temblor del dedo por debajo del umbral NO puede convertir un toque en
    #    arrastre: si lo hiciera, el dictado no arrancaria y pareceria que el
    #    boton "a veces no responde".
    umbral = int(cfg.ui.drag_threshold_px)
    antes_toggles = app.toggles
    ov._on_press(type("E", (), {"x_root": 300, "y_root": 300})())
    ov._on_motion(type("E", (), {"x_root": 300 + umbral - 2, "y_root": 300 + 2})())
    ov._on_release(type("E", (), {"x_root": 300 + umbral - 2, "y_root": 300 + 2})())
    if app.toggles != antes_toggles + 1:
        fallos.append(f"un toque con {umbral-2} px de temblor no disparo el dictado")
    else:
        print(f"toque con {umbral-2} px de temblor (umbral {umbral}) -> dicta igual")

    # 7. DOBLE TOQUE abre el panel y cancela el dictado que arranco el primero.
    #    Si no lo cancelara, el usuario se quedaria grabando mientras navega el
    #    menu, y eso acabaria insertando texto que no pidio.
    ov._ultimo_tap = 0.0
    app.toggles = app.cancels = 0
    tap = type("E", (), {"x_root": 300, "y_root": 300})()
    ov._on_press(tap); ov._on_release(tap)          # primer toque: dicta
    if app.toggles != 1:
        fallos.append("el primer toque del doble no arranco el dictado")
    ov._on_press(tap); ov._on_release(tap)          # segundo, inmediato: panel
    if app.cancels != 1:
        fallos.append(f"el doble toque no cancelo el dictado (cancels={app.cancels}); "
                      "el usuario se quedaria grabando dentro del menu")
    if app.toggles != 1:
        fallos.append(f"el segundo toque dicto ademas de abrir el panel "
                      f"(toggles={app.toggles})")
    if app.toggles == 1 and app.cancels == 1:
        print(f"doble toque -> cancela el dictado ({app.cancels}) y abre el panel")
    panel = ov._panel
    if not (panel and panel.vivo()):
        fallos.append("mantener pulsado no abrio el panel tactil")
    else:
        ov.root.update()
        n_botones = len(panel.win.winfo_children()[0].winfo_children())
        print(f"panel abierto con {n_botones} botones")
        # 8. El panel tampoco puede robar el foco.
        estilo_p = winapi._GetWindowLong(panel._hwnd, winapi.GWL_EXSTYLE)
        if not (estilo_p & winapi.WS_EX_NOACTIVATE):
            fallos.append("el panel no lleva NOACTIVATE: el dictado lanzado desde el "
                          "se pegaria dentro del propio panel")
        else:
            print("panel: NOACTIVATE puesto, no roba el foco")
        if winapi.foreground_hwnd() == panel._hwnd:
            fallos.append("abrir el panel se llevo el primer plano")
        # 9. Los botones son blancos comodos para el dedo.
        #    winfo_height(), NO winfo_reqheight(): el segundo ignora el ipady del
        #    pack y devolvia 26 px cuando en pantalla son 48. Lo que importa es
        #    el blanco real que ve el dedo.
        panel.win.update_idletasks()
        alto = panel.win.winfo_children()[0].winfo_children()[0].winfo_height()
        if alto < 34:
            fallos.append(f"botones del panel de {alto} px: pequenos para un dedo")
        else:
            print(f"botones del panel: {alto} px de alto")

    # 10. Tocar el boton con el panel abierto lo cierra, no dicta.
    if ov._panel and ov._panel.vivo():
        antes_toggles = app.toggles
        ov._on_press(tap)
        ov._on_release(tap)
        if ov._panel is not None and ov._panel.vivo():
            fallos.append("tocar el boton con el panel abierto no lo cerro")
        elif app.toggles != antes_toggles:
            fallos.append("cerrar el panel disparo un dictado")
        else:
            print("toque con el panel abierto -> lo cierra sin dictar")

    # 10b. RECORRER LAS PESTANAS no puede descuadrar el panel.
    #      Dos fallos reales que se sumaban: la ventana conservaba el alto de la
    #      pagina con la que se abrio (las largas salian recortadas por abajo), y
    #      el columnconfigure de la pagina anterior sobrevivia (al pasar de 4
    #      columnas a 2, las dos sobrantes seguian reservando la mitad del ancho
    #      y los botones salian aplastados con el texto cortado).
    ov._panel = None
    ov._ultimo_tap = 0.0
    ov.abrir_panel()
    p = ov._panel
    if p and p.vivo():
        for pagina in ("Mover", "Simbolos", "Opciones", "Teclas", "Simbolos"):
            p._ir_a(pagina)
            p.win.update_idletasks()
            alto_win = p.win.winfo_height()
            alto_nec = p.win.winfo_reqheight()
            botones = p._cuerpo.grid_slaves()
            if alto_win + 2 < alto_nec:
                fallos.append(f"pestana {pagina}: ventana de {alto_win} px para "
                              f"{alto_nec} px de contenido; queda recortada")
            # Ninguna columna vacia puede llevarse ancho: se compara el ancho de
            # los botones con el que les tocaria por su numero de columnas.
            cols = len({int(b.grid_info()["column"]) for b in botones}) if botones else 0
            if cols:
                ancho_max = max(b.winfo_width() for b in botones)
                esperado = (p.ANCHO - 12) / cols
                if ancho_max < esperado * 0.75:
                    fallos.append(f"pestana {pagina}: botones de {ancho_max} px con "
                                  f"{cols} columnas (tocarian ~{esperado:.0f}); "
                                  f"columnas fantasma de la pagina anterior")
            print(f"  pestana {pagina:<9} {alto_win:3d} px alto, {cols} columnas, "
                  f"{len(botones)} botones")
        p.cerrar()
    else:
        fallos.append("no se pudo abrir el panel para revisar las pestanas")

    # 11. Dos toques LENTOS (fuera de la ventana) son empezar y parar, no panel.
    #     Es el flujo normal de dictado y no puede confundirse con el gesto.
    ov._ultimo_tap = 0.0
    ov._panel = None
    app.toggles = app.cancels = 0
    ventana = int(cfg.ui.double_tap_ms) / 1000.0
    ov._on_press(tap); ov._on_release(tap)
    time.sleep(ventana + 0.12)
    ov._on_press(tap); ov._on_release(tap)
    if app.toggles != 2 or app.cancels != 0 or (ov._panel and ov._panel.vivo()):
        fallos.append(f"dos toques lentos deberian ser empezar+parar; salio "
                      f"toggles={app.toggles} cancels={app.cancels} "
                      f"panel={bool(ov._panel and ov._panel.vivo())}")
    else:
        print(f"dos toques separados {ventana+0.12:.2f}s -> empezar y parar, sin panel")

    ov.root.destroy()

    print()
    if fallos:
        print(f"{len(fallos)} FALLOS:")
        for f in fallos:
            print(f"  - {f}")
        return 1
    print("boton flotante OK: no roba el foco | toque dicta | doble toque abre el "
          "panel y cancela | arrastre solo mueve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
