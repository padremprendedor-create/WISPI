"""Maquina de estados de WISPI. EL UNICO sitio con logica de orquestacion.

Todo lo demas son piezas tontas: el hook observa teclas, el audio acumula
muestras, el ASR transcribe, el injector escribe. Quien decide QUE pasa y CUANDO
es este archivo. Si hay que entender el comportamiento del programa, se lee aqui.

## POR QUE THREADING PURO Y NO ASYNCIO
Los dos componentes obligatorios de Windows -el hook de teclado y el icono de
bandeja- exigen message loops nativos (GetMessageW), que un event loop de
asyncio no puede pumpear; habria que puentear con call_soon_threadsafe desde
hilos, o sea, el overhead que asyncio venia a ahorrar. La unica E/S de verdad
asincrona es UNA llamada HTTP a Ollama, que un httpx.Client sincrono en un
worker resuelve en cinco lineas. Todo lo demas es CPU-bound. Un proyecto
anterior con esta misma forma acabo envolviendo todo en run_in_executor, que
es threading con ceremonia encima. Aqui se salta la ceremonia.

## MAPA DE HILOS
  MainThread    -> bandeja (pystray) o espera en modo consola
  wispi-hook    -> SetWindowsHookExW + GetMessageW. Callback < 0.5 ms.
  PortAudio cb  -> ring de pre-roll + acumulacion
  wispi-wake    -> palabra de activacion, SOLO si wake.enabled. Ver wake.py.
  wispi-state   -> ESTE bucle. Consume eventos, decide, despacha.
  wispi-asr     -> ThreadPoolExecutor(1). Bloqueante.
  wispi-llm     -> ThreadPoolExecutor(1). I/O.
  wispi-inject  -> ThreadPoolExecutor(1). Serializa: nunca dos escrituras a la vez.

## POR QUE UN SOLO WORKER POR EXECUTOR
No es por ahorrar. `ASRBackend.transcribe` esta documentado como seguro para UN
llamador, y dos inyecciones simultaneas se pisarian el portapapeles y el foco.
El max_workers=1 es lo que HACE CIERTO ese contrato.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from . import logging_setup
from .audio import AudioCapture
from .config import Config
from .events import Dictation, HookEvent, State
from .feedback import Feedback
from .hotkey import KeyboardHook
from .inject.injector import Injector
from .inject.target import Target, current_target
from .metrics import DictationTrace, Metrics, now_ns
from .postprocess.hallucinations import is_whisper_hallucination
from .postprocess.pipeline import PostProcessor
from .wake import WakeWord
from .asr.registry import build_with_fallback

log = logging_setup.get("app")

POLL_S = 0.05          # timeout del get(); no anade latencia (get() retorna al llegar el evento)
PERIODIC_S = 1.0       # cadencia del trabajo periodico (hot-reload, watchdog)
NS_PER_MS = 1_000_000


class WispiApp:
    def __init__(self, cfg: Config, console: bool = False) -> None:
        self.cfg = cfg
        self.console = console
        self.state = State.IDLE
        self._running = threading.Event()
        self._paused = False

        self.events_q: queue.SimpleQueue = queue.SimpleQueue()
        self.jobs_q: queue.SimpleQueue = queue.SimpleQueue()
        self.metrics = Metrics(enabled=cfg.logging.latency_jsonl)

        self.audio = AudioCapture(cfg.audio, logging_setup.get("audio"))
        self.hook = KeyboardHook(cfg.hotkey, self.events_q, self.metrics,
                                 logging_setup.get("hotkey"))
        self.post = PostProcessor(cfg.postprocess, cfg.llm, logging_setup.get("post"))
        self.injector = Injector(cfg.injection, logging_setup.get("inject"))
        self.fb = Feedback(cfg.ui.chime_enabled, cfg.ui.chime_volume)
        self.wake = WakeWord(cfg.wake, cfg.asr, self.audio, self.events_q,
                             logging_setup.get("wake"),
                             log_text=cfg.logging.include_text)
        self.asr = None
        self.asr_warnings: list[str] = []

        self.ex_asr = ThreadPoolExecutor(1, thread_name_prefix="wispi-asr")
        self.ex_llm = ThreadPoolExecutor(1, thread_name_prefix="wispi-llm")
        self.ex_inject = ThreadPoolExecutor(1, thread_name_prefix="wispi-inject")

        # Estado del dictado en curso
        # Teclas y simbolos que el usuario mete DESDE EL PANEL TACTIL. Cuentan
        # aparte porque salen marcadas como input sintetico y el hook las filtra,
        # asi que `hook.user_key_count` no las ve: sin este contador, corregir
        # con el propio teclado de WISPI dejaba pasar la compuerta 3 del parche.
        self._ui_keys = 0
        self.cur: Dictation | None = None
        self.trace: DictationTrace | None = None
        self._combo_down_ns = 0
        self._pending_tap_until_ns = 0   # ventana viva de doble-toque
        self._last_periodic = 0.0
        self._state_thread: threading.Thread | None = None
        self.on_state_change = None      # lo cablea la bandeja para cambiar el icono

    # ==================================================================
    # Ciclo de vida
    # ==================================================================
    def start(self) -> None:
        log.info("WISPI arrancando")
        self.audio.start()
        if self.audio.last_error:
            log.error("audio: %s", self.audio.last_error)

        self.asr, self.asr_warnings = build_with_fallback(self.cfg.asr, log)
        for w in self.asr_warnings:
            log.warning("asr fallback: %s", w)
        # El warmup no es opcional: sin el, el PRIMER dictado del usuario paga
        # los allocs perezosos del modelo y se siente roto justo cuando mas
        # importa la primera impresion.
        self.ex_asr.submit(self._warmup_asr)
        if self.cfg.llm.enabled:
            self.ex_llm.submit(self._warmup_llm)

        # La palabra de activacion se arranca DESPUES del ASR principal a
        # proposito: los dos cargan un modelo y el que importa es el del dictado.
        # `start()` no bloquea; el modelo del detector carga en su propio hilo.
        if self.cfg.wake.enabled:
            self.audio.set_wake_sink(self.wake.feed)
            self.wake.start()
            self.wake.set_armed(True)

        hook_ok = self.hook.start()
        self._running.set()
        self._state_thread = threading.Thread(target=self._loop, name="wispi-state",
                                              daemon=True)
        self._state_thread.start()

        if hook_ok:
            log.info("WISPI listo | modelo=%s %s/%s | combo=%s",
                     self.cfg.asr.model, self.cfg.asr.device, self.cfg.asr.compute_type,
                     "+".join(self.cfg.hotkey.combo))
            self.fb.play("ready")
        else:
            # El hook no se instalo: la app esta SORDA a Ctrl+Win. Antes esto
            # se ignoraba en silencio -se sonaba "ready" igual- y el usuario no
            # tenia forma de saber por que no pasaba nada al dictar. El
            # watchdog de hotkey.py reintenta con backoff en segundo plano;
            # _periodic() vigila la recuperacion y sale de ERROR solo cuando
            # `hook.is_alive` vuelva a ser True de verdad.
            log.error("WISPI arranco SIN el hook de teclado instalado. Ctrl+Win "
                      "no respondera hasta que el watchdog consiga reengancharlo.")
            self._set_state(State.ERROR)
            self.fb.play("error")

    def stop(self) -> None:
        log.info("WISPI parando")
        self._running.clear()
        try:
            self.hook.stop()
        except Exception as e:
            log.warning("fallo al parar el hook: %s", e)
        try:
            # Antes que el audio: si el stream muere primero, el sink sigue
            # apuntando a un detector que ya no consume y el deque crece.
            self.audio.set_wake_sink(None)
            self.wake.stop()
        except Exception as e:
            log.warning("fallo al parar la palabra de activacion: %s", e)
        try:
            self.audio.stop()
        except Exception as e:
            log.warning("fallo al parar el audio: %s", e)
        try:
            # El injector tiene un hilo propio para restaurar el portapapeles a
            # los 400 ms. Sin este shutdown, salir justo despues de un dictado
            # deja el portapapeles con el texto dictado en vez del original.
            self.injector.shutdown()
        except Exception as e:
            log.warning("fallo al parar el injector: %s", e)
        for ex in (self.ex_asr, self.ex_llm, self.ex_inject):
            ex.shutdown(wait=False, cancel_futures=True)
        if self._state_thread and self._state_thread.is_alive():
            self._state_thread.join(timeout=2.0)

    def reload_asr(self) -> tuple[bool, str]:
        """Reconstruye el motor con la config actual, en caliente.

        Cambiar `asr.model` en el fichero no basta: el backend se construye una
        vez en start() y se queda cargado. La interfaz de configuracion llama
        aqui para que el cambio se note sin reiniciar WISPI.

        Se ejecuta en el executor de ASR, no en el hilo que llama: asi no puede
        pisar una transcripcion en vuelo (max_workers=1 lo serializa) ni congelar
        la interfaz mientras el modelo carga.
        """
        self.cfg.maybe_reload()
        fut = self.ex_asr.submit(self._do_reload_asr)
        try:
            return fut.result(timeout=90)
        except Exception as e:
            log.exception("fallo recargando el motor")
            return False, f"No pude recargar el motor: {e}"

    def _do_reload_asr(self) -> tuple[bool, str]:
        anterior = self.asr
        try:
            nuevo, warns = build_with_fallback(self.cfg.asr, log)
            nuevo.warmup()
        except Exception as e:
            # El motor viejo sigue cargado y funcionando: mejor un modelo que no
            # es el pedido que quedarse sin dictado.
            return False, f"{e}\n\nSe mantiene el motor anterior ({anterior.describe().get('model')})."
        self.asr = nuevo
        self.asr_warnings = warns
        if anterior is not None and anterior is not nuevo:
            try:
                anterior.unload()
            except Exception:
                pass
        d = nuevo.describe()
        msg = f"Motor recargado: {d.get('model')} {d.get('device')}/{d.get('compute_type')}"
        for w in warns:
            msg += f"\nAviso: {w}"
        log.info(msg.replace("\n", " | "))
        return True, msg

    def ui_cancel(self) -> None:
        """Cancela el dictado en curso desde la interfaz, si lo hay."""
        self.events_q.put((HookEvent.CANCEL, 0, now_ns()))

    # -- teclado tactil ----------------------------------------------------
    # Van por el executor de inyeccion, no directas: es el mismo worker unico que
    # usan los dictados, y eso es lo que garantiza que una tecla no se cuele en
    # mitad de un pegado. Sin esa serializacion, tocar Enter mientras entra un
    # dictado largo por la ruta unicode partiria el texto en dos.
    def ui_press(self, vk: int, modifiers: tuple[int, ...] = (), count: int = 1,
                 etiqueta: str = "", gen: int | None = None) -> None:
        # El destino se captura AQUI, en el hilo de Tk, en el instante del toque.
        # Si para cuando el worker ejecute la tecla el foco ya es otro, press()
        # aborta. Sin esto, una rafaga de Retroceso encolada seguia borrando en
        # la ventana a la que el usuario acababa de cambiar.
        tgt = current_target()
        self._ui_keys += 1
        self.ex_inject.submit(self.injector.press, vk, modifiers, count,
                              etiqueta, tgt, gen)

    def ui_insert_text(self, texto: str) -> None:
        """Inserta texto literal (simbolos del panel). No pasa por post-proceso:
        si tocas '¿' quieres un '¿', no lo que el limpiador opine de el."""
        if not texto:
            return
        self._ui_keys += 1
        fut = self.ex_inject.submit(self.injector.insert, texto, current_target())
        # Sin este callback, un fallo aqui era completamente mudo: ni el usuario
        # ni el log se enteraban de que el simbolo no habia llegado.
        fut.add_done_callback(self._log_ui_insert)

    def _log_ui_insert(self, fut: Future) -> None:
        try:
            r = fut.result()
        except Exception as e:
            log.warning("insercion desde la interfaz fallo: %s", e)
            return
        if not r.ok:
            log.warning("insercion desde la interfaz fallo (%s): %s", r.route, r.error)

    def ui_toggle(self) -> None:
        """Arranca o para un dictado desde la interfaz (boton flotante).

        Encola en vez de tocar el estado directamente: `_begin` y `_finalize` son
        del hilo de estado, y llamarlas desde el hilo de Tk seria una carrera con
        el evento de teclado que puede llegar en el mismo instante.
        """
        self.events_q.put((HookEvent.UI_TOGGLE, 0, now_ns()))

    def toggle_pause(self) -> bool:
        self._paused = not self._paused
        if self._paused and self.state in (State.RECORDING, State.HANDS_FREE):
            self._cancel_current("pausado")
        self._set_state(State.PAUSED if self._paused else State.IDLE)
        log.info("pausa=%s", self._paused)
        return self._paused

    def _warmup_asr(self) -> None:
        try:
            t0 = now_ns()
            self.asr.warmup()
            log.info("warmup ASR: %.0f ms", (now_ns() - t0) / NS_PER_MS)
        except Exception as e:
            log.warning("warmup ASR fallo: %s", e)

    def _warmup_llm(self) -> None:
        try:
            t0 = now_ns()
            ok = self.post.warmup()
            log.info("warmup LLM: %s (%.0f ms)", "ok" if ok else "no disponible",
                     (now_ns() - t0) / NS_PER_MS)
        except Exception as e:
            log.warning("warmup LLM fallo: %s", e)

    def _set_state(self, s: State) -> None:
        if s != self.state:
            self.state = s
            # C11.4: el detector SOLO escucha en reposo. Mientras se graba se
            # oiria a si mismo, y mientras se transcribe le robaria nucleos al
            # ASR de verdad. Es una asignacion de bandera: no cuesta nada.
            self.wake.set_armed(s == State.IDLE and not self._paused)
            if self.on_state_change:
                try:
                    self.on_state_change(s)
                except Exception:
                    pass

    # ==================================================================
    # Bucle principal
    # ==================================================================
    def _loop(self) -> None:
        while self._running.is_set():
            try:
                ev = self.events_q.get(timeout=POLL_S)
            except queue.Empty:
                ev = None
            if ev is not None:
                try:
                    self._on_hook_event(ev)
                except Exception:
                    log.exception("fallo procesando evento del hook")
            self._drain_jobs()
            self._periodic()

    def _drain_jobs(self) -> None:
        """Resultados de los executors. Se ejecutan en ESTE hilo para que toda
        la logica viva en un solo sitio y no haya carreras entre workers."""
        while True:
            try:
                kind, payload = self.jobs_q.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "asr_done":
                    self._on_asr_done(*payload)
                elif kind == "llm_done":
                    self._on_llm_done(*payload)
            except Exception:
                log.exception("fallo procesando resultado de %s", kind)

    def _periodic(self) -> None:
        now = time.monotonic()
        if now - self._last_periodic < PERIODIC_S:
            # El corte por silencio del modo manos libres SI se comprueba en
            # cada vuelta: 1 s de granularidad ahi se notaria como lentitud.
            if self.state == State.HANDS_FREE:
                self._check_hands_free_silence()
            return
        self._last_periodic = now

        # Vigilancia del hook, no solo en start(): puede autocurarse (el
        # watchdog reintenta con backoff) o Windows puede desengancharlo en
        # cualquier momento de la vida de la app, no solo al arrancar. Solo se
        # gestiona entrando/saliendo desde IDLE/ERROR a proposito: forzar el
        # estado en mitad de una grabacion o una transcripcion confundiria el
        # icono sin aportar nada, y el propio Ctrl+Win seguiria sin responder
        # de todas formas hasta que el usuario suelte lo que este haciendo.
        hook_alive = self.hook.is_alive
        if not hook_alive and self.state == State.IDLE:
            log.error("el hook de teclado dejo de estar activo; Ctrl+Win no responde")
            self._set_state(State.ERROR)
        elif hook_alive and self.state == State.ERROR:
            log.info("el hook de teclado se recupero; WISPI vuelve a escuchar")
            self._set_state(State.IDLE)
            self.fb.play("ready")

        if self.state == State.HANDS_FREE:
            self._check_hands_free_silence()

        changed, warns = self.cfg.maybe_reload()
        if changed:
            for w in warns:
                log.warning("config: %s", w)
            log.info("config recargada")
            self._on_config_reloaded()
        if self.post.reload_dictionary():
            log.info("diccionario recargado")

        # Tope duro de grabacion: si el usuario se deja la tecla pulsada, no
        # podemos acumular audio para siempre.
        if self.state == State.RECORDING and self.cur:
            held_s = (now_ns() - self.cur.t_keydown_ns) / 1e9
            if held_s > self.cfg.audio.max_duration_s:
                log.warning("tope de %.0fs alcanzado; finalizando", self.cfg.audio.max_duration_s)
                self._finalize(now_ns())

    def _on_config_reloaded(self) -> None:
        """UNICO sitio donde se re-aplica lo que no basta con leer de `self.cfg`.

        Desde que `Config.maybe_reload()` muta las secciones en sitio, casi todo
        se recarga solo: quien guardo una referencia a su seccion ve los valores
        nuevos sin hacer nada. Aqui quedan los dos casos que no encajan en eso:
        lo que se DERIVA de la config una sola vez, y lo que se COPIO por valor.
        Si añades un modulo que haga cualquiera de las dos cosas, se sincroniza
        desde aqui y se le añade un caso a `tools/test_config_reload.py`.
        """
        # Feedback copia los valores y ademas pregenera los tonos, asi que no le
        # vale con que la seccion sea la misma.
        fb, ui = self.fb, self.cfg.ui
        if fb.enabled != ui.chime_enabled or fb.volume != float(ui.chime_volume):
            fb.enabled = bool(ui.chime_enabled)
            fb.set_volume(float(ui.chime_volume))
        self._sync_wake()

    def _sync_wake(self) -> None:
        """Enciende o apaga la palabra de activacion tras recargar la config.

        Ya NO hace falta repuntar `self.wake.cfg`: desde que `maybe_reload()`
        muta las secciones en sitio, la referencia que guardo el detector al
        construirse sigue siendo valida. Lo que si hay que pedirle es que
        re-derive los frames del segmentador, porque esos se calculan una vez.

        `wake.model` y `wake.cpu_threads` no entran en caliente -el modelo se
        carga al arrancar el hilo-; de conservarlos y avisar se encarga
        `config.py::RESTART_ONLY`.
        """
        self.wake.log_text = self.cfg.logging.include_text
        self.wake.reconfigure()
        quiere = self.cfg.wake.enabled
        activo = self.wake.is_alive
        if quiere and not activo:
            log.info("wake: encendido desde la configuracion")
            self.audio.set_wake_sink(self.wake.feed)
            self.wake.start()
            self.wake.set_armed(self.state == State.IDLE and not self._paused)
        elif not quiere and activo:
            log.info("wake: apagado desde la configuracion")
            self.audio.set_wake_sink(None)
            self.wake.stop(join=False)

    def _check_hands_free_silence(self) -> None:
        if self.audio.silence_elapsed_s() >= self.cfg.audio.silence_duration_s:
            log.info("manos libres: silencio de %.1fs, cortando",
                     self.cfg.audio.silence_duration_s)
            self._finalize(now_ns())

    # ==================================================================
    # Eventos del hook
    # ==================================================================
    def _on_hook_event(self, ev) -> None:
        kind, vk, t_ns = ev

        if kind == HookEvent.HOOK_SLOW:
            # Windows desengancha en silencio si el callback se pasa de 300 ms.
            # Reinstalar a ciegas es la unica defensa: no hay notificacion.
            log.warning("callback lento detectado; re-enganchando el hook")
            self.hook.rehook()
            return

        if kind == HookEvent.USER_KEY:
            # No se hace nada aqui a proposito. El contador vive en el hook y se
            # consulta al decidir el parche: asi este camino cuesta cero.
            return

        if self._paused:
            return

        if kind == HookEvent.UI_TOGGLE:
            # Clic en el boton flotante. Entra en manos libres a proposito: con un
            # boton no hay "mantener pulsado", asi que el modelo natural es
            # clic-para-empezar / clic-o-silencio-para-parar.
            if self.state in (State.RECORDING, State.HANDS_FREE):
                self._finalize(t_ns)
            elif self.state == State.IDLE:
                self._begin(t_ns, hands_free=True)
            return

        if kind == HookEvent.WAKE:
            # "hey WISPI". A diferencia de UI_TOGGLE, esto NUNCA corta un dictado
            # en curso: si el estado no es IDLE el evento se tira. El detector ya
            # deberia estar desarmado, pero un evento puede haber salido de su
            # hilo justo antes de que llegara el desarme, y perder un despertar
            # es infinitamente mejor que cortar una frase a medias.
            if self.state == State.IDLE:
                self._begin(t_ns, hands_free=True, from_wake=True)
            return

        if kind == HookEvent.COMBO_DOWN:
            self._on_combo_down(t_ns)
        elif kind == HookEvent.COMBO_UP:
            self._on_combo_up(t_ns)
        elif kind == HookEvent.CANCEL:
            if self.state in (State.RECORDING, State.HANDS_FREE):
                self._cancel_current("Esc")

    def _on_combo_down(self, t_ns: int) -> None:
        # Segundo toque dentro de la ventana -> manos libres.
        if self._pending_tap_until_ns and t_ns <= self._pending_tap_until_ns:
            self._pending_tap_until_ns = 0
            self._begin(t_ns, hands_free=True)
            return
        self._pending_tap_until_ns = 0

        if self.state == State.HANDS_FREE:
            self._finalize(t_ns)          # segundo toque cierra el modo
            return
        if self.state != State.IDLE:
            return                        # ya hay un dictado en vuelo
        self._begin(t_ns, hands_free=False)

    def _on_combo_up(self, t_ns: int) -> None:
        if self.state == State.HANDS_FREE:
            return                        # en manos libres el key-up no corta
        if self.state != State.RECORDING or not self.cur:
            return

        held_ms = (t_ns - self._combo_down_ns) / NS_PER_MS
        if held_ms < self.cfg.hotkey.tap_max_ms:
            # Fue un TOQUE, no un push-to-talk. Se descarta el audio (esta por
            # debajo de min_duration de todas formas) y se abre la ventana de
            # doble-toque.
            #
            # Este es el detalle que hace que el modo manos libres NO cueste
            # nada en el camino comun: una pulsacion PTT real dura >500 ms, cae
            # por el else, y se finaliza AL INSTANTE sin esperar los 400 ms.
            self.audio.cancel_recording()
            self._pending_tap_until_ns = t_ns + self.cfg.hotkey.double_tap_ms * NS_PER_MS
            self.cur = None
            self.trace = None
            self._set_state(State.IDLE)
            return

        self._finalize(t_ns)

    # ==================================================================
    # Ciclo de un dictado
    # ==================================================================
    def _begin(self, t_ns: int, hands_free: bool, from_wake: bool = False) -> None:
        seq = self.metrics.next_seq()
        self.trace = DictationTrace(seq=seq)
        self.trace.mark("t0_keydown", t_ns)
        self.cur = Dictation(seq=seq, t_keydown_ns=t_ns)
        self._combo_down_ns = t_ns
        if from_wake:
            # Sin pre-roll: el ring lleva el final de "hey WISPI" y acabaria
            # escrito (C11.3). Y con mas gracia inicial, porque aqui el usuario
            # espera al tono antes de empezar a hablar.
            self.audio.begin_recording(include_preroll=self.cfg.wake.include_preroll,
                                       grace_s=self.cfg.wake.start_grace_s)
            self.trace.set(trigger="wake")
        else:
            self.audio.begin_recording()
        self.trace.mark("t1_capture_on")
        self._set_state(State.HANDS_FREE if hands_free else State.RECORDING)
        self.fb.play("start")

    def _cancel_current(self, reason: str) -> None:
        log.info("dictado cancelado (%s)", reason)
        self.fb.play("cancel")
        self.audio.cancel_recording()
        if self.trace and self.cur:
            self.cur.discarded_reason = reason
            self.trace.set(discarded=reason, n_words=0)
            self.metrics.write(self.trace)
        self.cur = None
        self.trace = None
        self._set_state(State.IDLE)

    def _finalize(self, t_keyup_ns: int) -> None:
        """Cierra la captura y despacha el trabajo pesado. NO bloquea."""
        if not self.cur or not self.trace:
            return
        self.trace.mark("t2_keyup", t_keyup_ns)
        self.cur.t_keyup_ns = t_keyup_ns

        # El destino se captura AQUI, en el key-up, no despues. Es la ventana
        # que el usuario tenia delante al terminar de hablar; si cambia mientras
        # transcribimos, las compuertas del parche lo detectaran.
        tgt = current_target()
        self.cur.target_hwnd, self.cur.target_pid = tgt.hwnd, tgt.pid
        self.cur.target_exe = tgt.exe

        self._set_state(State.TRANSCRIBING)
        self.fb.play("stop")
        dictation, trace = self.cur, self.trace
        self.cur, self.trace = None, None   # el dictado pasa a ser del worker
        self.ex_asr.submit(self._work_asr, dictation, trace, tgt)

    # ---- worker: audio final + gate + ASR --------------------------------
    def _work_asr(self, d: Dictation, tr: DictationTrace, tgt: Target) -> None:
        """Corre en wispi-asr. Aqui SI se puede bloquear."""
        try:
            audio = self.audio.end_recording()   # espera tail_ms
            tr.mark("t3_audio_final")
            dur_s = len(audio) / self.cfg.audio.sample_rate if audio is not None else 0.0
            d.audio_ms = dur_s * 1000.0
            peak = self.audio.peak_rms

            # --- gate ANTES del ASR (criterio C6.2) ---
            # Whisper alucina sobre silencio: large-v3 produce " Gracias." con
            # ruido a -54 dB. Descartar aqui es mas barato y mas fiable que
            # filtrar la alucinacion despues.
            reason = None
            if dur_s < self.cfg.audio.min_duration_s:
                reason = f"corto ({dur_s:.2f}s)"
            elif peak < self.cfg.audio.silence_threshold:
                reason = f"silencio (rms {peak:.4f})"
            tr.mark("t4_gate")
            if reason:
                tr.set(discarded=reason, audio_ms=round(d.audio_ms), n_words=0,
                       peak_rms=round(peak, 5))
                self.metrics.write(tr)
                log.info("descartado: %s", reason)
                self.jobs_q.put(("asr_done", (d, tr, tgt, None)))
                return

            tr.mark("t5_asr_in")
            # initial_prompt(), NO hotwords(): faster-whisper no tiene hotwords
            # reales y el backend mapea ese parametro a initial_prompt. Ahi una
            # lista de terminos hace que el decoder devuelva listas y trunque la
            # transcripcion (medido: 77% de retencion vs 100% con prosa).
            res = self.asr.transcribe(
                audio, language=self.cfg.asr.language,
                hotwords=self.post.initial_prompt(),
            )
            tr.mark("t6_asr_out")
            tr.set(audio_ms=round(d.audio_ms), peak_rms=round(peak, 5),
                   rtf=round(res.rtf, 3), model=res.model_id, device=res.device,
                   compute_type=res.compute_type, backend=res.backend,
                   no_speech=round(res.no_speech_prob, 3) if res.no_speech_prob is not None else None)
            self.jobs_q.put(("asr_done", (d, tr, tgt, res)))
        except Exception as e:
            log.exception("fallo en el worker de ASR")
            tr.set(error=str(e)[:200])
            self.metrics.write(tr)
            self.jobs_q.put(("asr_done", (d, tr, tgt, None)))

    # ---- de vuelta en el hilo de estado ---------------------------------
    def _on_asr_done(self, d: Dictation, tr: DictationTrace, tgt: Target, res) -> None:
        if res is None:
            self._set_state(State.IDLE)
            return

        raw = (res.text or "").strip()
        d.raw_text = raw

        # Segunda linea de defensa: el gate de RMS coge el silencio, pero una
        # tos o un golpe en la mesa lo pasan y Whisper responde con basura.
        if not raw or is_whisper_hallucination(raw):
            tr.set(discarded="alucinacion" if raw else "vacio", n_words=0)
            self.metrics.write(tr)
            log.info("descartado: %s", "alucinacion" if raw else "transcripcion vacia")
            self._set_state(State.IDLE)
            return
        if res.no_speech_prob is not None and res.no_speech_prob > self.cfg.asr.no_speech_threshold:
            tr.set(discarded=f"no_speech {res.no_speech_prob:.2f}", n_words=0)
            self.metrics.write(tr)
            self._set_state(State.IDLE)
            return

        text = self.post.level0(raw) if self.cfg.postprocess.enabled else raw
        tr.mark("t7_level0")
        d.level0_text = text
        tr.set(n_words=len(text.split()), n_chars=len(text),
               target_exe=tgt.exe or "?")
        if self.cfg.logging.include_text:
            tr.set(text=text)

        if not text.strip():
            tr.set(discarded="vacio tras nivel 0")
            self.metrics.write(tr)
            self._set_state(State.IDLE)
            return

        wants_llm = self.cfg.llm.enabled and self.post.needs_level1(text)
        is_no_patch = (tgt.exe or "").lower() in \
            {a.lower() for a in self.cfg.injection.no_patch_apps}

        if wants_llm and is_no_patch:
            # En terminal no se parchea NUNCA: seleccionar hacia atras en una
            # linea de comandos es una forma excelente de ejecutar medio comando.
            # Se espera acotado y se escribe UNA sola vez.
            self._set_state(State.POLISHING)
            self.ex_llm.submit(self._work_llm, d, tr, tgt, text, True)
            return

        if wants_llm:
            # Insercion optimista: el crudo entra YA (latencia percibida = solo
            # ASR) y el LLM viaja en paralelo. El parche llega despues, si puede.
            self._insert_now(d, tr, tgt, text, final=False)
            self._set_state(State.POLISHING)
            self.ex_llm.submit(self._work_llm, d, tr, tgt, text, False)
            return

        self._insert_now(d, tr, tgt, text, final=True)

    def _insert_now(self, d: Dictation, tr: DictationTrace, tgt: Target,
                    text: str, final: bool) -> None:
        tr.mark("t8_inject_in")
        d.user_keys_at_insert = self.hook.user_key_count + self._ui_keys
        fut = self.ex_inject.submit(self.injector.insert, text, tgt)
        fut.add_done_callback(
            lambda f: self._after_insert(f, d, tr, text, final)
        )

    def _after_insert(self, fut: Future, d: Dictation, tr: DictationTrace,
                      text: str, final: bool) -> None:
        try:
            r = fut.result()
        except Exception as e:
            log.exception("fallo la insercion")
            tr.set(inject_error=str(e)[:200])
            self.metrics.write(tr)
            self._set_state(State.IDLE)
            return
        tr.mark("t9_inject_out")
        tr.set(route=r.route, inserted=r.chars, inject_ok=r.ok)
        if not r.ok:
            tr.set(inject_error=(r.error or "")[:200])
            log.warning("insercion fallida por %s: %s", r.route, r.error)
        d.inserted_len = r.chars if r.ok else 0
        d.route = r.route
        if final:
            tr.set(patched=False)
            self.metrics.write(tr)
            self._set_state(State.IDLE)

    # ---- worker: LLM ------------------------------------------------------
    def _work_llm(self, d: Dictation, tr: DictationTrace, tgt: Target,
                  text: str, single_write: bool) -> None:
        """Corre en wispi-llm. single_write=True en apps donde no se parchea."""
        tr.mark("t10_llm_in")
        out = None
        try:
            out = self.post.level1(text)
        except Exception as e:
            log.warning("nivel 1 fallo: %s", e)
        tr.mark("t11_llm_out")
        self.jobs_q.put(("llm_done", (d, tr, tgt, text, out, single_write)))

    def _on_llm_done(self, d: Dictation, tr: DictationTrace, tgt: Target,
                     text: str, out: str | None, single_write: bool) -> None:
        final_text = out if out else text
        d.final_text = final_text
        tr.set(llm_used=bool(out))

        if single_write:
            # Camino de terminal: una sola escritura, con lo que haya llegado.
            self._insert_now(d, tr, tgt, final_text, final=True)
            return

        if not out or out == text:
            tr.set(patched=False, patch_skip="sin cambios" if out else "llm descartado")
            self.metrics.write(tr)
            self._set_state(State.IDLE)
            return

        # --- LAS CUATRO COMPUERTAS DEL PARCHE OPTIMISTA ---
        # Fallar una sola aborta el parche y deja el crudo. Perder la limpieza es
        # un fastidio; corromper un documento del usuario es inaceptable, y la
        # asimetria entre esos dos costes es toda la justificacion de esta lista.
        skip = None
        tgt_now = current_target()
        if tgt_now.hwnd != d.target_hwnd or tgt_now.pid != d.target_pid:
            skip = "cambio de ventana"                                   # 1
        elif not d.inserted_len:
            skip = "no se inserto nada"
        else:
            elapsed_ms = (tr.marks.get("t11_llm_out", 0)
                          - tr.marks.get("t9_inject_out", 0)) / NS_PER_MS
            if elapsed_ms > self.cfg.injection.patch_max_wait_ms:         # 2
                skip = f"tarde ({elapsed_ms:.0f} ms)"
            elif (self.hook.user_key_count + self._ui_keys) != d.user_keys_at_insert:  # 3
                skip = "el usuario siguio escribiendo"
            elif (tgt.exe or "").lower() in \
                    {a.lower() for a in self.cfg.injection.no_patch_apps}:  # 4
                skip = "app sin parche"

        if skip:
            log.info("parche abortado: %s", skip)
            tr.set(patched=False, patch_skip=skip)
            self.metrics.write(tr)
            self._set_state(State.IDLE)
            return

        # d.level0_text es lo que se inserto la PRIMERA vez (antes del LLM):
        # replace() lo usa para comprobar que la seleccion hacia atras coincide
        # con lo que creemos que hay ahi, antes de escribir encima a ciegas.
        fut = self.ex_inject.submit(self.injector.replace, d.inserted_len,
                                    final_text, tgt, d.level0_text)
        fut.add_done_callback(lambda f: self._after_patch(f, d, tr))

    def _after_patch(self, fut: Future, d: Dictation, tr: DictationTrace) -> None:
        try:
            r = fut.result()
            ok = r.ok
            if not ok:
                tr.set(patch_error=(r.error or "")[:200])
        except Exception as e:
            ok = False
            tr.set(patch_error=str(e)[:200])
            log.exception("fallo el parche")
        tr.mark("t12_patch_out")
        tr.set(patched=ok)
        d.patched = ok
        self.metrics.write(tr)
        self._set_state(State.IDLE)

    # ==================================================================
    def status(self) -> dict:
        return {
            "state": self.state.value,
            "paused": self._paused,
            "asr": self.asr.describe() if self.asr else None,
            "asr_warnings": self.asr_warnings,
            "hook": self.hook.stats(),
            "wake": self.wake.stats(),
            "audio": self.audio.device_info(),
            "audio_error": self.audio.last_error,
        }

    def run_console(self) -> None:
        """Modo consola: sin bandeja, Ctrl+C para salir."""
        self.start()
        print("WISPI en marcha. Manten Ctrl+Win para dictar. Ctrl+C para salir.")
        try:
            while self._running.is_set():
                time.sleep(0.3)
        except KeyboardInterrupt:
            print("\nSaliendo...")
        finally:
            self.stop()


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="wispi", description="Dictado por voz local")
    ap.add_argument("--console", action="store_true",
                    help="sin interfaz, log a stdout (desarrollo)")
    ap.add_argument("--no-overlay", action="store_true",
                    help="sin boton flotante, solo bandeja")
    ap.add_argument("--log-level", default=None)
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    if args.log_level:
        cfg.logging.level = args.log_level
    logging_setup.setup(cfg.logging.level, console=args.console,
                        max_bytes=cfg.logging.max_bytes,
                        backup_count=cfg.logging.backup_count)

    # El hilo del hook tiene 300 ms de presupuesto y en Python el riesgo real no
    # es tardar, es no poder EMPEZAR porque otro hilo tiene el GIL. Bajar el
    # switch interval recorta la espera peor de ~5 ms a ~1 ms. Es global al
    # interprete, por eso se hace aqui y no dentro de hotkey.py.
    sys.setswitchinterval(0.001)

    app = WispiApp(cfg, console=args.console)
    if args.console:
        app.run_console()
        return 0

    if cfg.ui.overlay_enabled and not args.no_overlay:
        from .overlay import run_overlay
        return run_overlay(app)

    if cfg.ui.tray_enabled:
        from .tray import run_tray
        return run_tray(app)

    app.run_console()
    return 0
