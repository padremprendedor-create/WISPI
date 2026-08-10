"""La cascada de inyeccion de texto. Punto de fallo numero 2 del proyecto.

Por que una cascada y no "Ctrl+V y ya":
    Existe un incidente real y documentado -anthropics/claude-code#38620- donde
    el Ctrl+V simulado de Wispr Flow, un producto comercial financiado, dejo de
    funcionar al dictar a Claude Code en Windows Terminal. La inyeccion de texto
    en Windows es fragil POR DISENO: cada clase de aplicacion decide como recibe
    el pegado. Se disena con peldanos desde el principio, no como parche.

Los peldanos, en orden:
    1. Portapapeles + Ctrl+V          -> aplicaciones graficas normales.
    2. Portapapeles + Shift+Insert    -> consolas (Windows Terminal, cmd,
       PowerShell, clientes SSH), donde Ctrl+V simplemente no pega.
    3. SendInput con KEYEVENTF_UNICODE caracter a caracter -> lo que sobreviva a
       todo lo demas. No toca el portapapeles y no depende del layout.

Regla que no se negocia: NUNCA se inyecta un salto de linea final. Un prompt
dictado a Claude Code se auto-enviaria antes de que el usuario lo lea (C4.3).
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import winapi as w
from ..logging_setup import get as _get_logger
from . import clipboard, sendinput
from .target import Target, current_target, is_terminal, matches_app_list

if TYPE_CHECKING:  # solo para tipos: no queremos dependencia dura con config.py
    from ..config import InjectionCfg

# Por encima de esto no se parchea. Seleccionar 400 caracteres hacia atras son
# 800 eventos de teclado: si la aplicacion se come uno solo, la seleccion queda
# desalineada y el parche escribe encima de texto que no era suyo. Es mejor
# dejar el crudo que corromper el documento del usuario.
MAX_PATCH_CHARS = 400

ROUTES = ("ctrl_v", "shift_insert", "unicode", "none")

_NEWLINE_RUN = re.compile(r"[ \t]*\n[ \t\n]*")


@dataclass
class InjectResult:
    ok: bool
    route: str
    chars: int
    error: str | None = None


class Injector:
    """Escribe texto en la ventana destino. Sin estado del dictado, solo del portapapeles."""

    def __init__(self, cfg: "InjectionCfg", log=None) -> None:
        self.cfg = cfg
        self.log = log if log is not None else _get_logger("inject")
        self._lock = threading.Lock()
        self._restore_timer: threading.Timer | None = None
        self._pending_snap: clipboard.Snapshot | None = None
        # Token de generacion para la auto-repeticion del teclado tactil.
        # El panel encola una pulsacion cada 55 ms pero el worker consume mucho
        # mas despacio, asi que soltar el dedo dejaba una cola de retrocesos que
        # se seguian aplicando hasta 3 s DESPUES, ya en otra ventana. Al soltar
        # se incrementa el token y todo lo encolado con el anterior se descarta.
        self._gen = 0

    # -- guarda de destino -------------------------------------------------
    def bump_generation(self) -> int:
        """Invalida todo el trabajo encolado con el token anterior."""
        with self._lock:
            self._gen += 1
            return self._gen

    @property
    def generation(self) -> int:
        return self._gen

    def _destino_ok(self, tgt: Target | None, que: str) -> bool:
        """La ventana en foco AHORA sigue siendo la que se eligio.

        Es la comprobacion que faltaba en el producto: existia solo en el arnes
        de pruebas (tools/e2e_pipeline.py), y por eso el criterio C4.6 estaba
        marcado como verificado sin estarlo. Entre que se decide el destino y
        que se escribe pasan 1,5-2 s de transcripcion, o el tiempo que la
        pulsacion pasa en la cola: un Alt-Tab en ese hueco mandaba el texto -o
        una rafaga de retrocesos- a la ventana equivocada, y el injector lo
        reportaba como exito porque SendInput habia aceptado los eventos.
        """
        if tgt is None or not tgt.hwnd:
            return True                     # no habia expectativa que romper
        ahora = current_target()
        if ahora.hwnd == tgt.hwnd and ahora.pid == tgt.pid:
            return True
        self.log.warning("%s abortado: la ventana cambio (%s -> %s)",
                         que, tgt.exe or "?", ahora.exe or "?")
        return False

    # -- decision de ruta --------------------------------------------------
    def choose_route(self, target: Target | None) -> str:
        """Que peldano se intenta primero. `injection.route` distinto de auto manda."""
        forced = str(getattr(self.cfg, "route", "auto") or "auto").strip().lower()
        if forced and forced != "auto":
            if forced in ROUTES:
                return forced
            self.log.warning("injection.route=%r desconocida; se usa auto", forced)
        if target is not None and is_terminal(target.exe, self.cfg.terminal_apps):
            return "shift_insert"
        return "ctrl_v"

    # -- API publica -------------------------------------------------------
    def insert(self, text: str, target: Target | None = None) -> InjectResult:
        """Escribe `text` en la ventana destino recorriendo la cascada."""
        tgt = target if target is not None else current_target()
        route = self.choose_route(tgt)
        terminal = is_terminal(tgt.exe, self.cfg.terminal_apps)
        body = self._normalize(text, collapse_newlines=terminal)
        if not body:
            return InjectResult(False, route, 0, "texto vacio tras normalizar")
        if not self._destino_ok(target, "insercion"):
            # Se deja en el portapapeles en vez de escribirlo donde no toca:
            # perder una transcripcion es un fastidio, escribirla en la ventana
            # de otro -o disparar sus atajos, caracter a caracter- no lo es.
            clipboard.set_text(body)
            return InjectResult(False, route, 0,
                                "cambio de ventana; el texto quedo en el portapapeles")
        self._settle_modifiers()
        return self._write(body, route)

    def replace(self, old_len: int, new_text: str,
                target: Target | None = None,
                verify_against: str | None = None) -> InjectResult:
        """Parche optimista: selecciona `old_len` hacia atras y escribe encima.

        Las cuatro compuertas que deciden SI procede parchear (misma ventana,
        mismo hwnd, el usuario no ha escrito, dentro de plazo) las evalua app.py.
        Aqui solo se rechaza lo que es peligroso por definicion.

        `verify_against`, si se da, es el texto que CREEMOS que ocupa `old_len`
        caracteres en el documento (lo que se inserto la primera vez). Antes de
        escribir encima se copia la seleccion y se compara: `old_len` solo
        prueba que SendInput acepto los eventos del primer Ctrl+V, no que la
        app pegara ni cuantas posiciones de cursor ocupa lo pegado. Si peg
        menos -paste bloqueado, maxlength, autocorreccion, un salto de linea
        que en un Edit de Win32 cuenta como CRLF (2 posiciones, no 1)-, la
        seleccion hacia atras se comeria texto que el usuario escribio de
        verdad. Sin `verify_against` se parchea a ciegas, como antes.
        """
        tgt = target if target is not None else current_target()
        route = self.choose_route(tgt)

        # is_terminal implica no-parche SIEMPRE, no solo si el usuario mantuvo
        # no_patch_apps sincronizada con terminal_apps. Son dos listas distintas
        # en config.yaml y alacritty/wezterm estaban en una y no en la otra:
        # Shift+Left en una TUI no selecciona nada y el pegado posterior deja el
        # crudo y el pulido concatenados en la linea de comandos.
        if (matches_app_list(tgt.exe, self.cfg.no_patch_apps)
                or is_terminal(tgt.exe, self.cfg.terminal_apps)):
            return InjectResult(False, route, 0, f"{tgt.exe or '?'} no admite parche")
        if not self._destino_ok(target, "parche"):
            return InjectResult(False, route, 0, "cambio de ventana")
        if old_len < 0:
            return InjectResult(False, route, 0, f"old_len invalido: {old_len}")
        if old_len > MAX_PATCH_CHARS:
            return InjectResult(
                False, route, 0,
                f"old_len={old_len} > {MAX_PATCH_CHARS}: no se parchea (riesgo de corromper)",
            )

        terminal = is_terminal(tgt.exe, self.cfg.terminal_apps)
        body = self._normalize(new_text, collapse_newlines=terminal)
        if not body:
            # Sin esto seleccionariamos texto y no escribiriamos nada encima:
            # la seleccion se quedaria viva y la siguiente tecla la borraria.
            return InjectResult(False, route, 0, "texto de reemplazo vacio: no se selecciona nada")

        self._settle_modifiers()
        if old_len:
            out = sendinput.send_shift_left(old_len)
            if not out.ok:
                return InjectResult(False, route, 0, f"seleccion hacia atras fallo: {out.error}")

            if verify_against is not None:
                esperado = self._normalize(verify_against, collapse_newlines=False)
                ok_verify, motivo = self._verificar_seleccion(esperado)
                if not ok_verify:
                    # Colapsar la seleccion al extremo derecho SIN Shift antes
                    # de soltarla: dejarla viva es dejar una tecla de distancia
                    # hasta que algo la borre sola, que es exactamente el
                    # "se queda seleccion viva" que este mismo modulo documenta
                    # como peligroso mas abajo.
                    sendinput.send_key(w.VK_RIGHT)
                    self.log.warning("parche abortado: %s", motivo)
                    return InjectResult(False, route, 0, motivo)
        return self._write(body, route)

    def _verificar_seleccion(self, esperado: str) -> tuple[bool, str]:
        """Copia lo seleccionado y comprueba que es EXACTAMENTE lo esperado.

        Se implementa con lo que ya existe -portapapeles + Ctrl+C- para no
        anadir una dependencia nueva (UI Automation seria la via "correcta"
        para leer la seleccion sin tocar el portapapeles, pero es una pieza
        entera y no cabe en el alcance de este arreglo). El portapapeles del
        usuario se preserva: se fotografia ANTES de tocar nada y se repone
        antes de devolver el control, para que la foto que tome
        `_via_clipboard` a continuacion sea la del usuario, no la de esta
        verificacion. Se hace con `clipboard.snapshot()/restore()`
        directamente, no con `self._take_snapshot()`: esa otra via toca el
        temporizador de restauracion pendiente entre dictados encadenados, y
        esta verificacion tiene que quedar fuera de esa contabilidad.
        """
        snap = clipboard.snapshot()
        try:
            out = sendinput.send_key(w.VK_C, (w.VK_CONTROL,))
            if not out.ok:
                return False, f"no se pudo copiar para verificar: {out.error}"
            settle = int(getattr(self.cfg, "settle_ms", 25) or 0) / 1000.0
            if settle > 0:
                time.sleep(settle)
            got = clipboard.get_text()
        finally:
            clipboard.restore(snap)
        if got is None:
            return False, "no se pudo leer la seleccion para verificar"
        # CRLF -> LF: un control Edit de Win32 devuelve \r\n por cada salto de
        # linea al copiar, aunque nosotros solo mandamos \n. Sin normalizar
        # esto, el propio desfase de un caracter por salto que el parche debe
        # detectar en casos REALES se confundiria con este caso INOCENTE.
        got = got.replace("\r\n", "\n").replace("\r", "\n")
        if got != esperado:
            return False, (f"la seleccion no coincide con lo esperado "
                           f"({len(got)} chars vs {len(esperado)}); no se parchea")
        return True, ""

    def shutdown(self) -> None:
        """Restaura el portapapeles ya, sin esperar al timer. Para el cierre limpio."""
        with self._lock:
            timer, snap = self._restore_timer, self._pending_snap
            self._restore_timer, self._pending_snap = None, None
        if timer is not None:
            timer.cancel()
        if snap is not None:
            clipboard.restore(snap)

    # -- normalizacion -----------------------------------------------------
    def _normalize(self, text: str, collapse_newlines: bool) -> str:
        """Aplica las reglas que no dependen de la ruta.

        `never_send_enter` quita CUALQUIER salto final (C4.3). En consolas ademas
        se colapsan los saltos internos a un espacio: en una terminal cada salto
        de linea EJECUTA lo escrito hasta ahi, asi que un dictado de dos frases
        con salto lanzaria media orden.
        """
        if not text:
            return ""
        t = text.replace("\r\n", "\n").replace("\r", "\n")
        # El orden importa: primero se quitan los saltos finales y luego se
        # colapsan los internos. Al reves, un "texto\n\n" acabaria en "texto "
        # con un espacio colgando que nadie pidio.
        if getattr(self.cfg, "never_send_enter", True):
            t = t.rstrip("\n")
        if collapse_newlines:
            t = _NEWLINE_RUN.sub(" ", t)
        # Un texto que solo tiene espacios es basura del pipeline, no contenido.
        # Tratarlo como vacio importa sobre todo en replace(): si no, se
        # seleccionarian caracteres reales del usuario para escribir blancos
        # encima, que es exactamente el "corromper el documento" que se evita.
        if not t.strip():
            return ""
        return t

    # -- teclas sueltas ----------------------------------------------------
    def press(self, vk: int, modifiers: tuple[int, ...] = (), count: int = 1,
              etiqueta: str = "", target: Target | None = None,
              gen: int | None = None) -> InjectResult:
        """Pulsa una tecla en la ventana en foco. Es lo que sustituye al teclado.

        NO pasa por la cascada de rutas: una tecla no es texto. Enter es Enter en
        todas partes, y meterlo por el portapapeles no tiene sentido.

        Si comparte la espera de modificadores con insert(), y por el mismo
        motivo: el usuario acaba de soltar Ctrl+Win, y si mandamos la tecla con
        Win aun fisicamente abajo, Windows la lee como Win+tecla. Un "Enter"
        recien dictado se convertiria en Win+Enter, que abre el Narrador.
        """
        nombre = etiqueta or f"vk{vk:#04x}"
        # Trabajo de una rafaga que el usuario ya solto: se descarta sin emitir.
        if gen is not None and gen != self._gen:
            return InjectResult(False, "key", 0, "rafaga cancelada")
        # La guarda de destino es OBLIGATORIA aqui, no opcional: entre estas
        # teclas hay Retroceso y Supr, y una rafaga en la ventana equivocada no
        # escribe de mas, BORRA trabajo del usuario.
        if not self._destino_ok(target, f"tecla {nombre}"):
            return InjectResult(False, "key", 0, "cambio de ventana")
        self._settle_modifiers()
        out = sendinput.send_key(vk, modifiers, count)
        if not out.ok:
            self.log.warning("tecla %s fallo: %s", nombre, out.error)
        return InjectResult(out.ok, "key", count if out.ok else 0,
                            None if out.ok else out.error)

    # -- fontaneria --------------------------------------------------------
    def _settle_modifiers(self) -> None:
        timeout = int(getattr(self.cfg, "modifier_release_timeout_ms", 200))
        ok, waited = sendinput.wait_modifiers_released(timeout)
        if ok:
            return
        freed = sendinput.release_stuck_modifiers()
        self.log.warning(
            "modificadores aun pulsados tras %.0f ms; key-ups sinteticos para %s",
            waited, ",".join(sendinput.vk_name(v) for v in freed) or "ninguno",
        )

    def _write(self, body: str, route: str) -> InjectResult:
        if route == "none":
            ok = clipboard.set_text(body)
            # chars=0 AUNQUE haya ido bien: esta ruta copia al portapapeles y NO
            # teclea nada en la ventana. Devolver len(body) hacia que app.py
            # creyera haber escrito N caracteres, y el parche posterior
            # seleccionaba N caracteres hacia atras del documento REAL del
            # usuario para escribir encima. `chars` significa "escrito en el
            # destino", no "procesado".
            return InjectResult(ok, "none", 0,
                                None if ok else "no se pudo escribir el portapapeles")
        if route == "unicode":
            return self._via_unicode(body)

        res = self._via_clipboard(body, route)
        if res.ok:
            return res
        # Peldano 3: lo que sobreviva a todo lo demas.
        self.log.warning("ruta %s fallo (%s); se cae a unicode", route, res.error)
        return self._via_unicode(body)

    def _via_unicode(self, body: str) -> InjectResult:
        # Los saltos internos se convierten en espacio TAMBIEN aqui: por teclado,
        # un salto es la tecla Enter, y en un chat web (WhatsApp, Slack) eso
        # envia el mensaje a medias. La ruta unicode es el ultimo recurso y se
        # elige seguridad sobre fidelidad del formato.
        flat = _NEWLINE_RUN.sub(" ", body)
        chunk = int(getattr(self.cfg, "unicode_chunk", 20) or 20)
        out = sendinput.send_unicode(flat, chunk=chunk)
        if not out.ok:
            return InjectResult(False, "unicode", 0, out.error)
        return InjectResult(True, "unicode", len(flat), None)

    def _via_clipboard(self, body: str, route: str) -> InjectResult:
        snap = self._take_snapshot()
        if not clipboard.set_text(body):
            # Restaurar y no simplemente olvidar la foto: si este dictado venia
            # encadenado a otro, `_take_snapshot` cancelo el timer pendiente y el
            # portapapeles contiene NUESTRO texto anterior. Descartar la foto aqui
            # dejaria al usuario sin lo suyo y con un dictado viejo pegado.
            self._restore_now(snap)
            return InjectResult(False, route, 0, "no se pudo escribir el portapapeles")

        seq = clipboard.sequence_number()
        settle = int(getattr(self.cfg, "settle_ms", 25) or 0)
        if settle > 0:
            # Sin esta pausa el Ctrl+V puede llegar antes de que la app destino
            # vea el portapapeles nuevo, y pega lo anterior.
            time.sleep(settle / 1000.0)

        out = (sendinput.send_shift_insert() if route == "shift_insert"
               else sendinput.send_ctrl_v())
        if not out.ok:
            # No se pego nada: se restaura ya, sin esperar al timer.
            self._restore_now(snap)
            return InjectResult(False, route, 0, out.error)

        self._schedule_restore(snap, seq)
        return InjectResult(True, route, len(body), None)

    # -- portapapeles: foto y restauracion ---------------------------------
    def _take_snapshot(self) -> clipboard.Snapshot | None:
        """Foto de lo que hay que devolver al portapapeles cuando acabemos.

        Si ya habia una restauracion pendiente, el portapapeles contiene NUESTRO
        texto del dictado anterior, no el del usuario: se cancela el timer y se
        arrastra la foto vieja. Sacar una foto nueva aqui congelaria nuestro
        propio texto como si fuera del usuario y lo perderia para siempre.
        """
        with self._lock:
            if self._restore_timer is not None:
                self._restore_timer.cancel()
                self._restore_timer = None
                return self._pending_snap
            snap = clipboard.snapshot()
            self._pending_snap = snap
            return snap

    def _restore_now(self, snap: clipboard.Snapshot | None) -> None:
        with self._lock:
            self._pending_snap = None
        if snap is not None:
            clipboard.restore(snap)

    def _schedule_restore(self, snap: clipboard.Snapshot | None, seq: int) -> None:
        if not getattr(self.cfg, "restore_clipboard", True):
            with self._lock:
                self._pending_snap = None
            return
        delay = max(0, int(getattr(self.cfg, "restore_delay_ms", 400))) / 1000.0
        timer = threading.Timer(delay, self._do_restore, args=(snap, seq))
        timer.daemon = True   # no puede impedir que el proceso termine
        with self._lock:
            self._pending_snap = snap
            self._restore_timer = timer
        timer.start()

    def _do_restore(self, snap: clipboard.Snapshot | None, seq: int) -> None:
        with self._lock:
            self._restore_timer = None
            self._pending_snap = None
        try:
            cur = clipboard.sequence_number()
            if seq and cur and cur != seq:
                # Alguien copio algo entre el pegado y ahora: restaurar pisaria
                # lo que el usuario acaba de poner. Se deja como esta.
                self.log.debug("portapapeles cambiado (seq %d -> %d); no se restaura", seq, cur)
                return
            if snap is None or not snap.had_text:
                self.log.debug("no habia texto restaurable; se queda el dictado en el portapapeles")
                return
            if not clipboard.restore(snap):
                self.log.warning("no se pudo restaurar el portapapeles")
        except Exception:
            self.log.exception("fallo restaurando el portapapeles")
