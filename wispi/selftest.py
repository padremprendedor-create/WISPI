"""Diagnosticos aislados. Cuando WISPI "no funciona", esto dice QUE parte.

    uv run python -m wispi.selftest --all
    uv run python -m wispi.selftest --audio --asr

POR QUE POR PARTES
    El pipeline tiene cinco eslabones (hook, audio, ASR, post-proceso,
    inyeccion) y cuatro de ellos pueden fallar de forma silenciosa. Un
    "no me escribe nada" sin diagnostico obliga a leer codigo. Con esto,
    obliga a leer una linea de salida.
"""
from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from . import logging_setup
from .config import Config

OK, BAD, WARN = "[ OK ]", "[FALLA]", "[AVISO]"


def _hr(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def test_audio(cfg: Config) -> bool:
    _hr("AUDIO")
    import sounddevice as sd
    from .audio import AudioCapture

    print("Dispositivos de entrada:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            api = sd.query_hostapis(d["hostapi"])["name"]
            mark = " <- default" if i == sd.default.device[0] else ""
            print(f"  [{i:2d}] {d['name']}  ({api}){mark}")

    cap = AudioCapture(cfg.audio, logging_setup.get("audio"))
    try:
        cap.start()
    except Exception as e:
        print(f"{BAD} no pude abrir el stream: {e}")
        return False
    if cap.last_error:
        print(f"{BAD} {cap.last_error}")
        return False
    print(f"{OK} stream abierto: {cap.device_info()}")

    print("\nGrabando 3 s de ambiente... (habla si quieres ver el pico subir)")
    cap.begin_recording()
    for i in range(3, 0, -1):
        print(f"  {i}...  rms={cap.peak_rms:.5f}", end="\r")
        time.sleep(1)
    audio = cap.end_recording()
    cap.stop()

    dur = len(audio) / cfg.audio.sample_rate
    rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
    print(f"\n  dtype={audio.dtype}  shape={audio.shape}  dur={dur:.2f}s  rms={rms:.5f}")

    ok = True
    if audio.dtype != np.float32:
        print(f"{BAD} el contrato exige float32, llego {audio.dtype}")
        ok = False
    if audio.ndim != 1:
        print(f"{BAD} el contrato exige mono 1-D, llego {audio.ndim}-D")
        ok = False
    if dur < 2.5:
        print(f"{WARN} solo {dur:.2f}s para 3 s pedidos; revisa el ring de pre-roll")
    if rms < cfg.audio.silence_threshold:
        print(f"{WARN} rms {rms:.5f} < umbral {cfg.audio.silence_threshold}: "
              f"si estabas hablando, el gate descartara tus dictados")
    else:
        print(f"{OK} rms por encima del umbral")
    return ok


def test_hook(cfg: Config, seconds: int = 20) -> bool:
    _hr("HOOK DE TECLADO")
    import queue as _q
    from .hotkey import KeyboardHook
    from .metrics import Metrics

    q: _q.SimpleQueue = _q.SimpleQueue()
    m = Metrics(enabled=False)
    hook = KeyboardHook(cfg.hotkey, q, m, logging_setup.get("hotkey"))
    try:
        hook.start()
    except Exception as e:
        print(f"{BAD} no pude instalar el hook: {e}")
        return False
    if not hook.is_alive:
        print(f"{BAD} el hook no quedo vivo tras start()")
        return False
    print(f"{OK} hook instalado. Combo: {'+'.join(cfg.hotkey.combo)}")
    print(f"\nPulsa el combo unas cuantas veces durante {seconds} s...")

    n = 0
    t_end = time.time() + seconds
    while time.time() < t_end:
        try:
            kind, vk, t_ns = q.get(timeout=0.2)
            n += 1
            print(f"  evento {n:3d}: {kind.name if hasattr(kind, 'name') else kind} vk=0x{vk:02X}")
        except _q.Empty:
            pass

    st = hook.stats()
    hook.stop()
    print(f"\n  eventos: {n}")
    print(f"  callback: {st}")

    p99 = st.get("p99_us")
    if p99 is None:
        print(f"{WARN} sin muestras del callback (no se pulso nada?)")
        return True
    # Presupuesto de Windows: 300 ms. Objetivo interno del SPEC (C3.3): < 5 ms.
    if p99 < 5000:
        print(f"{OK} p99 {p99} us < 5000 us (criterio C3.3)")
        return True
    print(f"{BAD} p99 {p99} us: Windows puede desenganchar el hook")
    return False


def test_asr(cfg: Config) -> bool:
    _hr("ASR")
    from .asr.registry import build_with_fallback

    log = logging_setup.get("asr")
    t0 = time.perf_counter()
    try:
        asr, warns = build_with_fallback(cfg.asr, log)
    except Exception as e:
        print(f"{BAD} no pude cargar ningun backend: {e}")
        return False
    print(f"{OK} {asr.describe()}  (carga {time.perf_counter()-t0:.2f}s)")
    for w in warns:
        print(f"{WARN} fallback: {w}")

    asr.warmup()
    # 10 s de silencio: mide el PISO de latencia, que es lo que domina en
    # dictados cortos porque whisper rellena a 30 s y corre el encoder entero.
    audio = np.zeros(cfg.audio.sample_rate * 10, dtype=np.float32)
    t0 = time.perf_counter()
    res = asr.transcribe(audio, language=cfg.asr.language)
    ms = (time.perf_counter() - t0) * 1000
    print(f"  10 s de silencio -> {ms:.0f} ms  texto={res.text!r}")
    if res.text.strip():
        print(f"{WARN} el modelo alucino sobre silencio: {res.text!r}")
        print("        (por eso existe el gate de RMS previo al ASR)")
    if ms < 2000:
        print(f"{OK} piso de latencia {ms:.0f} ms < 2000 ms (criterio C5.1)")
        return True
    print(f"{BAD} piso {ms:.0f} ms: demasiado para dictado. Baja de modelo.")
    return False


def test_llm(cfg: Config) -> bool:
    _hr("LLM (nivel 1)")
    from .postprocess.pipeline import PostProcessor

    post = PostProcessor(cfg.postprocess, cfg.llm, logging_setup.get("post"))
    if not cfg.llm.enabled:
        print(f"{WARN} nivel 1 desactivado en config")
        return True
    t0 = time.perf_counter()
    ok = post.warmup()
    print(f"  warmup: {'ok' if ok else 'NO DISPONIBLE'} ({(time.perf_counter()-t0)*1000:.0f} ms)")
    if not ok:
        print(f"{WARN} Ollama no responde en {cfg.llm.base_url}. "
              f"WISPI seguira funcionando solo con nivel 0.")
        return True

    muestra = ("eh bueno o sea lo que quiero es este que revises el flujo completo "
               "desde que entra la peticion hasta que sale la respuesta y me digas "
               "en que paso se pierde la informacion del usuario sabes")
    t0 = time.perf_counter()
    out = post.level1(muestra)
    ms = (time.perf_counter() - t0) * 1000
    print(f"\n  entrada ({len(muestra.split())} palabras): {muestra[:70]}...")
    print(f"  salida  ({ms:.0f} ms): {out!r}")
    if out is None:
        print(f"{WARN} la guarda de salida descarto la respuesta (es un comportamiento valido)")
    else:
        print(f"{OK} nivel 1 operativo")
    return True


def test_inject(cfg: Config) -> bool:
    _hr("INYECCION DE TEXTO")
    from .inject.injector import Injector
    from .inject.target import current_target

    tgt = current_target()
    print(f"  ventana en foco: exe={tgt.exe!r} pid={tgt.pid} hwnd={tgt.hwnd}")
    inj = Injector(cfg.injection, logging_setup.get("inject"))
    print(f"  ruta elegida para esta ventana: {inj.choose_route(tgt)}")
    print(f"\n  Vas a ver aparecer 'WISPI-CANARIO-001' donde tengas el cursor.")
    print("  Pon el foco donde quieras y espera 4 s...")
    for i in range(4, 0, -1):
        print(f"    {i}...", end="\r")
        time.sleep(1)
    tgt = current_target()
    r = inj.insert("WISPI-CANARIO-001", tgt)
    print(f"\n  resultado: ok={r.ok} ruta={r.route} chars={r.chars} error={r.error}")
    if r.ok:
        print(f"{OK} inyeccion ejecutada en {tgt.exe!r} "
              f"(comprueba con los ojos que llego)")
    else:
        print(f"{BAD} {r.error}")
    return r.ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wispi.selftest")
    for f in ("audio", "hook", "asr", "llm", "inject", "all"):
        ap.add_argument(f"--{f}", action="store_true")
    ap.add_argument("--hook-seconds", type=int, default=20)
    args = ap.parse_args(argv)

    chosen = [f for f in ("audio", "hook", "asr", "llm", "inject") if getattr(args, f)]
    if args.all or not chosen:
        chosen = ["audio", "asr", "llm", "hook", "inject"]

    logging_setup.setup("WARNING", console=True)
    cfg = Config.load()
    print(f"WISPI selftest | modelo={cfg.asr.model} {cfg.asr.device}/{cfg.asr.compute_type}")

    results = {}
    for name in chosen:
        fn = {"audio": test_audio, "hook": lambda c: test_hook(c, args.hook_seconds),
              "asr": test_asr, "llm": test_llm, "inject": test_inject}[name]
        try:
            results[name] = fn(cfg)
        except Exception as e:
            import traceback
            print(f"{BAD} {name} reviento:\n{traceback.format_exc()}")
            results[name] = False

    _hr("RESUMEN")
    for k, v in results.items():
        print(f"  {OK if v else BAD}  {k}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
