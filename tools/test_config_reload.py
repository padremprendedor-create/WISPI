"""Red de regresion de la recarga en caliente. SIN microfono ni modelo.

    uv run python tools/test_config_reload.py

QUE VIGILA Y POR QUE EXISTE

`config.yaml` y el README prometen que editar una perilla surte efecto en menos de
3 segundos, sin reiniciar. Era **mentira** para todo lo que vive en `audio.py`,
`hotkey.py` e `inject/injector.py`: esos modulos guardan su seccion al construirse
y `maybe_reload()` la SUSTITUIA por un objeto nuevo, asi que seguian leyendo los
valores del arranque. Medido el 2026-08-10: `cfg.audio.tail_ms` valia 333 y
`audio.cfg.tail_ms` seguia en 200.

Lo peligroso de ese fallo no es el fallo: es que **no se ve**. No hay excepcion, ni
log, ni nada. La perilla simplemente no hace nada. Por eso hace falta una prueba, y
por eso la prueba NO mira `cfg.*` -que siempre estuvo bien- sino lo que lee EL
MODULO que consume el valor.

Escribe de verdad en un config.yaml temporal, llama a `maybe_reload()` y comprueba
el objeto que lo consume. Si alguien vuelve a sustituir las secciones en vez de
mutarlas, esto se pone rojo.
"""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wispi.config import RESTART_ONLY, Config  # noqa: E402
from wispi.logging_setup import get as _log    # noqa: E402

OK, BAD = "[ OK ]", "[FALLA]"

PLANTILLA = """\
audio:
{tail}
  silence_threshold: {silence_threshold}
  max_duration_s: {max_duration_s}
  sample_rate: {sample_rate}
hotkey:
  slow_callback_ms: {slow_callback_ms}
  combo: {combo}
injection:
  restore_clipboard: {restore_clipboard}
  terminal_apps: {terminal_apps}
wake:
  threshold: {threshold}
  min_speech_s: {min_speech_s}
  model: {model}
"""

INICIAL = dict(tail="  tail_ms: 200", silence_threshold=0.012, max_duration_s=60.0,
               sample_rate=16000, slow_callback_ms=50, combo="[ctrl, win]",
               restore_clipboard="true", terminal_apps="[cmd.exe]",
               threshold=0.75, min_speech_s=0.25, model="tiny")

CAMBIADO = dict(tail="  tail_ms: 333", silence_threshold=0.099, max_duration_s=12.0,
                sample_rate=8000, slow_callback_ms=77, combo="[ctrl, alt]",
                restore_clipboard="false", terminal_apps="[wt.exe, xterm.exe]",
                threshold=0.61, min_speech_s=0.9, model="base")

_reloj = [1_000_000.0]


def _escribir(path: Path, valores: dict) -> None:
    """Escribe y empuja el mtime hacia delante.

    `maybe_reload()` compara `st_mtime`, y dos escrituras dentro del mismo tick del
    sistema de ficheros pueden dar el mismo valor: sin esto la prueba pasaria por
    no haber recargado, que es justo el falso verde que hay que evitar.
    """
    path.write_text(PLANTILLA.format(**valores), encoding="utf-8")
    _reloj[0] += 10.0
    os.utime(path, (_reloj[0], _reloj[0]))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wispi-reload-"))
    cfg_path = tmp / "config.yaml"
    _escribir(cfg_path, INICIAL)

    cfg = Config.load(cfg_path)
    # Que no se cuele el config.local.yaml real del repo en la prueba.
    cfg.local_path = tmp / "config.local.yaml"

    # Los modulos que guardan su seccion al construirse. Se instancian de verdad,
    # sin arrancar nada: lo que se prueba es la referencia, no el hardware.
    from wispi.audio import AudioCapture
    from wispi.inject.injector import Injector
    from wispi.wake import WakeWord

    audio = AudioCapture(cfg.audio, _log("audio"))
    injector = Injector(cfg.injection, _log("inject"))
    wake = WakeWord(cfg.wake, cfg.asr, audio, None, _log("wake"))
    # `hotkey.py` guarda la suya en `_cfg`. No se instancia porque su __init__
    # construye estructuras de Win32; se comprueba la referencia, que es lo mismo
    # que se comprueba en los demas.
    hotkey_cfg = cfg.hotkey

    print(f"Antes:  audio.cfg.tail_ms={audio.cfg.tail_ms}  "
          f"injector.cfg.restore_clipboard={injector.cfg.restore_clipboard}")

    _escribir(cfg_path, CAMBIADO)
    changed, warns = cfg.maybe_reload()
    print(f"maybe_reload() -> changed={changed}, {len(warns)} aviso(s)")
    for w in warns:
        print(f"   aviso: {w}")

    fallos = 0

    def check(etiqueta: str, real, esperado) -> None:
        nonlocal fallos
        bien = real == esperado
        extra = "" if bien else f"   <- se esperaba {esperado!r}"
        print(f"  {OK if bien else BAD} {etiqueta:36s} = {real!r}{extra}")
        if not bien:
            fallos += 1

    print("\nEn caliente, leido por el modulo que lo consume:\n")
    check("audio.cfg.tail_ms", audio.cfg.tail_ms, 333)
    check("audio.cfg.silence_threshold", audio.cfg.silence_threshold, 0.099)
    check("audio.cfg.max_duration_s", audio.cfg.max_duration_s, 12.0)
    check("hotkey._cfg.slow_callback_ms", hotkey_cfg.slow_callback_ms, 77)
    check("injector.cfg.restore_clipboard", injector.cfg.restore_clipboard, False)
    check("injector.cfg.terminal_apps", injector.cfg.terminal_apps,
          ["wt.exe", "xterm.exe"])
    check("wake.cfg.threshold", wake.cfg.threshold, 0.61)
    check("wake.cfg.min_speech_s", wake.cfg.min_speech_s, 0.9)

    print("\nDe reinicio: se conserva el valor VIVO y ademas se avisa:\n")
    for etiqueta, real, esperado, clave in [
        ("audio.cfg.sample_rate", audio.cfg.sample_rate, 16000, "audio.sample_rate"),
        ("hotkey._cfg.combo", hotkey_cfg.combo, ["ctrl", "win"], "hotkey.combo"),
        ("wake.cfg.model", wake.cfg.model, "tiny", "wake.model"),
    ]:
        conservado = real == esperado
        avisado = any(clave in w for w in warns)
        bien = conservado and avisado
        print(f"  {OK if bien else BAD} {etiqueta:36s} = {real!r}  "
              f"conservado={conservado} avisado={avisado}")
        if not bien:
            fallos += 1

    print("\nIdentidad de las secciones (la raiz del fallo original):\n")
    for nombre, obj, seccion in [("audio", audio.cfg, cfg.audio),
                                 ("injection", injector.cfg, cfg.injection),
                                 ("wake", wake.cfg, cfg.wake),
                                 ("hotkey", hotkey_cfg, cfg.hotkey)]:
        bien = obj is seccion
        print(f"  {OK if bien else BAD} el modulo y cfg.{nombre} son el MISMO objeto")
        if not bien:
            fallos += 1

    print("\nBorrar una clave del YAML devuelve al default:\n")
    sin_tail = dict(CAMBIADO, tail="  # tail_ms borrado")
    _escribir(cfg_path, sin_tail)
    cfg.maybe_reload()
    check("audio.cfg.tail_ms tras borrarla", audio.cfg.tail_ms, 200)

    print("\nCoherencia de RESTART_ONLY:\n")
    huerfanas = 0
    for sec, claves in RESTART_ONLY.items():
        seccion = getattr(cfg, sec, None)
        if seccion is None:
            print(f"  {BAD} nombra la seccion inexistente '{sec}'")
            huerfanas += 1
            continue
        validas = {f.name for f in fields(seccion)}
        for k in claves:
            if k not in validas:
                print(f"  {BAD} {sec}.{k} no existe en la dataclass")
                huerfanas += 1
    fallos += huerfanas
    if not huerfanas:
        n = sum(len(v) for v in RESTART_ONLY.values())
        print(f"  {OK} las {n} claves de reinicio existen en su dataclass")

    print()
    if fallos:
        print(f"{BAD} {fallos} comprobacion(es) fallidas")
        return 1
    print(f"{OK} la recarga en caliente llega a los modulos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
