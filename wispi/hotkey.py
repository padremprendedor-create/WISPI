"""Hook de teclado de bajo nivel (WH_KEYBOARD_LL). El punto de fallo numero 1.

POR QUE UN HOOK Y NO RegisterHotKey
    `RegisterHotKey` solo entrega WM_HOTKEY en la PULSACION: no existe evento de
    key-up. Sin key-up no hay push-to-talk, que es el gesto entero del producto.
    Ademas reserva la combinacion en todo el sistema y es disparable por input
    sintetico. Por eso se observa con un hook y NUNCA se hace swallow de la tecla.

POR QUE EL CALLBACK ES TAN CORTO
    Windows aplica `LowLevelHooksTimeout` (300 ms por defecto; verificado ausente
    en HKCU\\Control Panel\\Desktop, luego aplica el default). Si el callback se
    pasa, Windows DESENGANCHA EL HOOK EN SILENCIO: la app parece viva y no
    responde a la tecla. En Python el riesgo real no es tardar, es no poder
    EMPEZAR porque otro hilo tiene el GIL; de ahi `sys.setswitchinterval(0.001)`
    y de ahi que el callback solo lea la struct, toque enteros y encole una
    TUPLA PLANA. Nada de logging, locks, formateo ni I/O ahi dentro.

POR QUE HAY UN HILO AUXILIAR
    Dos trabajos no caben en el callback y tampoco pueden bloquear el bucle de
    mensajes: (a) emitir el SendInput que suprime el menu Inicio, (b) el watchdog
    que reinstala el hook cada `rehook_interval_s`. El hilo del hook esta
    bloqueado en `GetMessageW`, asi que el auxiliar hace el trabajo de SendInput
    por su cuenta y despierta al hilo del hook con `PostThreadMessageW` cuando
    toca reenganchar (SetWindowsHookEx tiene que ejecutarse en el hilo que tiene
    el bucle de mensajes, o el callback se entregaria al hilo equivocado).

LIMITACION CONOCIDA, NO SE INTENTA RESOLVER
    Mientras una ventana ELEVADA (administrador) tiene el foco, este hook NO
    recibe nada, salvo que nuestro propio proceso tambien este elevado: UIPI
    bloquea la interaccion de integridad baja hacia alta. Es una decision de
    Windows, no un bug. El arranque elevado es opt-in explicito del usuario
    (ver SPEC 5.1) precisamente porque un hook global elevado tiene forma de
    keylogger.

PRIVACIDAD
    Este modulo no registra NUNCA el vkCode de teclas ajenas al combo (C9.2).
    Lo unico que sale del hook hacia el log es el combo configurado al arrancar.
"""
from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time
from collections import deque
from ctypes import wintypes

from . import winapi as w
from . import logging_setup
from .config import HotkeyCfg
from .events import HookEvent
from .metrics import Metrics

# ---------------------------------------------------------------------------
# Constantes que NO estan en winapi.py.
# No las declaro alli porque winapi.py es de otro dueno en esta ronda; quedan
# reportadas para que se muevan luego. Ninguna es un prototipo ctypes: son un
# nCode y un id de mensaje propio, no tipos que puedan truncarse.
# ---------------------------------------------------------------------------
_HC_ACTION = 0                      # unico nCode >= 0 que entrega WH_KEYBOARD_LL
_WM_APP = 0x8000
_WM_WISPI_REHOOK = _WM_APP + 1      # mensaje privado: "reinstala el hook"

# Grupos de teclas del combo. Un grupo = UN bit del bitmask, de forma que da
# igual si el usuario usa el Ctrl izquierdo o el derecho.
# OJO con "alt": VK_RMENU es AltGr en teclado espanol y genera @ # ~ [ ] \.
# Se acepta por completitud, pero usarlo como hotkey es una mala idea.
_COMBO_GROUPS: dict[str, tuple[int, ...]] = {
    "ctrl": (w.VK_CONTROL, w.VK_LCONTROL, w.VK_RCONTROL),
    "control": (w.VK_CONTROL, w.VK_LCONTROL, w.VK_RCONTROL),
    "win": (w.VK_LWIN, w.VK_RWIN),
    "meta": (w.VK_LWIN, w.VK_RWIN),
    "super": (w.VK_LWIN, w.VK_RWIN),
    "shift": (w.VK_SHIFT, w.VK_LSHIFT, w.VK_RSHIFT),
    "alt": (w.VK_MENU, w.VK_LMENU, w.VK_RMENU),
}

_CANCEL_KEYS: dict[str, int] = {
    "esc": w.VK_ESCAPE,
    "escape": w.VK_ESCAPE,
}

_DEFAULT_COMBO = ("ctrl", "win")


class KeyboardHook:
    """Observa el teclado y encola tuplas planas (HookEvent, vk, t_ns).

    Nunca consume una tecla: siempre devuelve `CallNextHookEx`. El combo
    Ctrl+Win es seguro justamente porque solo se observa; hacer swallow de Ctrl
    o de Win romperia su uso normal en todo el sistema.
    """

    def __init__(self, cfg: HotkeyCfg, out_queue: "queue.SimpleQueue", metrics: Metrics, log) -> None:
        self._cfg = cfg
        self._q = out_queue
        self._metrics = metrics
        self._log = log if log is not None else logging_setup.get("hotkey")

        # -- estado del combo (lo escribe el callback, lo leen otros hilos) ---
        # Son enteros/booleanos sueltos a proposito: en CPython asignar un
        # atributo es atomico y no hace falta lock, que esta prohibido dentro
        # del callback.
        self._mask = 0
        self._active = False
        self._user_keys = 0
        self._suppress_pending = False
        self._slow_pending = False
        self._cb_errors = 0
        # Histograma del coste PROPIO (sin CallNextHookEx). `metrics` mide el
        # total, que es lo que cronometra Windows, pero ese total incluye a los
        # hooks de otras apps. MEDIDO en esta maquina: con Wispr Flow corriendo
        # (usa el mismo Ctrl+Win) CallNextHookEx llega a 15 ms mientras que lo
        # nuestro se queda en 26 us de p99. Sin este segundo histograma, una
        # regresion futura se investigaria en el sitio equivocado.
        # deque con maxlen: append O(1) y no realoca, apto para el callback.
        self._own_us: deque[int] = deque(maxlen=5000)

        # -- handles y control de hilos --------------------------------------
        self._hook = 0
        self._hmod = w.kernel32.GetModuleHandleW(None)
        self._tid = 0
        self._thread: threading.Thread | None = None
        self._aux: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._ready = threading.Event()
        self._install_ok = False
        self._n_rehooks = 0
        self._last_rehook_ns = 0
        self._rehook_ok = False
        self._rehook_done = threading.Event()
        self._rehook_lock = threading.Lock()

        # REFERENCIA VIVA AL CALLBACK. Si esto fuese una variable local, el
        # recolector liberaria el trampolin de ctypes y Windows llamaria a
        # memoria liberada: el proceso muere sin traza y sin explicacion.
        self._proc = None  # se rellena en start(): el CANDIDATO a instalar
        # El trampolin REALMENTE ligado al hook activo (self._hook). No es lo
        # mismo que self._proc: si un reenganche falla, Windows sigue llamando
        # al proc VIEJO aunque self._proc ya apunte al nuevo. Confundir los dos
        # es justo el bug que encontro la revision adversarial: apply_config()
        # liberaba self._proc viejo dando por hecho que ya no lo usaba nadie,
        # y si rehook() fallaba el hook activo se quedaba apuntando a memoria
        # liberada -> crash STATUS_FATAL_USER_CALLBACK_EXCEPTION sin traza.
        # Se actualiza EXACTAMENTE en los dos sitios donde self._hook cambia de
        # verdad (_run al instalar, _do_rehook al reenganchar con exito) y se
        # limpia cuando se desengancha. Mientras no se actualice, es la unica
        # referencia que hace falta mantener viva.
        self._hook_proc = None

        self._configure()
        self._e8_inputs = self._build_e8_inputs()

    # ------------------------------------------------------------------
    # Configuracion derivada (se calcula fuera del camino caliente)
    # ------------------------------------------------------------------
    def _configure(self) -> None:
        names = [str(n).strip().lower() for n in (self._cfg.combo or [])]
        valid = [n for n in names if n in _COMBO_GROUPS]
        for n in names:
            if n not in _COMBO_GROUPS:
                self._log.warning("hotkey.combo: '%s' no es una tecla conocida; ignorada", n)
        if not valid:
            self._log.error("hotkey.combo vacio o invalido (%s); se usa %s",
                            names, list(_DEFAULT_COMBO))
            valid = list(_DEFAULT_COMBO)
        if "alt" in valid:
            self._log.warning("hotkey.combo incluye 'alt': en teclado espanol AltGr "
                              "(VK_RMENU) lo disparara al escribir @ # ~ [ ] \\")

        # Un bit por grupo. Deduplicado conservando el orden.
        seen: list[str] = []
        for n in valid:
            if n not in seen:
                seen.append(n)
        self._combo_names = seen

        vk_bits: dict[int, int] = {}
        full = 0
        win_mask = 0
        for i, name in enumerate(seen):
            bit = 1 << i
            full |= bit
            if name in ("win", "meta", "super"):
                win_mask |= bit
            for vk in _COMBO_GROUPS[name]:
                vk_bits[vk] = bit
        self._vk_bits = vk_bits
        self._full_mask = full
        self._win_mask = win_mask

        ck = str(self._cfg.cancel_key or "esc").strip().lower()
        if ck not in _CANCEL_KEYS:
            self._log.warning("hotkey.cancel_key '%s' desconocida; se usa esc", ck)
        self._cancel_vk = _CANCEL_KEYS.get(ck, w.VK_ESCAPE)

    def _build_e8_inputs(self):
        """Prepara el down+up de VK 0xE8 UNA vez, en el arranque.

        Se construye aqui y no en el momento de emitirlo para que el hilo
        auxiliar no tenga que reservar memoria en mitad de una pulsacion.
        """
        arr = (w.INPUT * 2)()
        for i, flags in enumerate((0, w.KEYEVENTF_KEYUP)):
            arr[i].type = w.INPUT_KEYBOARD
            arr[i].ki.wVk = w.VK_UNASSIGNED_E8
            arr[i].ki.wScan = 0
            arr[i].ki.dwFlags = flags
            arr[i].ki.time = 0
            # Firmado como nuestro: si algun dia se acepta input sintetico, el
            # propio hook sabe que esto lo emitio el.
            arr[i].ki.dwExtraInfo = w.WISPI_EXTRAINFO
        return arr

    # ------------------------------------------------------------------
    # El callback. Todo lo caliente esta aqui.
    # ------------------------------------------------------------------
    def _build_proc(self):
        """Construye el callback como closure.

        POR QUE closure y no un metodo: lo que se ejecuta en CADA tecla del
        sistema queda como variable libre (LOAD_DEREF) en vez de recorrer
        `self.__dict__` o el diccionario del modulo. El ESTADO si vive en
        `self` porque `user_key_count` y `stats()` lo leen desde otro hilo.
        """
        now = time.perf_counter_ns
        call_next = w.user32.CallNextHookEx
        read = w.KBDLLHOOKSTRUCT.from_address
        put = self._q.put_nowait
        record = self._metrics.record_callback
        note_slow = self._metrics.note_slow_callback
        own = self._own_us.append

        vk_bits = self._vk_bits
        vk_get = vk_bits.get
        full = self._full_mask
        win_mask = self._win_mask
        cancel_vk = self._cancel_vk
        accept_injected = bool(self._cfg.accept_injected)
        suppress = bool(self._cfg.suppress_start_menu) and bool(win_mask)
        slow_ns = max(1, int(self._cfg.slow_callback_ms)) * 1_000_000

        INJECTED = w.LLKHF_INJECTED
        UP = w.LLKHF_UP
        SENTINEL = w.VK_UNASSIGNED_E8
        EV_DOWN = HookEvent.COMBO_DOWN
        EV_UP = HookEvent.COMBO_UP
        EV_CANCEL = HookEvent.CANCEL
        EV_USER = HookEvent.USER_KEY
        EV_SLOW = HookEvent.HOOK_SLOW

        def proc(nCode, wParam, lParam):
            t0 = now()
            if nCode == _HC_ACTION:
                # try/except de coste cero en 3.11 mientras no salta. Existe
                # para que un bug aqui no rompa la cadena de hooks del sistema:
                # pase lo que pase abajo se llama a CallNextHookEx.
                try:
                    ks = read(lParam)
                    flags = ks.flags
                    vk = ks.vkCode
                    # 0xE8 es NUESTRO supresor del menu Inicio: nunca es del
                    # usuario, se ignora siempre (tambien en modo test).
                    if vk != SENTINEL and ((flags & INJECTED) == 0 or accept_injected):
                        # ASIMETRIA DELIBERADA del filtro de input sintetico:
                        #  - produccion (accept_injected=false): se ignora TODO
                        #    lo inyectado, incluido lo nuestro. Sin esto, la
                        #    inyeccion de texto se auto-disparia en bucle.
                        #  - test (accept_injected=true): se acepta TODO lo
                        #    inyectado, incluido lo firmado con WISPI_EXTRAINFO,
                        #    porque si no no habria forma de probar el hook sin
                        #    dedos humanos.
                        bit = vk_get(vk)
                        if flags & UP:
                            if bit is not None:
                                mask = self._mask & ~bit
                                self._mask = mask
                                if self._active:
                                    self._active = False
                                    put((EV_UP, vk, t0))
                                    if suppress and (mask & win_mask):
                                        # Se solto Ctrl ANTES que Win: Win queda
                                        # sola y al soltarla abre Inicio y roba
                                        # el foco. Solo se MARCA la necesidad;
                                        # el SendInput lo hace el hilo auxiliar
                                        # porque no cabe en el presupuesto.
                                        self._suppress_pending = True
                        elif bit is not None:
                            if not (self._mask & bit):   # ignora autorrepeat
                                mask = self._mask | bit
                                self._mask = mask
                                if not self._active and (mask & full) == full:
                                    self._active = True
                                    put((EV_DOWN, vk, t0))
                        elif vk == cancel_vk:
                            # Se emite CANCEL siempre, no solo con el combo
                            # activo: en manos libres el combo YA no esta
                            # pulsado y aun asi Esc tiene que cancelar (C3.6).
                            # Cuando no hay combo activo ademas cuenta como
                            # tecla del usuario, porque puede ser un Esc ajeno
                            # y el parche optimista debe abortarse.
                            if not self._active:
                                self._user_keys += 1
                            put((EV_CANCEL, vk, t0))
                        else:
                            self._user_keys += 1
                            put((EV_USER, vk, t0))
                except Exception:
                    self._cb_errors += 1

            # Se cronometra ANTES y DESPUES de CallNextHookEx: Windows mide el
            # total (por eso es lo que va a `metrics`), pero solo la primera
            # mitad es nuestra y es la unica sobre la que se puede actuar.
            t1 = now()
            res = call_next(None, nCode, wParam, lParam)

            dt = now() - t0
            own((t1 - t0) // 1000)
            record(dt // 1000)
            if dt > slow_ns:
                note_slow()
                self._slow_pending = True     # el auxiliar forzara re-enganche
                try:
                    put((EV_SLOW, 0, t0))
                except Exception:
                    self._cb_errors += 1
            return res

        return proc

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    def start(self) -> bool:
        """Instala el hook y arranca el watchdog. Devuelve si quedo instalado YA.

        Un False no es fatal -el watchdog reintenta con backoff en segundo
        plano-, pero el llamador (app.py) tiene que saberlo: sin este valor de
        retorno, la app sonaba el chime de "listo" y no avisaba de nada sobre
        una instancia que no podia oir ni una tecla.
        """
        if self._thread is not None and self._thread.is_alive():
            return self.is_alive
        self._stop_evt.clear()
        self._ready.clear()
        self._install_ok = False
        self._proc = w.HOOKPROC(self._build_proc())   # referencia viva en self

        self._thread = threading.Thread(target=self._run, name="wispi-hook", daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            self._log.error("el hilo del hook no confirmo instalacion en 5 s")
        elif self._install_ok:
            self._log.info("hook instalado (combo=%s, cancel=esc, rehook cada %ss, "
                           "slow>%sms, accept_injected=%s)",
                           "+".join(self._combo_names), self._cfg.rehook_interval_s,
                           self._cfg.slow_callback_ms, bool(self._cfg.accept_injected))
        # Si no se instalo, _run() ya registro el motivo exacto por su cuenta.

        # El watchdog arranca IGUAL, se haya instalado o no: es quien reintenta.
        self._aux = threading.Thread(target=self._run_aux, name="wispi-hook-wd", daemon=True)
        self._aux.start()
        return self._install_ok

    def _run(self) -> None:
        # GLOBAL AL PROCESO, a proposito: baja el quantum del GIL de 5 ms a 1 ms
        # para que el callback pueda EMPEZAR aunque otro hilo este calculando.
        # Es la mitigacion mas barata del timeout de 300 ms.
        sys.setswitchinterval(0.001)
        self._tid = w.kernel32.GetCurrentThreadId()

        hook = w.user32.SetWindowsHookExW(w.WH_KEYBOARD_LL, self._proc, self._hmod, 0)
        if hook:
            self._hook = hook
            self._hook_proc = self._proc      # el trampolin al que Windows llama de verdad
            self._last_rehook_ns = time.perf_counter_ns()
            self._install_ok = True
        else:
            err = ctypes.get_last_error()
            self._install_ok = False
            self._log.error(
                "SetWindowsHookExW fallo al arrancar (GetLastError=%s); el "
                "watchdog reintentara con backoff. Sin hook no hay dictado.", err)
        self._ready.set()

        # EL BUCLE DE MENSAJES SE ARRANCA SIEMPRE, incluso sin hook instalado.
        #
        # Antes, un fallo aqui hacia `return` y el hilo moria: la app se
        # quedaba SORDA PARA SIEMPRE, porque el watchdog solo reintenta
        # posteando WM_WISPI_REHOOK a este hilo (PostThreadMessageW exige que
        # el hilo destino tenga cola de mensajes), y sin bucle no hay quien lo
        # reciba. Ademas la condicion de reintento del watchdog estaba
        # blindada tras `elif self._hook and ...`, asi que con _hook=0 nunca
        # se disparaba ni un solo intento. Entraba en produccion en silencio:
        # app.py ignoraba el resultado de start(), logueaba "WISPI listo" y
        # sonaba el chime de "ready" sobre una app que no podia oir ni una
        # tecla. SetWindowsHookExW crea la cola de mensajes del hilo tanto si
        # tiene exito como si falla, asi que entrar aqui es seguro en los dos
        # casos.
        msg = wintypes.MSG()
        pmsg = ctypes.byref(msg)
        try:
            while True:
                r = w.user32.GetMessageW(pmsg, None, 0, 0)
                if r == 0:            # WM_QUIT
                    break
                if r == -1:           # error real de GetMessage
                    self._log.error("GetMessageW devolvio -1 (GetLastError=%s)",
                                    ctypes.get_last_error())
                    break
                if msg.message == _WM_WISPI_REHOOK:
                    ok = self._do_rehook()
                    if msg.wParam:    # solo las peticiones externas piden acuse
                        self._rehook_ok = ok
                        self._rehook_done.set()
                    continue
                w.user32.TranslateMessage(pmsg)
                w.user32.DispatchMessageW(pmsg)
        finally:
            h = self._hook
            self._hook = 0
            self._hook_proc = None
            if h:
                w.user32.UnhookWindowsHookEx(h)
            self._log.info("hook desenganchado (rehooks=%d, cb_errors=%d)",
                           self._n_rehooks, self._cb_errors)

    def stop(self) -> None:
        self._stop_evt.set()
        t = self._thread
        if t is not None and t.is_alive():
            self._post(w.WM_QUIT)
            t.join(timeout=3.0)
            if t.is_alive():
                # Ultimo recurso: el bucle no salio. Desengancha desde aqui para
                # no dejar un hook huerfano apuntando a este proceso.
                self._log.warning("el hilo del hook no termino en 3 s; desenganche forzado")
                h = self._hook
                self._hook = 0
                self._hook_proc = None
                if h:
                    w.user32.UnhookWindowsHookEx(h)
        a = self._aux
        if a is not None and a.is_alive():
            a.join(timeout=2.0)
        self._thread = None
        self._aux = None
        self._tid = 0

    def apply_config(self) -> bool:
        """Reaplica la seccion `hotkey` sin reiniciar la app (C10.3).

        Hace falta un metodo explicito porque el callback captura la config en
        variables libres para no leerla en cada tecla: cambiar `combo`,
        `accept_injected`, `suppress_start_menu` o `slow_callback_ms` obliga a
        reconstruirlo y reinstalar el hook. Lo llama el hilo de estado despues
        de que `Config.maybe_reload()` diga que hubo cambios.

        `rehook_interval_s` es la excepcion: ese lo relee el watchdog en cada
        vuelta y no necesita nada de esto.

        NO se gestiona a mano la vida del proc viejo (antes habia un
        `del old_proc` incondicional aqui). `self._hook_proc` es ahora la
        UNICA fuente de verdad de que trampolin necesita seguir vivo, y solo
        se reasigna en `_run`/`_do_rehook`, exactamente cuando `self._hook`
        cambia de verdad. El fallo que esto reemplaza: liberar el callback
        viejo dando por hecho que `rehook()` habia tenido exito; si fallaba,
        el hook activo se quedaba apuntando a memoria liberada y el proceso
        moria con STATUS_FATAL_USER_CALLBACK_EXCEPTION, sin traza ni log.
        """
        self._configure()
        self._e8_inputs = self._build_e8_inputs()
        # Los bits del bitmask cambian de significado si cambio el combo, asi
        # que arrastrar el estado anterior seria interpretarlo con la tabla mal.
        self._mask = 0
        self._active = False
        self._suppress_pending = False
        t = self._thread
        if t is None or not t.is_alive():
            return False
        # Candidato nuevo. Si rehook() falla, self._hook_proc sigue siendo el
        # viejo -que es al que Windows sigue llamando de verdad- y este objeto
        # queda vivo como candidato para el proximo intento del watchdog.
        self._proc = w.HOOKPROC(self._build_proc())
        ok = self.rehook()
        self._log.info("hotkey reconfigurado en caliente (combo=%s, accept_injected=%s): %s",
                       "+".join(self._combo_names), bool(self._cfg.accept_injected),
                       "ok" if ok else "fallo el re-enganche; se mantiene el hook anterior")
        return ok

    # ------------------------------------------------------------------
    # Re-enganche (watchdog)
    # ------------------------------------------------------------------
    def rehook(self) -> bool:
        """Desengancha y reengancha. Idempotente y seguro desde cualquier hilo.

        Windows NO avisa cuando te desengancha por pasarte del timeout, asi que
        reinstalar a ciegas cada cierto tiempo es la unica defensa que existe.
        Cuesta microsegundos.
        """
        t = self._thread
        if t is None or not t.is_alive():
            return False
        if threading.get_ident() == t.ident:
            return self._do_rehook()
        with self._rehook_lock:
            self._rehook_done.clear()
            self._rehook_ok = False
            if not self._post(_WM_WISPI_REHOOK, 1):
                return False
            if not self._rehook_done.wait(2.0):
                self._log.warning("rehook: el hilo del hook no respondio en 2 s")
                return False
            return self._rehook_ok

    def _do_rehook(self) -> bool:
        """SOLO desde el hilo del hook: el callback se entrega al hilo que instala.

        Sirve para dos casos con el mismo codigo: reenganchar un hook vivo
        (`old` != 0) y hacer el PRIMER intento tras un fallo en el arranque
        (`old` == 0, `self._hook_proc` sigue en None). `if old:` ya cubre el
        segundo caso sin rama especial: no hay nada que desenganchar.
        """
        old = self._hook
        new = w.user32.SetWindowsHookExW(w.WH_KEYBOARD_LL, self._proc, self._hmod, 0)
        if not new:
            # Se conserva el viejo a proposito: un hook posiblemente muerto es
            # mejor que ninguno, y el siguiente ciclo del watchdog reintenta.
            # `self._hook_proc` NO se toca: sigue siendo el trampolin al que
            # Windows llama de verdad (si lo hubiera), y es lo que impide el
            # use-after-free si alguien mas ya reasigno self._proc.
            self._log.error("rehook: SetWindowsHookExW fallo (GetLastError=%s)",
                            ctypes.get_last_error())
            return False
        # Instalar ANTES de desenganchar: asi no hay ni un microsegundo sin hook.
        # El coste es que durante ese instante podria entregarse un evento dos
        # veces; un COMBO_DOWN duplicado lo absorbe el flag `_active`, mientras
        # que perder un COMBO_UP dejaria la grabacion colgada para siempre.
        self._hook = new
        self._hook_proc = self._proc   # AHORA si Windows llama al proc actual
        if old:
            w.user32.UnhookWindowsHookEx(old)
        self._n_rehooks += 1
        self._last_rehook_ns = time.perf_counter_ns()
        self._install_ok = True
        return True

    def _post(self, msg: int, wparam: int = 0) -> bool:
        """PostThreadMessageW con reintentos: falla si el hilo aun no tiene cola."""
        tid = self._tid
        if not tid:
            return False
        for _ in range(100):
            if w.user32.PostThreadMessageW(tid, msg, wparam, 0):
                return True
            t = self._thread
            if t is None or not t.is_alive():
                return False
            time.sleep(0.005)
        return False

    # ------------------------------------------------------------------
    # Hilo auxiliar: supresion del menu Inicio + watchdog
    # ------------------------------------------------------------------
    def _run_aux(self) -> None:
        last_slow_rehook = 0.0
        # time.monotonic() arranca en un valor grande (uptime del sistema), no
        # en 0. Si esto empezara en 0.0, "mono - last_install_attempt" ya
        # superaria el backoff inicial en la primerisima vuelta del bucle, y
        # el primer reintento saldria practicamente instantaneo en vez de
        # esperar 1 s como dice el backoff documentado. Sin ser un bug grave
        # -reintentar antes de tiempo no hace dano-, hace el backoff real
        # impredecible; se ancla al arranque del propio hilo para que 1, 2, 4,
        # 8, 16 s sean los tiempos de verdad, no un efecto secundario de que
        # el reloj del sistema ya llevaba horas corriendo.
        last_install_attempt = time.monotonic()
        # Backoff del reintento cuando NO hay hook instalado: 1, 2, 4, 8, 16,
        # tope 30 s. Una app sorda tiene que oir lo antes posible, no esperar
        # a rehook_interval_s (300 s por defecto), que es el ritmo pensado
        # para "reinstalar por precaucion un hook que ya funciona".
        install_backoff_s = 1.0
        while not self._stop_evt.is_set():
            # Se relee cada vuelta para que el hot-reload de config.yaml surta
            # efecto sin reiniciar el hook (C10.3).
            interval_s = max(5, int(self._cfg.rehook_interval_s or 300))
            # Sondeo adaptativo: rapido solo mientras hay una tecla del combo
            # abajo, que es la unica ventana en la que puede aparecer trabajo.
            # En reposo son 20 despertares por segundo de un hilo que mira dos
            # atributos: irrelevante para la bateria y para el GIL.
            self._stop_evt.wait(0.003 if self._mask else 0.05)

            if self._suppress_pending:
                self._suppress_pending = False
                self._emit_start_menu_guard()

            self._resincronizar_mask()

            now = time.perf_counter_ns()
            mono = time.monotonic()

            if not self._hook:
                # SIN HOOK ACTIVO. Antes esta rama no existia: la condicion de
                # reintento estaba blindada tras `elif self._hook and ...`, asi
                # que con _hook=0 el watchdog no intentaba NUNCA reinstalar, y
                # la app se quedaba sorda para siempre tras un fallo transitorio
                # de arranque (agotamiento del desktop heap, cambio de sesion).
                if mono - last_install_attempt >= install_backoff_s:
                    last_install_attempt = mono
                    if self._post(_WM_WISPI_REHOOK, 0):
                        install_backoff_s = min(install_backoff_s * 2.0, 30.0)
                continue

            install_backoff_s = 1.0   # se recupero: listo para la proxima vez

            if self._slow_pending:
                self._slow_pending = False
                if mono - last_slow_rehook > 1.0:   # antirrebote
                    last_slow_rehook = mono
                    self._log.warning("callback lento (> %s ms): re-enganchando el hook",
                                      self._cfg.slow_callback_ms)
                    self._post(_WM_WISPI_REHOOK, 0)
            elif (now - self._last_rehook_ns) > interval_s * 1_000_000_000:
                self._post(_WM_WISPI_REHOOK, 0)

    def _resincronizar_mask(self) -> None:
        """Limpia los bits del combo que ya no estan fisicamente pulsados.

        POR QUE HACE FALTA
            El bitmask solo se actualiza con lo que ve el hook, y hay situaciones
            en las que el hook NO recibe el key-up: Win+L, Ctrl+Alt+Supr, un
            prompt de UAC, cualquier ventana elevada al frente, RDP. Basta con
            perder uno para que el bit se quede pegado PARA SIEMPRE, y entonces
            la otra tecla del combo basta por si sola: con el bit de Win pegado,
            un Ctrl+C se convertia en COMBO_DOWN y un Ctrl+V posterior en un
            doble toque, o sea, copiar y pegar metia a WISPI en manos libres
            grabando el microfono, de forma permanente hasta reiniciar.

            En teclado espanol hay ademas un disparador cotidiano: Windows
            antepone un LCtrl falso a AltGr, y si se pierde ese key-up queda el
            bit de Ctrl puesto.

        POR QUE AQUI Y NO EN EL CALLBACK
            GetAsyncKeyState es una llamada al kernel. Este hilo ya despierta
            cada 3-50 ms y puede permitirsela; el callback no. Ademas solo se
            consulta cuando hay algun bit puesto, asi que en reposo cuesta cero.
        """
        if not self._mask:
            return
        # bit -> VKs de su grupo, invirtiendo el mapa que ya existe.
        limpiar = 0
        for bit in range(len(self._combo_names)):
            b = 1 << bit
            if not (self._mask & b):
                continue
            vks = [vk for vk, vb in self._vk_bits.items() if vb == b]
            if not any(w.is_physically_down(v) for v in vks):
                limpiar |= b
        if not limpiar:
            return
        self._mask &= ~limpiar
        nombres = ",".join(n for i, n in enumerate(self._combo_names)
                           if limpiar & (1 << i))
        self._log.warning("teclas del combo pegadas sin key-up (%s); resincronizado",
                          nombres)
        # Si el combo estaba activo hay que cerrarlo, o app.py se queda grabando
        # hasta el tope de max_duration_s con una transcripcion que nadie pidio.
        if self._active and (self._mask & self._full_mask) != self._full_mask:
            self._active = False
            try:
                self._q.put_nowait((HookEvent.COMBO_UP, 0, time.perf_counter_ns()))
            except Exception:
                pass

    def _emit_start_menu_guard(self) -> bool:
        """Emite VK 0xE8 down+up para que Windows no abra el menu Inicio.

        0xE8 esta SIN ASIGNAR en Windows: no hace nada en ninguna aplicacion.
        Windows lo contabiliza como "hubo una tecla intermedia" y por eso deja
        de tratar la Win como pulsacion suelta al soltarla.

        Va aqui y no en el callback porque SendInput es una llamada al kernel
        que puede tardar cientos de microsegundos o mas si el hilo de entrada
        del sistema esta ocupado, y el callback no tiene ese margen.
        """
        if not (w.is_physically_down(w.VK_LWIN) or w.is_physically_down(w.VK_RWIN)):
            return False   # ya se solto: no hay Inicio que suprimir
        n = w.user32.SendInput(2, self._e8_inputs, ctypes.sizeof(w.INPUT))
        if n != 2:
            self._log.warning("supresion de Inicio: SendInput devolvio %s (GetLastError=%s)",
                              n, ctypes.get_last_error())
            return False
        return True

    # ------------------------------------------------------------------
    # Lectura desde otros hilos
    # ------------------------------------------------------------------
    @property
    def is_alive(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive() and self._hook)

    @property
    def user_key_count(self) -> int:
        """Teclas del usuario vistas. Compuerta del parche optimista: si este
        numero cambio entre insertar el crudo y volver el LLM, el usuario
        siguio escribiendo y el parche se aborta."""
        return self._user_keys

    def own_stats(self) -> dict:
        """Percentiles del coste PROPIO del callback, sin CallNextHookEx.

        Si `p99_us` de `metrics` sube pero `own_p99_us` no, el problema esta en
        el hook de otra aplicacion, no aqui, y optimizar este modulo no arregla
        nada. Es la pregunta que se hace primero cuando el hook va lento.
        """
        s = sorted(self._own_us)
        n = len(s)
        if not n:
            return {"own_n": 0}
        return {
            "own_n": n,
            "own_p50_us": s[n // 2],
            "own_p99_us": s[min(n - 1, int(n * 0.99))],
            "own_max_us": s[-1],
        }

    def stats(self) -> dict:
        s = dict(self._metrics.callback_stats())
        s.update(self.own_stats())
        s.update(
            alive=self.is_alive,
            hooked=bool(self._hook),
            combo="+".join(self._combo_names),
            n_rehooks=self._n_rehooks,
            user_keys=self._user_keys,
            cb_errors=self._cb_errors,
            combo_active=self._active,
            mask=self._mask,
            since_rehook_s=(round((time.perf_counter_ns() - self._last_rehook_ns) / 1e9, 1)
                            if self._last_rehook_ns else None),
        )
        return s
