"""Verifica los dos P0 de supervivencia del hook, hallados por la revision
adversarial del 2026-08-09.

    1. USE-AFTER-FREE en apply_config(): si el reenganche fallaba, se liberaba
       el trampolin del hook TODAVIA INSTALADO, dando por hecho que ya estaba
       desenganchado. Windows seguia llamando a esa memoria liberada ->
       STATUS_FATAL_USER_CALLBACK_EXCEPTION sin traza ni log.

    2. ARRANQUE SORDO: si SetWindowsHookExW fallaba al arrancar, el hilo del
       hook simplemente terminaba (`return`), y como el watchdog solo
       reintenta posteando un mensaje a ESE hilo, la app se quedaba sorda
       para siempre. app.py ademas ignoraba el resultado y sonaba el chime
       de "listo" sobre una instancia que no podia oir ni una tecla.

METODO
    Se instala un hook REAL (inofensivo: el callback solo encola tuplas y
    llama a CallNextHookEx, nunca traga nada) y se fuerza el fallo de
    SetWindowsHookExW envolviendo la funcion real, no con un mock completo:
    asi el bucle de mensajes, PostThreadMessageW y el watchdog son los de
    verdad, que es justo lo que hay que poner a prueba.

    El hook se desinstala siempre en el finally. No se toca la instancia de
    WISPI que pueda estar corriendo (PID en logs/wispi.pid): esta prueba usa
    su propio KeyboardHook, independiente.

    uv run python tools/test_hook_resilience.py
"""
from __future__ import annotations

import ctypes
import gc
import queue
import sys
import time
import weakref
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wispi import logging_setup, winapi                    # noqa: E402
from wispi.config import HotkeyCfg                         # noqa: E402
from wispi.hotkey import KeyboardHook                       # noqa: E402
from wispi.metrics import Metrics                           # noqa: E402


class FailToggle:
    """Envoltorio de SetWindowsHookExW que puede fallar a demanda.

    Cuando `fail` es True devuelve NULL (como un fallo real de Windows);
    cuando es False llama a la funcion REAL. El bucle de mensajes,
    PostThreadMessageW y el watchdog nunca se tocan: son los de produccion.
    """

    def __init__(self):
        self.real = winapi.user32.SetWindowsHookExW
        self.fail = False
        self.calls = 0

    def __call__(self, id_, proc, hmod, tid):
        self.calls += 1
        if self.fail:
            ctypes.set_last_error(1428)  # ERROR_HOOK_NOT_INSTALLED, plausible
            return 0
        return self.real(id_, proc, hmod, tid)


def nuevo_hook(toggle: FailToggle) -> KeyboardHook:
    cfg = HotkeyCfg(combo=["ctrl", "win"])
    q: "queue.SimpleQueue" = queue.SimpleQueue()
    m = Metrics(enabled=False)
    log = logging_setup.get("test-hook-resilience")
    h = KeyboardHook(cfg, q, m, log)
    return h


def test_uaf(toggle: FailToggle) -> list[str]:
    fallos: list[str] = []
    print("=== 1. Use-after-free en apply_config() ===")
    toggle.fail = False
    hook = nuevo_hook(toggle)
    try:
        ok = hook.start()
        if not ok or not hook.is_alive:
            fallos.append("uaf: el hook inicial no se instalo (precondicion del test)")
            return fallos

        proc_vivo = hook._hook_proc
        ref = weakref.ref(proc_vivo)
        del proc_vivo   # solo queda la referencia de hook._hook_proc

        # Ahora se fuerza el fallo del REENGANCHE, que es lo que apply_config()
        # dispara al final. self._proc ya se reasigna a un candidato nuevo
        # ANTES de intentar instalar: es justo el instante donde el bug vivia.
        toggle.fail = True
        ok2 = hook.apply_config()
        if ok2:
            fallos.append("uaf: apply_config() deberia devolver False con el "
                          "reenganche forzado a fallar")

        gc.collect()
        # LA COMPROBACION QUE IMPORTA: el trampolin del hook que Windows SIGUE
        # teniendo instalado no puede haber sido liberado.
        if ref() is None:
            fallos.append("uaf: el trampolin del hook activo se libero con el "
                          "hook TODAVIA instalado -> use-after-free real")
        else:
            print("  ok    el proc del hook activo sigue vivo tras el fallo "
                  "de reenganche (gc.collect incluido)")

        if not hook.is_alive:
            fallos.append("uaf: el hook dejo de estar 'vivo' tras el fallo; "
                          "deberia conservarse el viejo (mejor un hook stale "
                          "que ninguno)")
        else:
            print("  ok    el hook sigue instalado (el viejo, a proposito)")

        # Y la recuperacion: cuando el reenganche SI puede tener exito, el
        # candidato nuevo (self._proc) pasa a ser el activo.
        toggle.fail = False
        ok3 = hook.rehook()
        if not ok3:
            fallos.append("uaf: rehook() deberia recuperar cuando ya no falla")
        elif hook._hook_proc is not hook._proc:
            fallos.append("uaf: tras un reenganche con exito, _hook_proc "
                          "deberia ser el candidato actual")
        else:
            print("  ok    recuperado: el proc activo ahora es el candidato nuevo")
    finally:
        hook.stop()
    return fallos


def test_arranque_sordo(toggle: FailToggle) -> list[str]:
    fallos: list[str] = []
    print("\n=== 2. Arranque sordo + recuperacion del watchdog ===")
    toggle.fail = True
    hook = nuevo_hook(toggle)
    # Backoff de prueba mas corto que el de produccion (arranca en 1s en el
    # codigo real) no hace falta tocarlo: 1s/2s/4s ya caben en un test.
    try:
        t0 = time.monotonic()
        ok = hook.start()
        if ok:
            fallos.append("sordo: start() deberia devolver False con "
                          "SetWindowsHookExW forzado a fallar")
        if hook.is_alive:
            fallos.append("sordo: is_alive no puede ser True sin hook instalado")

        # LA COMPROBACION QUE IMPORTA: el hilo del mensaje sigue vivo. Antes
        # de este arreglo, _run() hacia `return` en el fallo inicial y el
        # hilo moria: sin hilo, PostThreadMessageW del watchdog no tiene a
        # quien despertar y jamas habria una segunda oportunidad.
        if not (hook._thread and hook._thread.is_alive()):
            fallos.append("sordo: el hilo del hook murio tras el fallo inicial; "
                          "el watchdog no tiene forma de reintentar")
        else:
            print("  ok    el hilo del hook sigue vivo (bucle de mensajes activo) "
                  "pese al fallo inicial")

        print("  esperando a que el watchdog reintente con backoff "
              "(hasta ~8 s)...")
        toggle.fail = False   # a partir de ahora, si se reintenta, tendra exito
        recuperado = False
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if hook.is_alive:
                recuperado = True
                break
            time.sleep(0.2)
        dt = time.monotonic() - t0

        if not recuperado:
            fallos.append("sordo: el watchdog no reengancho en 10 s; antes de "
                          "este arreglo no reenganchaba NUNCA (bug permanente)")
        else:
            print(f"  ok    el watchdog reengancho solo, en {dt:.1f} s desde "
                  f"el arranque, sin ninguna llamada externa")
    finally:
        hook.stop()
    return fallos


def main() -> int:
    logging_setup.setup("WARNING", console=True)
    toggle = FailToggle()
    winapi.user32.SetWindowsHookExW = toggle
    try:
        fallos = test_uaf(toggle) + test_arranque_sordo(toggle)
    finally:
        winapi.user32.SetWindowsHookExW = toggle.real

    print()
    if fallos:
        print(f"{len(fallos)} FALLOS:")
        for f in fallos:
            print(f"  - {f}")
        return 1
    print("hook resiliente OK: sin use-after-free al fallar el reenganche, "
          "y el watchdog reengancha solo tras un fallo de arranque")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
