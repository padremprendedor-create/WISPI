"""Ventana de configuracion de WISPI.

DOS REGLAS QUE EXPLICAN COMO ESTA HECHA

1. NO ESCRIBE EN config.yaml. PyYAML no preserva comentarios, y config.yaml es
   mitad valores mitad explicaciones de por que cada valor es el que es. La
   primera vez que esta ventana guardara ahi, se llevaria por delante la parte
   cara del fichero. Escribe en config.local.yaml, que se aplica encima.

2. DICE LO QUE SE APLICA AL VUELO Y LO QUE NO. Casi todo entra solo (el hilo de
   estado hace poll de mtime cada segundo), pero cambiar de modelo obliga a
   recargar el motor y eso tarda un par de segundos. Mentir sobre eso hace que el
   usuario cambie algo, no vea efecto, y desconfie de toda la ventana.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from . import logging_setup
from .config import Config

log = logging_setup.get("settings")

# Modelos que ya estan en disco, con lo que costo medirlos en esta maquina.
MODELOS = [
    ("tiny",     "tiny - 0,21 s - el mas rapido, calidad justa"),
    ("base",     "base - 0,36 s - rapido, calidad aceptable"),
    ("small",    "small - 1,24 s - RECOMENDADO, el equilibrio"),
    ("large-v3", "large-v3 - 6,26 s - NO usable en CPU (medido)"),
]

RUTAS = [
    ("auto",         "Automatico segun la aplicacion (recomendado)"),
    ("ctrl_v",       "Siempre Ctrl+V"),
    ("shift_insert", "Siempre Shift+Insert (terminales)"),
    ("unicode",      "Teclear letra a letra (no toca el portapapeles)"),
    ("none",         "Solo copiar al portapapeles, no pegar"),
]


class SettingsWindow:
    def __init__(self, parent: tk.Misc, app) -> None:
        self.app = app
        self.cfg: Config = app.cfg
        self.win = tk.Toplevel(parent)
        self.win.title("WISPI - configuracion")
        self.win.geometry("620x560")
        self.win.minsize(560, 480)
        # Esta SI toma el foco, al reves que el boton flotante: es una ventana de
        # verdad y el usuario va a escribir en ella.
        self.win.transient(parent)
        self.win.lift()
        self.win.focus_force()

        self._vars: dict[str, tk.Variable] = {}
        self._pendiente_recarga = False

        nb = ttk.Notebook(self.win)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        self._tab_general(nb)
        self._tab_wake(nb)
        self._tab_motor(nb)
        self._tab_limpieza(nb)
        self._tab_escritura(nb)
        self._tab_jerga(nb)

        barra = ttk.Frame(self.win)
        barra.pack(fill="x", padx=10, pady=10)
        self.estado = ttk.Label(barra, text="", foreground="#0a7f3f")
        self.estado.pack(side="left")
        ttk.Button(barra, text="Cerrar", command=self.win.destroy).pack(side="right")
        ttk.Button(barra, text="Guardar", command=self.guardar).pack(side="right", padx=6)
        ttk.Button(barra, text="Volver a los valores por defecto",
                   command=self.restablecer).pack(side="right", padx=6)

    # -- utilidades de construccion ---------------------------------------
    def _fila(self, padre, fila: int, etiqueta: str, ayuda: str = "") -> ttk.Frame:
        ttk.Label(padre, text=etiqueta).grid(row=fila, column=0, sticky="w", pady=(8, 0))
        cont = ttk.Frame(padre)
        cont.grid(row=fila, column=1, sticky="ew", pady=(8, 0))
        if ayuda:
            ttk.Label(padre, text=ayuda, foreground="#6e7681",
                      wraplength=560, justify="left").grid(
                row=fila + 1, column=0, columnspan=2, sticky="w")
        return cont

    def _pagina(self, nb: ttk.Notebook, titulo: str) -> ttk.Frame:
        f = ttk.Frame(nb, padding=14)
        f.columnconfigure(1, weight=1)
        nb.add(f, text=titulo)
        return f

    def _var(self, clave: str, valor) -> tk.Variable:
        v = (tk.BooleanVar if isinstance(valor, bool) else
             tk.DoubleVar if isinstance(valor, float) else
             tk.IntVar if isinstance(valor, int) else tk.StringVar)(value=valor)
        self._vars[clave] = v
        return v

    # -- pestanas ----------------------------------------------------------
    def _tab_general(self, nb) -> None:
        f = self._pagina(nb, "General")
        r = 0

        c = self._fila(f, r, "Boton flotante",
                       "Clic para dictar, arrastra para moverlo. No roba el foco, "
                       "asi que el texto va a donde estabas escribiendo.")
        ttk.Checkbutton(c, text="Mostrar", variable=self._var(
            "ui.overlay_enabled", self.cfg.ui.overlay_enabled)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Tamano del boton", "Pixeles. Entre 40 y 90.")
        ttk.Spinbox(c, from_=40, to=90, width=6, textvariable=self._var(
            "ui.overlay_size", self.cfg.ui.overlay_size)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Opacidad", "1.0 = opaco.")
        ttk.Spinbox(c, from_=0.3, to=1.0, increment=0.05, width=6, format="%.2f",
                    textvariable=self._var("ui.overlay_opacity",
                                           self.cfg.ui.overlay_opacity)).pack(side="left")
        r += 2

        ttk.Separator(f, orient="horizontal").grid(row=r, column=0, columnspan=2,
                                                   sticky="ew", pady=12)
        r += 1
        ttk.Label(f, text="Tactil", font=("", 9, "bold")).grid(row=r, column=0, sticky="w")
        r += 1
        ttk.Label(f, text="Un toque dicta. Doble toque abre el panel de opciones con "
                          "botones grandes, sin necesidad de raton.",
                  foreground="#6e7681", wraplength=560, justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(0, 4))
        r += 1

        c = self._fila(f, r, "Ventana del doble toque (ms)",
                       "Tiempo maximo entre los dos toques. Subelo si te cuesta "
                       "encadenarlos; bajalo si el panel te sale sin querer. El "
                       "primer toque dicta igual: si llega el segundo, se cancela "
                       "y se abre el panel.")
        ttk.Spinbox(c, from_=150, to=900, increment=25, width=6, textvariable=self._var(
            "ui.double_tap_ms", self.cfg.ui.double_tap_ms)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Tolerancia del dedo (px)",
                       "Cuanto puede temblar el dedo sin que el toque cuente como "
                       "arrastre. Si a veces tocas y no arranca el dictado, subelo.")
        ttk.Spinbox(c, from_=2, to=40, width=6, textvariable=self._var(
            "ui.drag_threshold_px", self.cfg.ui.drag_threshold_px)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Cerrar el panel solo (ms)",
                       "Si no lo tocas, se cierra pasado este tiempo. 0 = nunca.")
        ttk.Spinbox(c, from_=0, to=30000, increment=1000, width=8, textvariable=self._var(
            "ui.panel_autoclose_ms", self.cfg.ui.panel_autoclose_ms)).pack(side="left")
        r += 2

        ttk.Separator(f, orient="horizontal").grid(row=r, column=0, columnspan=2,
                                                   sticky="ew", pady=12)
        r += 1

        c = self._fila(f, r, "Sonidos",
                       "Un tono al empezar y otro al terminar. Suena decenas de veces "
                       "al dia: si molesta, quitalo o bajalo.")
        ttk.Checkbutton(c, text="Activados", variable=self._var(
            "ui.chime_enabled", self.cfg.ui.chime_enabled)).pack(side="left")
        ttk.Spinbox(c, from_=0.0, to=1.0, increment=0.05, width=6, format="%.2f",
                    textvariable=self._var("ui.chime_volume",
                                           self.cfg.ui.chime_volume)).pack(side="left", padx=10)
        r += 2

        c = self._fila(f, r, "Icono en la bandeja", "Ademas del boton flotante.")
        ttk.Checkbutton(c, text="Mostrar", variable=self._var(
            "ui.tray_enabled", self.cfg.ui.tray_enabled)).pack(side="left")
        r += 2

        ttk.Separator(f, orient="horizontal").grid(row=r, column=0, columnspan=2,
                                                   sticky="ew", pady=14)
        r += 1
        ttk.Label(f, text="Atajo de teclado", font=("", 9, "bold")).grid(
            row=r, column=0, sticky="w")
        r += 1
        ttk.Label(f, text=f"Actual: {'+'.join(self.cfg.hotkey.combo).upper()}  "
                          f"(manten pulsado para dictar, doble toque para manos libres, "
                          f"Esc para cancelar)",
                  wraplength=560, justify="left").grid(row=r, column=0, columnspan=2,
                                                       sticky="w", pady=(4, 0))
        r += 1
        ttk.Label(f, text="Cambiarlo se hace a mano en config.yaml. Aviso para teclado "
                          "espanol: no uses Alt derecho (es AltGr y romperia @ # [ ] \\), "
                          "ni Ctrl+Alt+tecla, que Windows trata igual que AltGr.",
                  foreground="#a05a00", wraplength=560, justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _tab_wake(self, nb) -> None:
        f = self._pagina(nb, "Palabra clave")
        r = 0

        ttk.Label(f, text='Decir "hey WISPI" arranca el dictado sin tocar nada.',
                  font=("", 9, "bold")).grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1
        ttk.Label(f, text="Entra en manos libres: hablas y corta solo cuando callas. "
                          "No sustituye a Ctrl+Win, que sigue siendo el camino rapido; "
                          "esto es para cuando no tienes las manos en el teclado.",
                  foreground="#6e7681", wraplength=560, justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(2, 8))
        r += 1

        c = self._fila(f, r, "Escuchar la palabra clave",
                       "Mientras esta encendido, WISPI analiza el microfono EN TU "
                       "MAQUINA, sin salir a internet, para oir la frase. Solo escucha "
                       "cuando no esta haciendo nada: mientras dicta, transcribe o esta "
                       "en pausa, esta sordo.")
        ttk.Checkbutton(c, text="Encendida", variable=self._var(
            "wake.enabled", self.cfg.wake.enabled)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Sensibilidad de la frase",
                       "Parecido minimo con la frase entera. Bajalo si no te reconoce; "
                       "subelo si se activa sola. Entre 0,65 y 0,85.")
        ttk.Spinbox(c, from_=0.50, to=0.95, increment=0.01, width=6, format="%.2f",
                    textvariable=self._var("wake.threshold",
                                           self.cfg.wake.threshold)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Sensibilidad del nombre",
                       "Igual pero mirando solo 'wispi'. Este es el que evita que "
                       "'hey wifi' cuente como activacion, asi que bajarlo de 0,65 "
                       "abre falsos positivos.")
        ttk.Spinbox(c, from_=0.50, to=0.95, increment=0.01, width=6, format="%.2f",
                    textvariable=self._var("wake.name_threshold",
                                           self.cfg.wake.name_threshold)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Margen para empezar a hablar (s)",
                       "Tras el tono, cuanto puedes tardar en arrancar antes de que "
                       "lo de por terminado.")
        ttk.Spinbox(c, from_=0.3, to=6.0, increment=0.1, width=6, format="%.1f",
                    textvariable=self._var("wake.start_grace_s",
                                           self.cfg.wake.start_grace_s)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Espera entre activaciones (s)",
                       "Tras despertar, ignora la frase durante este rato.")
        ttk.Spinbox(c, from_=0.0, to=10.0, increment=0.5, width=6, format="%.1f",
                    textvariable=self._var("wake.cooldown_s",
                                           self.cfg.wake.cooldown_s)).pack(side="left")
        r += 2

        ttk.Label(f, text="Frases que despiertan", font=("", 9, "bold")).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(14, 4))
        r += 1
        ttk.Label(f, text="Una por linea. Son FORMAS, no ortografias exactas: la "
                          "comparacion es por parecido, porque 'wispi' no es una "
                          "palabra espanola y el modelo la escribe distinta cada vez "
                          "(Wispy, Guispi, Vispi). No hace falta anadir variantes.",
                  foreground="#6e7681", wraplength=560, justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w")
        r += 1
        self.txt_frases = tk.Text(f, height=5, width=44)
        self.txt_frases.insert("1.0", "\n".join(self.cfg.wake.phrases))
        self.txt_frases.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        r += 1

        ttk.Label(f, text="Para calibrarla viendo lo que oye de verdad:\n"
                          "   uv run python -m wispi.selftest --wake",
                  foreground="#6e7681", wraplength=560, justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(10, 0))
        r += 1
        ttk.Label(f, text="Encenderla la primera vez puede tardar unos segundos: carga "
                          "un modelo pequeno aparte. Si no lo tienes en disco, mira el "
                          "log; WISPI te dira que hacer y Ctrl+Win seguira igual.",
                  foreground="#a05a00", wraplength=560, justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _tab_motor(self, nb) -> None:
        f = self._pagina(nb, "Motor de voz")
        r = 0

        c = self._fila(f, r, "Modelo",
                       "Tiempos MEDIDOS en esta maquina (i9-10850K, int8). El coste es "
                       "FIJO por dictado, no proporcional a lo que hables: Whisper "
                       "rellena a 30 s y corre el encoder entero igual.")
        self._var("asr.model", self.cfg.asr.model)
        cb = ttk.Combobox(c, width=46, state="readonly",
                          values=[d for _, d in MODELOS])
        actual = next((d for m, d in MODELOS if m == self.cfg.asr.model), MODELOS[2][1])
        cb.set(actual)
        cb.pack(side="left")
        self._cb_modelo = cb
        r += 2

        c = self._fila(f, r, "Idioma", "Codigo ISO. 'es' para espanol.")
        ttk.Entry(c, width=8, textvariable=self._var(
            "asr.language", self.cfg.asr.language)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Hilos de CPU",
                       "Nucleos fisicos (10 en esta maquina). Poner 20 con "
                       "HyperThreading empeora el calculo en int8.")
        ttk.Spinbox(c, from_=1, to=32, width=6, textvariable=self._var(
            "asr.cpu_threads", self.cfg.asr.cpu_threads)).pack(side="left")
        r += 2

        ttk.Separator(f, orient="horizontal").grid(row=r, column=0, columnspan=2,
                                                   sticky="ew", pady=14)
        r += 1

        c = self._fila(f, r, "Umbral de silencio",
                       "RMS. Por debajo de esto el audio se descarta SIN llamar al "
                       "motor: es lo que evita que Whisper se invente frases sobre "
                       "ruido de fondo. Subelo si tu sala es ruidosa.")
        ttk.Spinbox(c, from_=0.001, to=0.100, increment=0.001, width=8, format="%.3f",
                    textvariable=self._var("audio.silence_threshold",
                                           self.cfg.audio.silence_threshold)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Silencio para cortar (s)",
                       "Solo en manos libres: cuanto callas para que dé por terminado.")
        ttk.Spinbox(c, from_=0.3, to=5.0, increment=0.1, width=6, format="%.1f",
                    textvariable=self._var("audio.silence_duration_s",
                                           self.cfg.audio.silence_duration_s)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Cola tras soltar (ms)",
                       "Audio que se sigue grabando al soltar la tecla, para no cortar "
                       "la ultima silaba. Sale directo del presupuesto de latencia.")
        ttk.Spinbox(c, from_=0, to=600, increment=25, width=6, textvariable=self._var(
            "audio.tail_ms", self.cfg.audio.tail_ms)).pack(side="left")
        r += 2

        ttk.Button(f, text="Aplicar y recargar el motor",
                   command=self.recargar_motor).grid(row=r, column=0, sticky="w", pady=(16, 0))
        ttk.Label(f, text="Cambiar de modelo o de hilos exige recargar. Tarda 1-2 s.",
                  foreground="#6e7681").grid(row=r, column=1, sticky="w", pady=(16, 0))

    def _tab_limpieza(self, nb) -> None:
        f = self._pagina(nb, "Limpieza")
        r = 0

        c = self._fila(f, r, "Quitar muletillas",
                       "Reglas deterministas, 0,03 ms. Las que no son palabras "
                       "('eh', 'o sea') caen siempre; las que si lo son ('pues', "
                       "'este', 'digamos') solo cuando la puntuacion las delata, para "
                       "no comerse una palabra buena.")
        ttk.Checkbutton(c, text="Activado", variable=self._var(
            "postprocess.strip_fillers", self.cfg.postprocess.strip_fillers)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Aplicar el diccionario",
                       "Corrige la jerga tecnica que Whisper destroza.")
        ttk.Checkbutton(c, text="Activado", variable=self._var(
            "postprocess.apply_dictionary",
            self.cfg.postprocess.apply_dictionary)).pack(side="left")
        r += 2

        ttk.Separator(f, orient="horizontal").grid(row=r, column=0, columnspan=2,
                                                   sticky="ew", pady=14)
        r += 1

        c = self._fila(f, r, "Pulir con IA local",
                       "Ollama en tu maquina, sin salir a internet. Solo se dispara en "
                       "dictados largos. El texto en crudo se pega YA y la version "
                       "limpia lo sustituye despues, asi no esperas.")
        ttk.Checkbutton(c, text="Activado", variable=self._var(
            "llm.enabled", self.cfg.llm.enabled)).pack(side="left")
        r += 2

        c = self._fila(f, r, "A partir de cuantas palabras",
                       "Por debajo de esto no merece la pena: no hay nada que "
                       "reestructurar y el riesgo de que el modelo invente es mayor "
                       "que la ganancia.")
        ttk.Spinbox(c, from_=5, to=200, width=6, textvariable=self._var(
            "llm.min_words", self.cfg.llm.min_words)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Modelo de Ollama", "Tiene que estar ya descargado.")
        ttk.Entry(c, width=28, textvariable=self._var(
            "llm.model", self.cfg.llm.model)).pack(side="left")
        r += 2

        c = self._fila(f, r, "Tiempo maximo (s)",
                       "Si tarda mas, se queda el texto en crudo. Nunca te deja esperando.")
        ttk.Spinbox(c, from_=0.5, to=15.0, increment=0.5, width=6, format="%.1f",
                    textvariable=self._var("llm.timeout_s",
                                           self.cfg.llm.timeout_s)).pack(side="left")

    def _tab_escritura(self, nb) -> None:
        f = self._pagina(nb, "Escritura")
        r = 0

        c = self._fila(f, r, "Como pega el texto",
                       "'Automatico' usa Ctrl+V en general y Shift+Insert en "
                       "terminales, donde Ctrl+V no funciona. Si una aplicacion se "
                       "resiste, prueba 'letra a letra'.")
        self._var("injection.route", self.cfg.injection.route)
        cb = ttk.Combobox(c, width=46, state="readonly", values=[d for _, d in RUTAS])
        cb.set(next((d for k, d in RUTAS if k == self.cfg.injection.route), RUTAS[0][1]))
        cb.pack(side="left")
        self._cb_ruta = cb
        r += 2

        c = self._fila(f, r, "Devolver el portapapeles",
                       "WISPI usa el portapapeles para pegar y despues lo deja como "
                       "estaba. Aviso: los gestores de historial (Win+V, Ditto) veran "
                       "el texto de todas formas; si eso te molesta, usa 'letra a "
                       "letra', que no toca el portapapeles.")
        ttk.Checkbutton(c, text="Si", variable=self._var(
            "injection.restore_clipboard",
            self.cfg.injection.restore_clipboard)).pack(side="left")
        r += 2

        ttk.Label(f, text="Aplicaciones tratadas como terminal", font=("", 9, "bold")).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(14, 4))
        r += 1
        ttk.Label(f, text="Una por linea. En estas se usa Shift+Insert y NUNCA se "
                          "reescribe el texto despues, porque seleccionar hacia atras "
                          "en una linea de comandos es una forma excelente de ejecutar "
                          "medio comando.",
                  foreground="#6e7681", wraplength=560, justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w")
        r += 1
        self.txt_terminales = tk.Text(f, height=7, width=44)
        self.txt_terminales.insert("1.0", "\n".join(self.cfg.injection.terminal_apps))
        self.txt_terminales.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def _tab_jerga(self, nb) -> None:
        f = self._pagina(nb, "Jerga")
        f.rowconfigure(2, weight=1)
        d = self.app.post.dictionary
        ttk.Label(f, text=f"{d.n_terms} terminos y {d.n_variants} variantes cargados.",
                  font=("", 9, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Se recarga solo al guardar dictionary.yaml, sin reiniciar. "
                          "Anadir un termino aqui lo agrega al final del fichero sin "
                          "tocar el resto.",
                  foreground="#6e7681", wraplength=560, justify="left").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 8))

        marco = ttk.Frame(f)
        marco.grid(row=2, column=0, columnspan=2, sticky="nsew")
        marco.rowconfigure(0, weight=1)
        marco.columnconfigure(0, weight=1)
        sb = ttk.Scrollbar(marco)
        sb.grid(row=0, column=1, sticky="ns")
        self.lista = tk.Listbox(marco, yscrollcommand=sb.set)
        self.lista.grid(row=0, column=0, sticky="nsew")
        sb.config(command=self.lista.yview)
        for t in d.canonicals:
            self.lista.insert("end", t)

        alta = ttk.LabelFrame(f, text="Anadir termino", padding=8)
        alta.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        alta.columnconfigure(1, weight=1)
        ttk.Label(alta, text="Como debe escribirse:").grid(row=0, column=0, sticky="w")
        self.e_canon = ttk.Entry(alta)
        self.e_canon.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(alta, text="Como lo oye mal (separado por comas):").grid(
            row=1, column=0, sticky="w", pady=(6, 0))
        self.e_var = ttk.Entry(alta)
        self.e_var.grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(alta, text="Anadir", command=self.anadir_termino).grid(
            row=2, column=1, sticky="e", pady=(8, 0))
        ttk.Label(alta, text="Cuidado: no pongas como variante una palabra espanola "
                             "corriente. Comerse una palabra buena es peor que dejar "
                             "un termino mal escrito, porque no lo ves venir.",
                  foreground="#a05a00", wraplength=520, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Button(f, text="Abrir dictionary.yaml", command=self._abrir_dict).grid(
            row=4, column=0, sticky="w", pady=(10, 0))

    # -- acciones ----------------------------------------------------------
    def _abrir_dict(self) -> None:
        import os
        from .postprocess.dictionary import DICT_PATH
        try:
            os.startfile(str(DICT_PATH))  # noqa: S606
        except Exception as e:
            messagebox.showerror("WISPI", f"No pude abrirlo: {e}", parent=self.win)

    def anadir_termino(self) -> None:
        from .postprocess.dictionary import DICT_PATH
        canon = self.e_canon.get().strip()
        variantes = [v.strip().lower() for v in self.e_var.get().split(",") if v.strip()]
        if not canon:
            messagebox.showwarning("WISPI", "Escribe como debe quedar el termino.",
                                   parent=self.win)
            return
        # Se anade como TEXTO al final del fichero, no reserializando el YAML:
        # asi se conservan todos los comentarios que documentan las reglas.
        bloque = f'\n  - canonical: "{canon}"\n'
        if variantes:
            lista = ", ".join(f'"{v}"' for v in variantes)
            bloque += f"    variants: [{lista}]\n"
        try:
            with DICT_PATH.open("a", encoding="utf-8") as fh:
                fh.write(bloque)
        except OSError as e:
            messagebox.showerror("WISPI", f"No pude escribir: {e}", parent=self.win)
            return
        self.lista.insert("end", canon)
        self.e_canon.delete(0, "end")
        self.e_var.delete(0, "end")
        self._decir(f"'{canon}' anadido. Se recarga solo en unos segundos.")

    def recargar_motor(self) -> None:
        self.guardar(silencioso=True)
        self._decir("Recargando el motor...")
        self.win.update_idletasks()
        try:
            ok, msg = self.app.reload_asr()
        except Exception as e:
            messagebox.showerror("WISPI", f"No pude recargar: {e}", parent=self.win)
            return
        if ok:
            self._decir(msg)
        else:
            messagebox.showerror("WISPI", msg, parent=self.win)

    def _recoger(self) -> dict[str, dict]:
        cambios: dict[str, dict] = {}
        for clave, var in self._vars.items():
            seccion, nombre = clave.split(".", 1)
            try:
                cambios.setdefault(seccion, {})[nombre] = var.get()
            except tk.TclError:
                continue  # campo a medio escribir: se ignora en vez de romper
        # Los combos guardan la descripcion larga; hay que traducir a la clave.
        desc = self._cb_modelo.get()
        cambios.setdefault("asr", {})["model"] = next(
            (m for m, d in MODELOS if d == desc), self.cfg.asr.model)
        desc = self._cb_ruta.get()
        cambios.setdefault("injection", {})["route"] = next(
            (k for k, d in RUTAS if d == desc), self.cfg.injection.route)
        frases = [l.strip().lower() for l in
                  self.txt_frases.get("1.0", "end").splitlines() if l.strip()]
        # Si el usuario vacia la caja NO se guarda una lista vacia: eso dejaria el
        # detector encendido y sordo, que es la peor combinacion posible (gasta y
        # no responde). Se queda con las frases que ya habia.
        if frases:
            cambios.setdefault("wake", {})["phrases"] = frases

        apps = [l.strip().lower() for l in
                self.txt_terminales.get("1.0", "end").splitlines() if l.strip()]
        if apps:
            cambios.setdefault("injection", {})["terminal_apps"] = apps
            # Sin parche en terminal: seleccionar hacia atras en una linea de
            # comandos puede ejecutar medio comando. Se mantienen sincronizadas.
            cambios["injection"]["no_patch_apps"] = apps
        return cambios

    def guardar(self, silencioso: bool = False) -> None:
        try:
            self.cfg.write_overrides(self._recoger())
        except Exception as e:
            messagebox.showerror("WISPI", f"No pude guardar: {e}", parent=self.win)
            return
        if not silencioso:
            self._decir("Guardado. Se aplica en unos segundos.")
        log.info("configuracion guardada desde la interfaz")

    def restablecer(self) -> None:
        if not messagebox.askyesno(
                "WISPI",
                "Se borran TUS cambios y vuelve a mandar config.yaml.\n\n"
                "El diccionario y la posicion del boton no se tocan.",
                parent=self.win):
            return
        try:
            overrides = self.cfg.read_overrides()
            pos = {k: v for k, v in (overrides.get("ui") or {}).items()
                   if k in ("overlay_x", "overlay_y")}
            self.cfg.local_path.unlink(missing_ok=True)
            if pos:
                self.cfg.write_overrides({"ui": pos})
        except Exception as e:
            messagebox.showerror("WISPI", f"No pude restablecer: {e}", parent=self.win)
            return
        self._decir("Restablecido. Cierra y vuelve a abrir esta ventana.")

    def _decir(self, txt: str) -> None:
        self.estado.config(text=txt)
        self.win.after(6000, lambda: self.estado.config(text=""))

    # -- ciclo de vida -----------------------------------------------------
    def vivo(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def al_frente(self) -> None:
        self.win.lift()
        self.win.focus_force()
