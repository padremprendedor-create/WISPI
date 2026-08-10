"""Regresion de la gracia inicial del modo manos libres.

POR QUE EXISTE ESTE TEST EN CONCRETO
    `start_grace_s` estuvo INERTE y nadie lo noto. El guardia comparaba contra
    `preroll_frames + rec_frames`, y con la config real el pre-roll (300 ms) ya
    igualaba el umbral de gracia (300 ms): la condicion salia falsa en el primer
    bloque y la gracia efectiva era CERO. En manos libres eso adelantaba el corte
    por silencio de 1,5 s a 1,2 s.

    Lo que hizo que pasara desapercibido: se midio con el ring VACIO. Ese estado
    solo existe en los primeros 300 ms tras start() y la app nunca lo alcanza en
    un dictado real, porque el stream lleva rato abierto y el ring lleno.

    De ahi la forma de este test: llena el ring PRIMERO y mide despues. Un test
    que no lo haga volveria a dar verde con el bug puesto.

    uv run python tools/test_grace.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wispi import logging_setup                 # noqa: E402
from wispi.audio import AudioCapture            # noqa: E402
from wispi.config import Config                 # noqa: E402


def main() -> int:
    logging_setup.setup("ERROR", console=True)
    cfg = Config.load()
    rate, grace = cfg.audio.sample_rate, cfg.audio.start_grace_s
    preroll_frames = cfg.audio.preroll_blocks * cfg.audio.blocksize
    umbral = int(grace * rate)

    print(f"preroll_ms={cfg.audio.preroll_ms}  start_grace_s={grace}  "
          f"silence_duration_s={cfg.audio.silence_duration_s}")
    print(f"frames de preroll = {preroll_frames}   umbral de gracia = {umbral}")
    if preroll_frames >= umbral:
        print("  (nota: el preroll iguala o supera el umbral; es justo la "
              "configuracion que destapaba el bug)")

    cap = AudioCapture(cfg.audio, logging_setup.get("audio"))
    cap.start()
    if cap.last_error:
        print(f"FALLA: no hay audio ({cap.last_error})")
        return 1

    print("\nllenando el ring 2 s (condicion REAL: el stream lleva rato abierto)...")
    time.sleep(2.0)

    cap.begin_recording()
    t0 = time.perf_counter()
    muestras = []
    for objetivo in (0.10, 0.20, 0.30, 0.50, 1.00, 1.30, 1.50, 1.70):
        while time.perf_counter() - t0 < objetivo:
            time.sleep(0.01)
        muestras.append((objetivo, cap.silence_elapsed_s()))
    cap.cancel_recording()
    cap.stop()

    bloque_s = cfg.audio.block_ms / 1000.0
    print(f"\n{'t desde begin':>14s}  {'silence_elapsed_s':>18s}   esperado")
    ok = True
    for t, s in muestras:
        # En el borde exacto se acepta cualquiera de los dos lados: el muestreo
        # tiene la granularidad de un bloque y clavar el instante seria medir el
        # reloj, no el comportamiento.
        if abs(t - grace) <= bloque_s * 1.5:
            esperado, bien = "borde (cualquiera)", (s <= bloque_s * 2)
        elif t < grace:
            esperado, bien = "0.0 (en gracia)", (s == 0.0)
        else:
            ideal = t - grace
            esperado, bien = f"~{ideal:.2f}", abs(s - ideal) < 0.20
        ok &= bien
        print(f"{t:13.2f}s  {s:17.3f}s   {esperado:18s} {'ok' if bien else 'FALLA'}")

    corte = grace + cfg.audio.silence_duration_s
    print(f"\nEl corte por silencio debe caer a ~{corte:.1f}s del key-down.")
    print("RESULTADO:", "gracia efectiva OK" if ok else "LA GRACIA SIGUE ROTA")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
