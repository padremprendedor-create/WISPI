"""Prueba end-to-end del pipeline SIN voz humana ni dedos humanos.

QUE CUBRE Y QUE NO
    Cubre: corpus WAV -> gate -> ASR -> anti-alucinacion -> nivel 0 + diccionario
    -> decision de nivel 1 -> LLM -> inyeccion REAL (cross-process) en la ventana
    diana -> lectura de vuelta y comparacion.

    NO cubre: el hook de teclado (eso lo prueba tools/hook_selftest.py con input
    sintetico) ni la captura de microfono real. Y NO cierra ningun criterio que
    el SPEC marque como HUMANO: Piper no es una voz humana con un microfono
    real, y un Text de Tk no es Windows Terminal con Claude Code.

    Sirve para lo que un humano no puede hacer barato: correr las 21 pruebas
    otra vez despues de cada cambio.

USO
    uv run python tools/e2e_pipeline.py [--no-llm] [--no-inject] [--clip jerga01]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wispi import logging_setup                                    # noqa: E402
from wispi.asr.registry import build_with_fallback                 # noqa: E402
from wispi.config import Config                                    # noqa: E402
from wispi.postprocess.hallucinations import is_whisper_hallucination  # noqa: E402
from wispi.postprocess.pipeline import PostProcessor               # noqa: E402

CORPUS = ROOT / "bench" / "corpus"
REF = ROOT / "bench" / "corpus_ref.yaml"
OUT = ROOT / "bench" / "out"

# Los 18 terminos del criterio C7.1 del SPEC. La comparacion es EXACTA en la
# forma canonica: "next.js" en minusculas NO cuenta como acierto, porque el
# valor de la feature es justamente que salga bien escrito.
JERGA_18 = [
    "Supabase", "Vercel", "Next.js", "Claude Code", "n8n", "RLS", "commit",
    "push", "deploy", "prompt", "endpoint", "Prisma", "Tailwind", "shadcn",
    "Remotion", "Ollama", "Obsidian", "ffmpeg",
]


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        pcm = np.frombuffer(w.readframes(n), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0, sr


def load_ref() -> list[dict]:
    """Parser minimo del corpus_ref.yaml que genera make_corpus.py.
    Se evita PyYAML a proposito para que este arnes no dependa del formato
    exacto de un YAML generado por nosotros mismos."""
    clips, cur = [], None
    for line in REF.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("- id:"):
            if cur:
                clips.append(cur)
            cur = {"id": s.split(":", 1)[1].strip()}
        elif cur is None or not s or s.startswith("#"):
            continue
        elif s.startswith("file:"):
            cur["file"] = s.split(":", 1)[1].strip()
        elif s.startswith("text:"):
            cur["text"] = json.loads(s.split(":", 1)[1].strip())
        elif s.startswith("tags:"):
            body = s.split(":", 1)[1].strip().strip("[]")
            cur["tags"] = [t.strip() for t in body.split(",") if t.strip()]
        elif s.startswith("duration_s:"):
            cur["duration_s"] = float(s.split(":", 1)[1])
        elif s.startswith("n_words:"):
            cur["n_words"] = int(s.split(":", 1)[1])
        elif s.startswith("rms:"):
            cur["rms"] = float(s.split(":", 1)[1])
    if cur:
        clips.append(cur)
    return clips


class TargetHarness:
    """Arranca la ventana diana y lee lo que recibe."""

    def __init__(self) -> None:
        self.state = OUT / "target_state.json"
        self.proc: subprocess.Popen | None = None
        self.pid: int = 0

    def start(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        self.state.unlink(missing_ok=True)
        py = ROOT / ".venv" / "Scripts" / "python.exe"
        self.proc = subprocess.Popen(
            [str(py), str(ROOT / "tools" / "target_window.py"), "--state", str(self.state)],
            cwd=str(ROOT),
        )
        for _ in range(100):
            if self.state.exists():
                try:
                    self.pid = json.loads(self.state.read_text(encoding="utf-8"))["pid"]
                except (OSError, json.JSONDecodeError, KeyError):
                    time.sleep(0.1)
                    continue
                return
            time.sleep(0.1)
        raise RuntimeError("la ventana diana no arranco en 10 s")

    def ensure_focus(self, intentos: int = 24) -> bool:
        """Devuelve True solo si la ventana diana tiene el foco DE VERDAD.

        POR QUE ESTA GUARDA ES OBLIGATORIA Y NO UN DETALLE DEL TEST
            La inyeccion va a donde este el foco, punto. Windows bloquea
            SetForegroundWindow desde procesos en segundo plano, asi que el
            focus_force() de la diana puede fallar en silencio. Sin comprobar el
            PID, el test le estaria escribiendo frases sueltas a las ventanas
            REALES del usuario mientras corre. Eso paso: 14 de 18 clips fueron a
            parar a otro sitio, y el injector reportaba exito porque, desde su
            punto de vista, lo tuvo.
        """
        from wispi.inject.target import current_target
        for _ in range(intentos):
            self.focus()
            if current_target().pid == self.pid:
                return True
            time.sleep(0.25)
        return False

    def _cmd(self, c: str) -> None:
        Path(str(self.state) + ".cmd").write_text(c, encoding="utf-8")
        time.sleep(0.25)

    def clear(self) -> None:
        self._cmd("clear")

    def focus(self) -> None:
        self._cmd("focus")

    def read(self, settle_s: float = 0.6) -> str:
        """Espera a que el contenido se estabilice antes de leer: la inyeccion
        por Unicode llega caracter a caracter y leer demasiado pronto da un
        falso negativo."""
        last, stable = None, 0
        deadline = time.time() + settle_s + 3.0
        while time.time() < deadline:
            try:
                st = json.loads(self.state.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            cur = st.get("content", "")
            if cur == last:
                stable += 1
                if stable >= 4:
                    return cur
            else:
                stable = 0
                last = cur
            time.sleep(0.08)
        return last or ""

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            self._cmd("quit")
            self.proc.wait(timeout=4)
        except Exception:
            self.proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-inject", action="store_true")
    ap.add_argument("--clip", default=None, help="correr un solo clip por id")
    args = ap.parse_args()

    logging_setup.setup("WARNING", console=True)
    log = logging_setup.get("e2e")
    cfg = Config.load()
    if args.no_llm:
        cfg.llm.enabled = False

    print("=" * 78)
    print("WISPI - prueba end-to-end del pipeline")
    print("=" * 78)

    t0 = time.perf_counter()
    asr, warns = build_with_fallback(cfg.asr, log)
    print(f"ASR: {asr.describe()}  (carga {time.perf_counter()-t0:.2f}s)")
    for w in warns:
        print(f"  AVISO fallback: {w}")
    asr.warmup()

    post = PostProcessor(cfg.postprocess, cfg.llm, logging_setup.get("post"))
    # initial_prompt(), no hotwords(): ver el comentario en app.py::_work_asr
    hot = post.initial_prompt()
    print(f"initial_prompt: {len(hot or '')} chars -> {(hot or '')[:70]!r}...")
    if cfg.llm.enabled:
        print(f"LLM warmup: {'ok' if post.warmup() else 'NO DISPONIBLE'}")

    harness = None
    if not args.no_inject:
        harness = TargetHarness()
        harness.start()
        print(f"ventana diana: pid activo, estado en {harness.state.name}")

    clips = load_ref()
    if args.clip:
        clips = [c for c in clips if c["id"] == args.clip]
    print(f"\n{len(clips)} clips\n" + "-" * 78)

    rows, asr_ms_all, l0_ms_all = [], [], []
    try:
        for c in clips:
            audio, sr = load_wav(CORPUS / c["file"])
            assert sr == cfg.audio.sample_rate, f"{c['id']}: sr={sr}"
            dur = len(audio) / sr
            rms = float(np.sqrt(np.mean(audio ** 2)))

            row = {"id": c["id"], "tags": c["tags"], "ref": c["text"],
                   "dur_s": round(dur, 2), "rms": round(rms, 5)}

            # --- gate previo al ASR (criterio C6.2) ---
            if dur < cfg.audio.min_duration_s or rms < cfg.audio.silence_threshold:
                row.update(gated=True, reason=f"rms {rms:.5f} < {cfg.audio.silence_threshold}",
                           out="", ok="silencio" in c["tags"])
                rows.append(row)
                print(f"{c['id']:12s} GATE   rms={rms:.5f}  -> descartado "
                      f"{'(correcto)' if 'silencio' in c['tags'] else '(!! ERROR)'}")
                continue
            row["gated"] = False

            ta = time.perf_counter()
            res = asr.transcribe(audio, language=cfg.asr.language, hotwords=hot)
            asr_ms = (time.perf_counter() - ta) * 1000
            asr_ms_all.append(asr_ms)
            raw = (res.text or "").strip()
            row.update(raw=raw, asr_ms=round(asr_ms), rtf=round(res.rtf, 3))

            if is_whisper_hallucination(raw):
                row.update(out="", reason="alucinacion", ok="silencio" in c["tags"])
                rows.append(row)
                print(f"{c['id']:12s} ALUC   {raw[:50]!r}")
                continue

            tl = time.perf_counter()
            text = post.level0(raw)
            l0_ms = (time.perf_counter() - tl) * 1000
            l0_ms_all.append(l0_ms)
            row.update(level0=text, level0_ms=round(l0_ms, 3))

            needs = post.needs_level1(text)
            row["needs_llm"] = needs
            final = text
            if needs and cfg.llm.enabled:
                tll = time.perf_counter()
                out = post.level1(text)
                row["llm_ms"] = round((time.perf_counter() - tll) * 1000)
                row["llm_used"] = bool(out)
                if out:
                    final = out
            row["out"] = final

            # --- inyeccion real, cross-process ---
            if harness:
                harness.clear()
                from wispi.inject.injector import Injector
                from wispi.inject.target import current_target
                if not harness.ensure_focus():
                    # NO se inyecta a ciegas: iria a una ventana real del usuario.
                    tgt = current_target()
                    row["focus_failed"] = True
                    row["injected_ok"] = False
                    print(f"{c['id']:12s} SIN FOCO: lo tiene {tgt.exe!r} (pid {tgt.pid}), "
                          f"no la diana (pid {harness.pid}). No se inyecta.")
                else:
                    inj = Injector(cfg.injection, logging_setup.get("inject"))
                    ti = time.perf_counter()
                    r = inj.insert(final, current_target())
                    row["inject_ms"] = round((time.perf_counter() - ti) * 1000)
                    row["route"] = r.route
                    row["inject_ok_reported"] = r.ok
                    got = harness.read()
                    row["injected_ok"] = (got == final)
                    row["got"] = got
                    if got != final:
                        print(f"{c['id']:12s} INJECT MISMATCH (ruta {r.route}, "
                              f"injector dijo ok={r.ok})\n   esperado: {final!r}\n"
                              f"   recibido: {got!r}")

            flag = ""
            if needs != (c["n_words"] >= cfg.llm.min_words):
                flag = " [umbral LLM discrepa del recuento de referencia]"
            print(f"{c['id']:12s} asr={asr_ms:6.0f}ms l0={l0_ms:5.2f}ms "
                  f"llm={'si' if needs else 'no':2s} -> {final[:60]!r}{flag}")
            rows.append(row)
    finally:
        if harness:
            harness.stop()

    # ==================== INFORME ====================
    print("\n" + "=" * 78)
    print("INFORME")
    print("=" * 78)

    # C6: los clips de silencio deben quedar descartados, TODOS
    sil = [r for r in rows if "silencio" in r["tags"]]
    sil_ok = sum(1 for r in sil if r.get("gated") or r.get("reason") == "alucinacion"
                 or not r.get("out"))
    print(f"C6 anti-alucinacion : {sil_ok}/{len(sil)} clips sin voz descartados "
          f"{'OK' if sil_ok == len(sil) else 'FALLA'}")

    # C7.1: cobertura de jerga sobre los clips etiquetados
    jerga_rows = [r for r in rows if "jerga" in r["tags"]]
    todo = " ".join(r.get("out", "") for r in jerga_rows)
    aciertos = [t for t in JERGA_18 if t in todo]
    fallos = [t for t in JERGA_18 if t not in todo]
    print(f"C7.1 jerga          : {len(aciertos)}/18 exactos "
          f"{'OK' if len(aciertos) >= 16 else 'FALLA (umbral 16)'}")
    if fallos:
        print(f"     no encontrados : {', '.join(fallos)}")

    # C7.3: latencia del nivel 0
    if l0_ms_all:
        p99 = max(l0_ms_all) if len(l0_ms_all) < 100 else \
            statistics.quantiles(l0_ms_all, n=100)[98]
        print(f"C7.3 nivel 0 p99    : {p99:.3f} ms {'OK' if p99 < 5 else 'FALLA (umbral 5 ms)'}")

    # C5.1: latencia del ASR
    if asr_ms_all:
        print(f"C5.1 ASR            : p50={statistics.median(asr_ms_all):.0f} ms  "
              f"max={max(asr_ms_all):.0f} ms  "
              f"{'OK' if statistics.median(asr_ms_all) < 2000 else 'FALLA (umbral 2000 ms)'}")

    # C8.2: los cortos NO deben llamar al LLM
    cortos = [r for r in rows if "corto" in r["tags"] and not r.get("gated")]
    mal = [r["id"] for r in cortos if r.get("needs_llm")]
    print(f"C8.2 cortos sin LLM : {len(cortos)-len(mal)}/{len(cortos)} "
          f"{'OK' if not mal else 'FALLA: ' + ', '.join(mal)}")

    # C4: inyeccion. Se separa "no llego el texto" de "no habia foco": son fallos
    # distintos y confundirlos haria culpar al injector de un problema del arnes.
    inj = [r for r in rows if "injected_ok" in r]
    if inj:
        sin_foco = [r for r in inj if r.get("focus_failed")]
        probados = [r for r in inj if not r.get("focus_failed")]
        ok = sum(1 for r in probados if r["injected_ok"])
        print(f"C4 inyeccion        : {ok}/{len(probados)} exactos "
              f"{'OK' if probados and ok == len(probados) else 'FALLA'}")
        if sin_foco:
            print(f"     {len(sin_foco)} clips sin foco (no se inyecto, a proposito): "
                  f"{', '.join(r['id'] for r in sin_foco)}")

    # C7.2: las muletillas SEGURAS tienen que desaparecer siempre. Se comprueba
    # sobre el texto POST-nivel-0, no sobre el crudo.
    #
    # OJO CON LO QUE NO ESTA EN ESTA LISTA: "digamos", "pues" y "este" son
    # muletillas Y palabras legitimas. El nivel 0 solo las borra cuando la
    # puntuacion las delata, asi que exigirlas aqui seria pedirle al sistema el
    # falso positivo que evita a proposito. "Pues digamos que no responde" se
    # queda entero, y esta bien.
    MULETILLAS = ("eh", "o sea", "sabes")
    mrows = [r for r in rows if "muletilla" in r["tags"] and "trampa" not in r["tags"]
             and r.get("level0")]
    m_ok, m_det = 0, []
    for r in mrows:
        low = " " + r["level0"].lower() + " "
        quedan = [m for m in MULETILLAS if f" {m} " in low or f" {m}," in low]
        if not quedan:
            m_ok += 1
        else:
            m_det.append(f"{r['id']}: quedan {quedan}")
    print(f"C7.2 muletillas     : {m_ok}/{len(mrows)} clips limpios "
          f"{'OK' if m_ok == len(mrows) else 'FALLA'}")
    for d in m_det:
        print(f"     {d}")

    # TRAMPAS: palabras legitimas que NO se pueden comer. Un falso positivo aqui es
    # peor que dejar una muletilla, porque el usuario no lo ve venir.
    TRAMPAS = {"trampa01": ["Este", "este otro"], "trampa02": ["Bueno", "pues"]}
    t_ok, t_det = 0, []
    for r in rows:
        if r["id"] not in TRAMPAS or not r.get("out"):
            continue
        faltan = [w for w in TRAMPAS[r["id"]] if w.lower() not in r["out"].lower()]
        if not faltan:
            t_ok += 1
        else:
            t_det.append(f"{r['id']}: se comio {faltan} -> {r['out']!r}")
    print(f"TRAMPAS (falso pos) : {t_ok}/{len(TRAMPAS)} "
          f"{'OK' if t_ok == len(TRAMPAS) else 'FALLA'}")
    for d in t_det:
        print(f"     {d}")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "e2e_results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDetalle -> {OUT / 'e2e_results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
