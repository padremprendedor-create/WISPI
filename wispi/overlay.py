"""Boton flotante: clic para dictar, sin tocar el teclado.

LA DECISION QUE HACE QUE ESTO FUNCIONE
    El boton lleva WS_EX_NOACTIVATE. Sin ese estilo, pulsarlo convierte al propio
    boton en la ventana ACTIVA, y entonces el dictado se pegaria dentro de WISPI
    en vez de en el documento que el usuario tenia delante. No es un detalle de
    pulido: es la diferencia entre que el boton sirva o no sirva.
    (Ver winapi.make_click_through_overlay.)

POR QUE Tkinter Y NO OTRA COSA
    Viene con Python, no anade dependencias, y para un circulo de 58 px con un
    microfono dentro sobra. Una libreria de UI para esto seria peso muerto en un
    proceso que tiene que estar encendido todo el dia.

QUIEN MANDA EN EL HILO PRINCIPAL
    Tk. Su mainloop() vive en el MainThread y la bandeja (pystray) pasa a modo
    detached. Los dos necesitan bucle de mensajes de Windows y solo uno puede
    tener el hilo principal; Tk lo gana porque es la interfaz que el usuario ve.

GESTOS (los mismos con dedo y con raton)
    toque / clic       -> empieza o termina el dictado
    doble toque        -> panel de opciones con botones grandes
    arrastrar          -> mover el boton; la posicion se guarda sola
    clic derecho       -> el mismo panel (atajo de raton)

POR QUE EL DOBLE TOQUE NO RETRASA EL TOQUE SIMPLE
    El primer toque dispara el dictado AL INSTANTE. Si llega un segundo dentro
    de la ventana, se cancela lo empezado y se abre el panel. Lo natural seria
    esperar la ventana antes de actuar, pero eso meteria ~350 ms de retardo en
    CADA dictado -la accion que se hace cincuenta veces al dia- para dar servicio
    a un gesto que se usa de vez en cuando. Cancelar sale gratis: esos 300 ms de
    audio caen por debajo de min_duration_s y el gate los descarta.

POR QUE UN PANEL PROPIO Y NO EL MENU DE WINDOWS
    En una pantalla tactil el menu contextual solo sale manteniendo pulsado y
    esperando a que Windows decida convertirlo en clic derecho: llega tarde, a
    veces no llega, y sus entradas son demasiado finas para un dedo. El panel es
    una ventana normal con botones de 48 px.

    Hereda WS_EX_NOACTIVATE del boton por el mismo motivo que el: si tomara el
    foco, el dictado que lanzas desde el se pegaria dentro del panel.
"""
from __future__ import annotations

import time
import tkinter as tk

from . import logging_setup, winapi
from .events import State

log = logging_setup.get("overlay")

TICK_MS = 120           # refresco del color y del pulso
PANEL_BOTON_H = 46      # alto de cada boton del panel; blanco comodo para un dedo
PANEL_ANCHO = 208

# Mismos colores que la bandeja: el usuario aprende UN codigo, no dos.
COLORES: dict[State, tuple[str, str]] = {
    State.IDLE:         ("#6e7681", "Listo. Clic para dictar"),
    State.RECORDING:    ("#dc3c3c", "Grabando... (Ctrl+Win)"),
    State.HANDS_FREE:   ("#eb643c", "Grabando manos libres. Clic para parar"),
    State.TRANSCRIBING: ("#dea028", "Transcribiendo..."),
    State.POLISHING:    ("#debe3c", "Limpiando el texto..."),
    State.PAUSED:       ("#4678c8", "En pausa"),
    State.ERROR:        ("#8c1e1e", "Error: mira los logs"),
}


CTRL = (winapi.VK_CONTROL,)
SHIFT = (winapi.VK_SHIFT,)
CTRL_SHIFT = (winapi.VK_CONTROL, winapi.VK_SHIFT)

# Teclas que se repiten al mantenerlas, como en un teclado de verdad. Borrar de
# una en una tocando 30 veces no es usable.
REPETIBLES = {winapi.VK_BACK, winapi.VK_DELETE, winapi.VK_LEFT, winapi.VK_RIGHT,
              winapi.VK_UP, winapi.VK_DOWN}
REPEAT_DELAY_MS = 420    # espera antes de empezar a repetir
REPEAT_EVERY_MS = 55

SIMBOLOS = [
    "@", "#", "/", "\\", "|", "_",
    "-", ":", ";", ",", ".", "?",
    "(", ")", "[", "]", "{", "}",
    "<", ">", "=", "+", "*", "&",
    "%", "$", "€", "¿", "¡", "~",
    "\"", "'", "`", "^", "°", "…",
]


class KeyPanel:
    """Teclado tactil: teclas, navegacion, simbolos y opciones.

    NO TOMA EL FOCO, igual que el boton. Es lo unico que hace que esto funcione:
    si el panel se activara al tocarlo, la ventana donde estas escribiendo
    dejaria de ser la ventana en foco y cada Enter, cada flecha y cada simbolo
    irian a parar al propio panel.

    Los botones son `Label` y no `Button` por lo mismo: un Button de Tk pide el
    foco al pulsarlo y pelea con WS_EX_NOACTIVATE.
    """

    ANCHO = 348

    def __init__(self, overlay: "Overlay") -> None:
        self.ov = overlay
        self.app = overlay.app
        self.win = tk.Toplevel(overlay.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#22262d")
        self._cierre_id = None
        self._repeat_id = None
        self._gen_activa: int | None = None
        self._pagina = overlay._ultima_pagina
        # None hasta la primera colocacion: _pintar() corre antes que _colocar()
        # y tiene que saber que aun no hay posicion que conservar.
        self._x: int | None = None
        self._y: int | None = None

        self._cabecera = tk.Frame(self.win, bg="#22262d")
        self._cabecera.pack(fill="x", padx=6, pady=(6, 0))
        self._cuerpo = tk.Frame(self.win, bg="#22262d")
        self._cuerpo.pack(fill="both", expand=True, padx=6, pady=6)
        self._pie = tk.Frame(self.win, bg="#22262d")
        self._pie.pack(fill="x", padx=6, pady=(0, 6))

        self._tabs: dict[str, tk.Label] = {}
        for nombre in ("Teclas", "Mover", "Simbolos", "Opciones"):
            t = tk.Label(self._cabecera, text=nombre, bg="#2b3038", fg="#c9d1d9",
                         font=("Segoe UI", 10), cursor="hand2")
            t.pack(side="left", fill="x", expand=True, padx=1, ipady=8)
            t.bind("<ButtonRelease-1>", lambda e, n=nombre: self._ir_a(n))
            self._tabs[nombre] = t

        self._boton(self._pie, "Dictar", self._dictar, "#2f7d4f",
                    font=("Segoe UI", 11, "bold")).pack(side="left", fill="x",
                                                        expand=True, ipady=11)
        self._boton(self._pie, "Cerrar", self.cerrar, "#3d4450").pack(
            side="left", padx=(4, 0), ipadx=14, ipady=11)

        self._pintar()
        self._colocar()
        self.win.update_idletasks()
        hwnd = self.win.winfo_id()
        padre = winapi.user32.GetParent(hwnd)
        self._hwnd = int(padre) if padre else int(hwnd)
        winapi.make_click_through_overlay(self._hwnd)
        winapi.show_without_activating(self._hwnd)
        self._armar_autocierre()

    # -- construccion ------------------------------------------------------
    def _boton(self, padre, texto, cmd, color="#3d4450", font=("Segoe UI", 11),
               repite_vk=None) -> tk.Label:
        b = tk.Label(padre, text=texto, bg=color, fg="white", font=font,
                     cursor="hand2")
        if repite_vk is not None:
            # Mantener pulsado repite, como una tecla fisica.
            b.bind("<ButtonPress-1>", lambda e, c=cmd: self._empezar_repeticion(c))
            b.bind("<ButtonRelease-1>", lambda e: self._parar_repeticion())
        else:
            b.bind("<ButtonRelease-1>", lambda e, c=cmd: (self._armar_autocierre(), c()))
        b.bind("<Enter>", lambda e, w=b: w.config(relief="raised", bd=1))
        b.bind("<Leave>", lambda e, w=b: w.config(relief="flat", bd=0))
        return b

    def _grid(self, items, columnas: int, alto_ipady: int = 12,
              font=("Segoe UI", 11)) -> None:
        for c in range(columnas):
            self._cuerpo.columnconfigure(c, weight=1, uniform="k")
        for i, item in enumerate(items):
            texto, cmd, color, vk = item
            b = self._boton(self._cuerpo, texto, cmd, color, font, repite_vk=vk)
            b.grid(row=i // columnas, column=i % columnas, sticky="nsew",
                   padx=2, pady=2, ipady=alto_ipady)

    def _ir_a(self, nombre: str) -> None:
        self._pagina = nombre
        self.ov._ultima_pagina = nombre   # se recuerda para la proxima apertura
        self._armar_autocierre()
        self._pintar()

    def _pintar(self) -> None:
        # Se DESTRUYE Y RECREA el marco entero en vez de vaciarlo.
        #
        # columnconfigure() es del CONTENEDOR, no de los hijos, y sobrevive a
        # borrar los botones. Al pasar de "Mover" (4 columnas) a "Opciones" (2),
        # las columnas 2 y 3 seguian con weight=1 reservando la mitad del ancho,
        # y los botones salian aplastados y con el texto cortado. Vaciar el marco
        # no lo arregla; recrearlo si, y de paso no deja rastro de la pagina
        # anterior.
        try:
            self._cuerpo.destroy()
        except tk.TclError:
            pass
        # before=self._pie para conservar el orden: cabecera, cuerpo, pie. Sin
        # eso el marco nuevo se empaqueta al final y el cuerpo aparece DEBAJO de
        # la barra de "Dictar".
        self._cuerpo = tk.Frame(self.win, bg="#22262d")
        self._cuerpo.pack(fill="both", expand=True, padx=6, pady=6,
                          before=self._pie)

        for nombre, t in self._tabs.items():
            activa = nombre == self._pagina
            t.config(bg="#3d4450" if activa else "#2b3038",
                     fg="white" if activa else "#8b949e")
        getattr(self, f"_pagina_{self._pagina.lower()}")()
        # Cada pagina tiene su alto: 6 teclas no ocupan lo mismo que 36 simbolos.
        # Sin esto la ventana conserva el alto de la pagina con la que se abrio y
        # las mas largas salen recortadas por abajo.
        self._ajustar_alto()

    # -- paginas -----------------------------------------------------------
    def _pagina_teclas(self) -> None:
        K = self._tecla
        self._grid([
            ("Enter",     K(winapi.VK_RETURN, (), "Enter"),  "#2f6f9e", None),
            ("Tab",       K(winapi.VK_TAB, (), "Tab"),       "#3d4450", None),
            ("Esc",       K(winapi.VK_ESCAPE, (), "Esc"),    "#3d4450", None),
            ("Retroceso", K(winapi.VK_BACK, (), "Retroceso"), "#7a4040", winapi.VK_BACK),
            ("Supr",      K(winapi.VK_DELETE, (), "Supr"),   "#7a4040", winapi.VK_DELETE),
            # Espacio va por press(), NO por ui_insert_text(" "): _normalize
            # trata como vacio todo lo que sea solo blancos -guarda pensada para
            # replace()- y el boton no escribia nada, en silencio.
            ("Espacio",   K(winapi.VK_SPACE, (), "Espacio"), "#3d4450", None),
        ], columnas=3, alto_ipady=16)

    def _pagina_mover(self) -> None:
        K = self._tecla
        self._grid([
            ("←", K(winapi.VK_LEFT, (), "izquierda"),  "#3d4450", winapi.VK_LEFT),
            ("↑", K(winapi.VK_UP, (), "arriba"),        "#3d4450", winapi.VK_UP),
            ("↓", K(winapi.VK_DOWN, (), "abajo"),       "#3d4450", winapi.VK_DOWN),
            ("→", K(winapi.VK_RIGHT, (), "derecha"),    "#3d4450", winapi.VK_RIGHT),
            ("Inicio", K(winapi.VK_HOME, (), "Inicio"),      "#3d4450", None),
            ("Fin",    K(winapi.VK_END, (), "Fin"),          "#3d4450", None),
            ("Sel ←", K(winapi.VK_LEFT, SHIFT, "sel izq"),  "#4a5568", winapi.VK_LEFT),
            ("Sel →", K(winapi.VK_RIGHT, SHIFT, "sel der"), "#4a5568", winapi.VK_RIGHT),
            ("Sel palabra", K(winapi.VK_LEFT, CTRL_SHIFT, "sel palabra"), "#4a5568", None),
            ("Sel todo", K(winapi.VK_A, CTRL, "sel todo"),   "#4a5568", None),
            ("Copiar", K(winapi.VK_C, CTRL, "copiar"),       "#2f6f9e", None),
            ("Pegar",  K(winapi.VK_V, CTRL, "pegar"),        "#2f6f9e", None),
            ("Cortar", K(winapi.VK_X, CTRL, "cortar"),       "#2f6f9e", None),
            ("Deshacer", K(winapi.VK_Z, CTRL, "deshacer"),   "#7a5a30", None),
            ("Rehacer",  K(winapi.VK_Y, CTRL, "rehacer"),    "#7a5a30", None),
        ], columnas=4, alto_ipady=13, font=("Segoe UI", 10))

    def _pagina_simbolos(self) -> None:
        self._grid(
            [(s, (lambda t=s: self.app.ui_insert_text(t)), "#3d4450", None)
             for s in SIMBOLOS],
            columnas=6, alto_ipady=12, font=("Consolas", 13))

    def _pagina_opciones(self) -> None:
        pausado = getattr(self.app, "_paused", False)
        self._grid([
            ("Reanudar" if pausado else "Pausar", self._pausar, "#3d4450", None),
            ("Estado", self._estado, "#3d4450", None),
            ("Configuracion", self._config, "#3d4450", None),
            ("Salir de WISPI", self._salir, "#6b2f2f", None),
        ], columnas=2, alto_ipady=18)

    # -- acciones ----------------------------------------------------------
    def _tecla(self, vk: int, mods: tuple = (), etiqueta: str = ""):
        """Devuelve el callback que pulsa esa tecla. El panel NO se cierra: se
        encadenan varias (cinco retrocesos, tres flechas) sin reabrirlo.

        Acepta `gen` opcional para que la auto-repeticion pueda etiquetar cada
        pulsacion con su rafaga y descartarla si el dedo ya se levanto."""
        def _fn(gen: int | None = None):
            self.app.ui_press(vk, mods, 1, etiqueta, gen)
        return _fn

    def _empezar_repeticion(self, cmd) -> None:
        """Repite mientras el dedo aguanta, con token de generacion.

        El token es lo que impide el peor fallo del panel: el temporizador
        encolaba una pulsacion cada 55 ms y el worker las consumia mucho mas
        despacio, asi que soltar el dedo dejaba una cola de retrocesos que se
        seguian aplicando hasta 3 s DESPUES, ya en otra ventana. Medido: de 20
        disparos, 15 se aplicaban tras soltar. Cancelar el `after` de Tk no
        bastaba porque no cancela lo ya encolado en el executor.
        """
        self._armar_autocierre()
        gen = self.app.injector.bump_generation()
        self._gen_activa = gen
        cmd(gen)                    # la primera va inmediata, sin esperar

        def _rep():
            if self._gen_activa != gen:
                return              # se solto: no encolar mas
            cmd(gen)
            self._repeat_id = self.win.after(REPEAT_EVERY_MS, _rep)
        self._repeat_id = self.win.after(REPEAT_DELAY_MS, _rep)

    def _parar_repeticion(self) -> None:
        if self._repeat_id:
            try:
                self.win.after_cancel(self._repeat_id)
            except tk.TclError:
                pass
            self._repeat_id = None
        # Invalida de golpe todo lo que quedara encolado de esta rafaga.
        self._gen_activa = None
        try:
            self.app.injector.bump_generation()
        except Exception:
            pass

    def _dictar(self) -> None:
        self.cerrar()
        self.app.ui_toggle()

    def _pausar(self) -> None:
        self.app.toggle_pause()
        self._pintar()

    def _config(self) -> None:
        self.cerrar()
        self.ov.abrir_configuracion()

    def _estado(self) -> None:
        self.cerrar()
        self.ov._mostrar_estado()

    def _salir(self) -> None:
        self.cerrar()
        self.ov.salir()

    # -- colocacion y ciclo de vida ----------------------------------------
    def _alto_pagina(self) -> int:
        self.win.update_idletasks()
        return max(200, self.win.winfo_reqheight())

    def _colocar(self) -> None:
        """Junto al boton, volteando si no cabe. Un panel medio fuera de pantalla
        es un panel con botones inalcanzables."""
        alto = self._alto_pagina()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        bx, by, s = self.ov.root.winfo_x(), self.ov.root.winfo_y(), self.ov.size

        x = bx + s + 8 if bx + s + 8 + self.ANCHO <= sw else bx - self.ANCHO - 8
        x = max(4, min(x, sw - self.ANCHO - 4))
        y = by + s // 2 - alto // 2
        y = max(4, min(y, sh - alto - 4))
        self._x, self._y = int(x), int(y)
        self.win.geometry(f"{self.ANCHO}x{alto}+{self._x}+{self._y}")

    def _ajustar_alto(self) -> None:
        """Reajusta SOLO el alto al cambiar de pagina, anclando la esquina
        superior izquierda.

        Se mantiene el borde de arriba fijo a proposito: si se recentrara sobre
        el boton, las pestanas saltarian de sitio en cada cambio y habria que
        volver a buscarlas con el dedo. Asi solo se mueve el borde inferior.
        """
        if self._x is None:
            return   # todavia no hay primera colocacion; ya la hara _colocar()
        alto = self._alto_pagina()
        sh = self.win.winfo_screenheight()
        y = self._y
        if y + alto > sh - 4:           # la pagina larga se saldria por abajo
            y = max(4, sh - alto - 4)
        self.win.geometry(f"{self.ANCHO}x{alto}+{self._x}+{int(y)}")

    def _armar_autocierre(self) -> None:
        """Cada interaccion reinicia la cuenta: escribir con el panel no puede
        hacer que el panel desaparezca a media frase."""
        ms = int(getattr(self.app.cfg.ui, "panel_autoclose_ms", 8000) or 0)
        if self._cierre_id:
            try:
                self.win.after_cancel(self._cierre_id)
            except tk.TclError:
                pass
            self._cierre_id = None
        if ms > 0:
            self._cierre_id = self.win.after(ms, self.cerrar)

    def vivo(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def cerrar(self) -> None:
        self._parar_repeticion()
        if self._cierre_id:
            try:
                self.win.after_cancel(self._cierre_id)
            except tk.TclError:
                pass
            self._cierre_id = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.ov._panel = None


TouchPanel = KeyPanel   # nombre anterior, por si quedan referencias


class Overlay:
    def __init__(self, app) -> None:
        self.app = app
        self.cfg = app.cfg
        self.size = int(self.cfg.ui.overlay_size)
        self.root = tk.Tk()
        self.root.withdraw()          # no parpadear antes de estar colocado
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", float(self.cfg.ui.overlay_opacity))
        except tk.TclError:
            pass
        # El color de fondo se declara transparente: deja el circulo recortado
        # sobre el escritorio en vez de un cuadrado gris.
        self._chroma = "#010203"
        self.root.configure(bg=self._chroma)
        try:
            self.root.attributes("-transparentcolor", self._chroma)
        except tk.TclError:
            pass  # si el sistema no lo soporta, se ve el cuadrado y ya

        self.canvas = tk.Canvas(self.root, width=self.size, height=self.size,
                                highlightthickness=0, bd=0, bg=self._chroma)
        self.canvas.pack()

        self._estado = State.IDLE
        self._pulso = 0
        self._drag = False
        self._press = (0, 0)
        self._origen = (0, 0)
        self._hwnd = 0
        self._estilo_ok = False
        self._ultimo_tap = 0.0
        self._panel: "KeyPanel | None" = None
        # La pestana se recuerda entre aperturas: si estas escribiendo simbolos,
        # cada vez que reabres el panel lo quieres donde lo dejaste.
        self._ultima_pagina = "Teclas"
        self._settings = None

        self._colocar()
        self._bind()
        # ORDEN CRITICO: el estilo va ANTES de mostrar la ventana.
        # update_idletasks() crea el HWND sin mapear la ventana; ahi se marca
        # NOACTIVATE y solo entonces se muestra. Aplicarlo despues de deiconify()
        # -aunque sea 60 ms despues- deja una rendija en la que la ventana es
        # normal, Windows le da el primer plano al mapearla, y el boton arranca
        # habiendole robado el foco a lo que el usuario tuviera delante.
        previo = winapi.foreground_hwnd()
        self.root.update_idletasks()
        self._aplicar_estilo_win32()
        # ShowWindow(SW_SHOWNOACTIVATE) en vez de deiconify(): WS_EX_NOACTIVATE
        # impide que un CLIC active la ventana, pero no impide que el propio acto
        # de mapearla se lleve el primer plano.
        winapi.show_without_activating(self._hwnd)
        self.root.after(30, self._aplicar_estilo_win32)
        # Red de seguridad: si aun asi se llevo el foco, se devuelve. Perder el
        # foco al arrancar significaria que el primer dictado del usuario acaba
        # en el sitio equivocado.
        self.root.after(50, lambda: self._devolver_foco(previo))
        self.root.after(TICK_MS, self._tick)

    def _devolver_foco(self, previo: int) -> None:
        if not previo or previo == self._hwnd:
            return
        if winapi.foreground_hwnd() == self._hwnd:
            winapi.restore_foreground(previo)

    # -- construccion ------------------------------------------------------
    def _colocar(self) -> None:
        x, y = int(self.cfg.ui.overlay_x), int(self.cfg.ui.overlay_y)
        if x < 0 or y < 0:
            # Primera vez: abajo a la derecha, por encima de la barra de tareas.
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x, y = sw - self.size - 28, sh - self.size - 96
        x, y = self._dentro_de_pantalla(x, y)
        self.root.geometry(f"{self.size}x{self.size}+{x}+{y}")

    def _dentro_de_pantalla(self, x: int, y: int) -> tuple[int, int]:
        """Que no quede fuera de la pantalla: si el usuario cambia de monitor o de
        resolucion, un boton en x=3800 seria un boton perdido para siempre."""
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        return (max(0, min(x, sw - self.size)), max(0, min(y, sh - self.size)))

    def _aplicar_estilo_win32(self) -> None:
        hwnd = self.root.winfo_id()
        padre = winapi.user32.GetParent(hwnd)
        # Con overrideredirect Tk no envuelve la ventana, pero si alguna version lo
        # hiciera el estilo tiene que ir al toplevel real, no al hijo.
        self._hwnd = int(padre) if padre else int(hwnd)
        if not winapi.make_click_through_overlay(self._hwnd):
            log.warning("no pude aplicar WS_EX_NOACTIVATE; el boton robara el foco "
                        "y el dictado ira a parar a WISPI en vez de a tu documento")
        elif not self._estilo_ok:
            self._estilo_ok = True
            log.info("boton flotante listo (no roba el foco)")

    def _bind(self) -> None:
        for w in (self.root, self.canvas):
            w.bind("<ButtonPress-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_motion)
            w.bind("<ButtonRelease-1>", self._on_release)
            w.bind("<Button-3>", self._on_right)
            w.bind("<Enter>", lambda e: self._dibujar(hover=True))
            w.bind("<Leave>", lambda e: self._dibujar(hover=False))

    # -- gestos ------------------------------------------------------------
    def _on_press(self, ev) -> None:
        self._drag = False
        self._press = (ev.x_root, ev.y_root)
        self._origen = (self.root.winfo_x(), self.root.winfo_y())

    def _on_motion(self, ev) -> None:
        dx = ev.x_root - self._press[0]
        dy = ev.y_root - self._press[1]
        umbral = int(self.cfg.ui.drag_threshold_px)
        if not self._drag and (abs(dx) > umbral or abs(dy) > umbral):
            self._drag = True
        if self._drag:
            x, y = self._dentro_de_pantalla(self._origen[0] + dx, self._origen[1] + dy)
            self.root.geometry(f"+{x}+{y}")

    def _on_release(self, ev) -> None:
        if self._drag:
            self._guardar_posicion()
            self._ultimo_tap = 0.0      # un arrastre no cuenta como medio doble toque
            return

        ahora = time.monotonic()
        ventana = int(self.cfg.ui.double_tap_ms) / 1000.0
        doble = self._ultimo_tap and (ahora - self._ultimo_tap) <= ventana

        if doble:
            # DOBLE TOQUE -> panel.
            # El primer toque ya arranco el dictado, asi que hay que cortarlo:
            # dejarlo grabando mientras el usuario navega el menu seria absurdo.
            # No se pierde nada: esos ~300 ms de audio caen por debajo de
            # min_duration_s y el gate los descarta sin llegar al motor.
            self._ultimo_tap = 0.0
            self.app.ui_cancel()
            self.abrir_panel()
            return

        self._ultimo_tap = ahora

        # Con el panel abierto, un toque lo cierra en vez de dictar: es lo que
        # espera quien lo abrio sin querer.
        if self._panel and self._panel.vivo():
            self._panel.cerrar()
            self._ultimo_tap = 0.0
            return

        self.app.ui_toggle()

    def _on_right(self, ev) -> None:
        # El raton llega al mismo panel que el dedo: una sola interfaz que
        # mantener, y lo que aprendes con el dedo te sirve con el raton.
        self.abrir_panel()

    def abrir_panel(self) -> None:
        if self._panel and self._panel.vivo():
            self._panel.cerrar()
            return
        self._panel = KeyPanel(self)

    def _guardar_posicion(self) -> None:
        x, y = self.root.winfo_x(), self.root.winfo_y()
        try:
            self.cfg.write_overrides({"ui": {"overlay_x": x, "overlay_y": y}})
        except Exception as e:
            log.warning("no pude guardar la posicion del boton: %s", e)

    # -- acciones del menu -------------------------------------------------
    def abrir_configuracion(self) -> None:
        from .settings_ui import SettingsWindow
        if self._settings and self._settings.vivo():
            self._settings.al_frente()
            return
        self._settings = SettingsWindow(self.root, self.app)

    def _pausar(self) -> None:
        self.app.toggle_pause()

    def _mostrar_estado(self) -> None:
        from tkinter import messagebox
        st = self.app.status()
        asr = st.get("asr") or {}
        hook = st.get("hook") or {}
        p99 = hook.get("p99_us")
        lineas = [
            f"Estado: {st['state']}" + ("  (EN PAUSA)" if st["paused"] else ""),
            "",
            f"Modelo: {asr.get('model','?')} {asr.get('device','?')}/{asr.get('compute_type','?')}",
            f"Microfono: {(st.get('audio') or {}).get('name', '?')}",
            "",
            f"Callback del hook p99: {p99 if p99 is not None else 'sin datos'} us",
            "  (Windows desengancha el hook por encima de 300000 us)",
        ]
        if st.get("audio_error"):
            lineas += ["", f"AUDIO: {st['audio_error']}"]
        for w in st.get("asr_warnings") or []:
            lineas += ["", f"ASR: {w}"]
        messagebox.showinfo("WISPI - estado", "\n".join(lineas), parent=self.root)

    def _abrir_logs(self) -> None:
        import os
        from .metrics import LOG_DIR
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(LOG_DIR))  # noqa: S606
        except Exception as e:
            log.warning("no pude abrir la carpeta de logs: %s", e)

    def salir(self) -> None:
        try:
            self.app.stop()
        finally:
            if self._panel and self._panel.vivo():
                self._panel.cerrar()
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    # -- pintado -----------------------------------------------------------
    def _dibujar(self, hover: bool = False) -> None:
        c = self.canvas
        c.delete("all")
        s = self.size
        color, _ = COLORES.get(self._estado, COLORES[State.IDLE])
        grabando = self._estado in (State.RECORDING, State.HANDS_FREE)
        trabajando = self._estado in (State.TRANSCRIBING, State.POLISHING)

        # Anillo de pulso mientras graba: comunica "te estoy oyendo" sin texto.
        if grabando:
            r = 2 + (self._pulso % 8)
            c.create_oval(r, r, s - r, s - r, outline=color, width=2)

        m = 6 if not hover else 4       # el circulo crece un pelin al pasar por encima
        c.create_oval(m, m, s - m, s - m, fill=color, outline="")

        # Microfono dibujado a mano: una capsula, el arco del soporte y el pie.
        # Con primitivas y no con un emoji, porque el emoji depende de la fuente
        # instalada y se ve distinto en cada maquina.
        cx = s / 2
        cw, ch = s * 0.17, s * 0.27
        top = s * 0.26
        c.create_oval(cx - cw, top, cx + cw, top + ch, fill="white", outline="")
        c.create_rectangle(cx - cw, top + ch * 0.4, cx + cw, top + ch, fill="white",
                           outline="")
        arco_r = s * 0.24
        c.create_arc(cx - arco_r, top + ch * 0.28, cx + arco_r, top + ch * 0.28 + arco_r * 2,
                     start=200, extent=140, style="arc", outline="white",
                     width=max(2, int(s * 0.045)))
        pie_y = top + ch * 0.28 + arco_r
        c.create_line(cx, pie_y, cx, pie_y + s * 0.11, fill="white",
                      width=max(2, int(s * 0.045)))

        if trabajando:
            # Tres puntos girando: distingue "pensando" de "escuchando".
            import math
            for i in range(3):
                ang = math.radians(self._pulso * 12 + i * 120)
                px = cx + math.cos(ang) * s * 0.33
                py = s / 2 + math.sin(ang) * s * 0.33
                c.create_oval(px - 2, py - 2, px + 2, py + 2, fill="white", outline="")

    # -- bucle -------------------------------------------------------------
    def _tick(self) -> None:
        estado = self.app.state
        if estado != self._estado:
            self._estado = estado
            self._pulso = 0
        if self._estado in (State.RECORDING, State.HANDS_FREE,
                            State.TRANSCRIBING, State.POLISHING):
            self._pulso += 1
        self._dibujar()
        # Reafirmar el topmost: hay apps (juegos, presentaciones a pantalla
        # completa) que se ponen por encima y dejarian el boton enterrado.
        if self._pulso % 40 == 0 and self._hwnd:
            winapi.keep_on_top(self._hwnd)
        self.root.after(TICK_MS, self._tick)

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run_overlay(app) -> int:
    """Arranca la app y le pone el boton flotante. Bloquea hasta que se sale."""
    app.start()
    ov = Overlay(app)

    if app.cfg.ui.tray_enabled:
        # La bandeja va en modo detached porque Tk ya tiene el hilo principal.
        # Si no esta disponible, no pasa nada: el menu del boton hace lo mismo.
        try:
            from .tray import Tray
            tray = Tray(app)
            tray.run_detached()
        except Exception as e:
            log.warning("bandeja no disponible (%s); se usa solo el boton", e)

    return ov.run()
