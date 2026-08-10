"""Banco de medida y la REGLA DE DECISION de la migracion de motor.

Dos modos:

  # 1. Comparar backends sobre el corpus fijo (mismos 21 clips siempre)
  uv run python -m wispi.bench --corpus
  uv run python -m wispi.bench --corpus --models tiny,base,small

  # 2. Analizar el uso REAL acumulado en logs/latency.jsonl
  uv run python -m wispi.bench --analyze

POR QUE ESTO EXISTE
    La eleccion de motor se dejo deliberadamente abierta: se arranco en `small`
    porque la medicion del dia 0 mostro que `large-v3` cuesta 6,3 s de piso en
    esta CPU. Pero esa decision hay que revisitarla con datos de uso real, no
    con una corazonada a las tres semanas. Este modulo es quien la cierra.

LA REGLA (del plan, seccion "Medicion")
    Tras >= 100 dictados reales en >= 3 dias:
        objetivo   p50(ttt_ms) <= 1200 ms   y   p90(ttt_ms) <= 2000 ms
    Si se cumple  -> NO se migra nada. Se escribe la decision en el README.
    Si no         -> primero se identifica el termino dominante:
        asr_ms / ttt_ms > 0.6  -> el problema ES el modelo. Sube la escalera.
        si no                  -> el problema es la cola de audio o la
                                  inyeccion, y cambiar de modelo NO arregla
                                  nada. Mira tail_ms e inject_ms antes de tocar
                                  el ASR.

    Escalera, en orden estricto, re-midiendo sobre EL MISMO corpus:
        1. base int8 CPU        (0,36 s medidos, ya en disco, coste cero)
        2. large-v3 GPU int8_float16  (prerequisitos duros: ver README)
        3. Parakeet TDT 0.6B v3 via onnx-asr en CPU
    large-v3-turbo y distil-large-v3 NO entran: medido, comparten encoder.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import wave
from pathlib import Path

import numpy as np

from . import logging_setup
from .config import ASRCfg, Config
from .metrics import LATENCY_PATH

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "bench" / "corpus"

P50_TARGET_MS = 1200
P90_TARGET_MS = 2000
ASR_DOMINANCE = 0.6


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = min(len(s) - 1, int(round((len(s) - 1) * p)))
    return s[k]


def _load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        pcm = np.frombuffer(w.readframes(n), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0, sr


# ---------------------------------------------------------------------------
# Modo 1: corpus
# ---------------------------------------------------------------------------
def run_corpus(cfg: Config, models: list[str], repeats: int) -> int:
    wavs = sorted(CORPUS.glob("*.wav"))
    if not wavs:
        print(f"No hay corpus en {CORPUS}.")
        print("Generalo con:  python tools/make_corpus.py  (requiere piper-tts;")
        print("               ver el docstring del script para la voz .onnx)")
        return 1

    # Los clips sin voz se excluyen: miden el piso del encoder, no el trabajo
    # real, y meterlos en la mediana la sesga hacia abajo.
    voiced = [w for w in wavs if not w.stem.startswith("silencio")]
    print(f"Corpus: {len(voiced)} clips con voz ({len(wavs)} totales), "
          f"{repeats} repeticion(es)\n")

    from .asr.registry import build
    log = logging_setup.get("bench")
    rows = []

    for model in models:
        acfg = ASRCfg(**{**cfg.asr.__dict__, "model": model})
        acfg.fallback_chain = []
        try:
            t0 = time.perf_counter()
            backend = build(acfg)
            backend.load()
            load_s = time.perf_counter() - t0
        except Exception as e:
            print(f"{model:10s} NO CARGA: {e}")
            continue
        backend.warmup()

        per_clip, total_audio = [], 0.0
        for _ in range(repeats):
            for w in voiced:
                audio, sr = _load_wav(w)
                total_audio += len(audio) / sr
                t0 = time.perf_counter()
                backend.transcribe(audio, language=cfg.asr.language)
                per_clip.append((time.perf_counter() - t0) * 1000)
        backend.unload()

        r = {
            "model": model, "load_s": round(load_s, 2), "n": len(per_clip),
            "p50_ms": round(statistics.median(per_clip)),
            "p90_ms": round(_pct(per_clip, 0.90)),
            "max_ms": round(max(per_clip)),
            "rtf": round((sum(per_clip) / 1000) / total_audio, 3),
        }
        rows.append(r)
        print(f"{model:10s} carga={r['load_s']:5.2f}s  p50={r['p50_ms']:5d}ms  "
              f"p90={r['p90_ms']:5d}ms  max={r['max_ms']:5d}ms  RTF={r['rtf']:.3f}")

    if rows:
        out = ROOT / "bench" / "out" / "bench_corpus.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\n-> {out}")
        print("\nRecuerda: el ASR es solo UN termino de ttt_ms. Un modelo que gane")
        print("aqui puede no mover la aguja si el cuello esta en tail_ms o inject_ms.")
    return 0


# ---------------------------------------------------------------------------
# Modo 2: analisis del uso real -> LA DECISION
# ---------------------------------------------------------------------------
def analyze(path: Path) -> int:
    if not path.exists():
        print(f"No hay {path} todavia. Usa WISPI un tiempo y vuelve.")
        return 1

    recs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    total = len(recs)
    good = [r for r in recs if r.get("ttt_ms") and not r.get("discarded")]
    discarded = [r for r in recs if r.get("discarded")]

    print("=" * 70)
    print(f"ANALISIS DE {total} DICTADOS  ({path})")
    print("=" * 70)
    if not good:
        print("Ningun dictado completo todavia.")
        return 1

    days = len({r.get("ts", "")[:10] for r in good})
    ttt = [r["ttt_ms"] for r in good]
    p50, p90 = statistics.median(ttt), _pct(ttt, 0.90)

    print(f"\nCompletados : {len(good)}  |  descartados: {len(discarded)}  |  dias: {days}")
    if discarded:
        motivos: dict[str, int] = {}
        for r in discarded:
            motivos[r["discarded"].split("(")[0].strip()] = \
                motivos.get(r["discarded"].split("(")[0].strip(), 0) + 1
        print(f"  motivos   : {motivos}")

    print(f"\nTIEMPO HASTA TEXTO (t9-t2, el KPI que siente el usuario)")
    print(f"  p50={p50:.0f} ms   p90={p90:.0f} ms   max={max(ttt):.0f} ms")

    print(f"\nDESGLOSE POR ETAPA (mediana)")
    etapas = ("capture_ms", "gate_ms", "asr_ms", "level0_ms", "inject_ms",
              "llm_ms", "patch_ms")
    medianas = {}
    for k in etapas:
        vals = [r[k] for r in good if r.get(k) is not None]
        if vals:
            medianas[k] = statistics.median(vals)
            share = medianas[k] / p50 * 100 if p50 else 0
            bar = "#" * int(share / 3)
            print(f"  {k:11s} {medianas[k]:7.1f} ms  {share:5.1f}%  {bar}")

    rutas: dict[str, int] = {}
    for r in good:
        rutas[r.get("route", "?")] = rutas.get(r.get("route", "?"), 0) + 1
    print(f"\nRutas de inyeccion: {rutas}")
    apps: dict[str, int] = {}
    for r in good:
        apps[r.get("target_exe", "?")] = apps.get(r.get("target_exe", "?"), 0) + 1
    print(f"Apps destino      : {dict(sorted(apps.items(), key=lambda x: -x[1])[:8])}")

    p99_cb = [r["hook_cb_p99_us"] for r in recs if r.get("hook_cb_p99_us")]
    if p99_cb:
        peor = max(p99_cb)
        estado = "OK" if peor < 5000 else "RIESGO: Windows puede desenganchar el hook"
        print(f"Callback del hook : p99 peor observado {peor} us  -> {estado}")

    # ---------------- LA DECISION ----------------
    print("\n" + "=" * 70)
    print("DECISION DE MIGRACION")
    print("=" * 70)
    if len(good) < 100 or days < 3:
        print(f"AUN NO SE DECIDE: hacen falta >=100 dictados en >=3 dias.")
        print(f"Llevas {len(good)} en {days} dia(s). Sigue usando WISPI con normalidad.")
        return 0

    cumple = p50 <= P50_TARGET_MS and p90 <= P90_TARGET_MS
    print(f"Objetivo: p50<={P50_TARGET_MS} ms y p90<={P90_TARGET_MS} ms")
    print(f"Real    : p50={p50:.0f} ms, p90={p90:.0f} ms  ->  "
          f"{'SE CUMPLE' if cumple else 'NO SE CUMPLE'}")

    if cumple:
        print("\n=> NO SE MIGRA NADA. El motor actual es suficiente.")
        print("   Escribe la decision y estos numeros en el README y cierra el tema.")
        return 0

    asr_share = medianas.get("asr_ms", 0) / p50 if p50 else 0
    print(f"\nTermino dominante: asr_ms/ttt_ms = {asr_share:.2f}")
    if asr_share > ASR_DOMINANCE:
        print("=> EL PROBLEMA ES EL MODELO. Sube la escalera, en orden:")
        print("   1. base int8 CPU (coste cero, ya en disco)")
        print("   2. large-v3 GPU int8_float16 (lee los prerequisitos DUROS del README:")
        print("      SubprocessASRBackend, venv .venv-gpu, cuDNN 9 no 8,")
        print("      os.add_dll_directory, y el gate de 20 transcripciones sin crash)")
        print("   3. Parakeet TDT 0.6B v3 via onnx-asr")
    else:
        print("=> EL PROBLEMA NO ES EL MODELO. Cambiarlo no arreglaria nada.")
        print(f"   capture_ms={medianas.get('capture_ms', 0):.0f} (baja audio.tail_ms)")
        print(f"   inject_ms ={medianas.get('inject_ms', 0):.0f} "
              f"(prueba injection.route: unicode, o baja restore_delay_ms)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wispi.bench")
    ap.add_argument("--corpus", action="store_true", help="comparar backends sobre el corpus")
    ap.add_argument("--analyze", action="store_true", help="analizar logs/latency.jsonl")
    ap.add_argument("--models", default=None, help="lista separada por comas")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--jsonl", default=None)
    args = ap.parse_args(argv)

    logging_setup.setup("WARNING", console=True)
    cfg = Config.load()
    if not args.corpus and not args.analyze:
        args.analyze = True

    rc = 0
    if args.corpus:
        models = [m.strip() for m in args.models.split(",")] if args.models else [cfg.asr.model]
        rc |= run_corpus(cfg, models, args.repeats)
    if args.analyze:
        rc |= analyze(Path(args.jsonl) if args.jsonl else LATENCY_PATH)
    return rc


if __name__ == "__main__":
    sys.exit(main())
