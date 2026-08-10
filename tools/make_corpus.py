"""Genera el corpus de audio de prueba con Piper (TTS local).

POR QUE EXISTE
    Verificar WISPI de punta a punta exige voz. Pedirsela a un humano cada vez
    convierte cada regresion en una sesion de laboratorio. Piper sintetiza
    espanol offline y da un corpus REPRODUCIBLE: el mismo audio, byte a byte,
    en cada corrida.

    Y hay una propiedad que lo hace mejor de lo que parece: una voz ESPANOLA
    leyendo "Supabase", "deploy" o "commit" produce justo la pronunciacion
    castellanizada que Whisper-es destroza. O sea, el corpus ataca el modo de
    fallo real que el diccionario tiene que arreglar, no uno inventado.

LIMITACION QUE HAY QUE TENER PRESENTE
    Piper NO sustituye una voz humana con un microfono real. El TTS es limpio,
    sin ruido de sala, sin respiraciones, sin muletillas reales y con prosodia
    de sintetizador. Whisper acierta mas sobre esto que sobre voz humana real.
    Por tanto:
      - Sirve para: regresiones, latencia, cobertura del diccionario, wiring.
      - NO sirve para: dar por bueno el WER real ni cerrar los criterios que el
        SPEC marca como 🔴 HUMANO.

REQUISITOS
    `piper-tts` y una voz espanola .onnx. Ninguno de los dos hace falta para
    USAR WISPI: solo para regenerar este corpus. Por eso no estan en las
    dependencias del proyecto y se instalan aparte, donde prefieras:

        pip install piper-tts
        # y descarga una voz de https://huggingface.co/rhasspy/piper-voices
        # (es_ES-sharvard-medium.onnx + su .json al lado)

COMO SE EJECUTA
    Con cualquier interprete que tenga piper-tts instalado — no hace falta que
    sea el venv de WISPI:

        python tools/make_corpus.py

    La voz se busca, en este orden:
      1. La variable de entorno WISPI_PIPER_VOICE (ruta al .onnx).
      2. models/piper/es_ES-sharvard-medium.onnx dentro del repo.

    Salida: bench/corpus/*.wav        (int16 mono 16 kHz)
            bench/corpus_ref.yaml     (transcripcion de referencia)
"""
from __future__ import annotations

import json
import os
import sys
import wave
from pathlib import Path

import numpy as np

# Relativo al fichero, no absoluto: el repo se clona donde cada uno quiera.
ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "bench" / "corpus"
REF_PATH = ROOT / "bench" / "corpus_ref.yaml"

DEFAULT_VOICE = ROOT / "models" / "piper" / "es_ES-sharvard-medium.onnx"
VOICE = Path(os.environ.get("WISPI_PIPER_VOICE") or DEFAULT_VOICE)
TARGET_SR = 16000

# ---------------------------------------------------------------------------
# El corpus. Cada entrada: (id, texto, etiquetas)
#
# Las etiquetas dicen QUE criterio del SPEC ataca cada clip, para que bench.py
# pueda filtrar. No son decorativas.
#   jerga    -> C7.1 (18 terminos escritos correctamente)
#   muletilla-> C7.2 (nivel 0 las quita)
#   largo    -> C8.1 (>25 palabras: dispara el nivel 1)
#   corto    -> C8.2 (<25 palabras: NO debe disparar el nivel 1)
#   pregunta -> el LLM debe LIMPIARLA, no contestarla
#   latencia -> clips de duracion controlada para medir
# ---------------------------------------------------------------------------
CLIPS: list[tuple[str, str, list[str]]] = [
    # --- Jerga tecnica: el nucleo del criterio C7.1 -------------------------
    ("jerga01",
     "Crea un endpoint en Next.js que consulte Supabase con RLS activado y haz commit.",
     ["jerga", "largo"]),
    ("jerga02",
     "Haz push a la rama y deploy en Vercel cuando pase el build.",
     ["jerga", "corto"]),
    ("jerga03",
     "El workflow de n8n manda el webhook al backend de Prisma en Postgres.",
     ["jerga", "corto"]),
    ("jerga04",
     "Instala shadcn y Tailwind en el frontend, y revisa el prompt de Claude Code.",
     ["jerga", "corto"]),
    ("jerga05",
     "Ollama corre en localhost y Remotion renderiza el video final.",
     ["jerga", "corto"]),
    ("jerga06",
     "Sube el repo nuevo a GitHub y abre un pull request desde la branch.",
     ["jerga", "corto"]),
    ("jerga07",
     "Abre Obsidian y apunta la migracion de SQL que falta en Postgres.",
     ["jerga", "corto"]),
    ("jerga08",
     "Convierte el video con ffmpeg y devuelve el JSON de la API en TypeScript.",
     ["jerga", "corto"]),

    # --- Muletillas: criterio C7.2 -----------------------------------------
    ("muletilla01",
     "Eh, bueno, o sea, lo que quiero es, este, que el deploy salga bien, sabes.",
     ["muletilla", "corto"]),
    ("muletilla02",
     "Pues digamos que el endpoint, o sea, no responde, eh, cuando hay mucha carga.",
     ["muletilla", "corto"]),

    # --- Trampa deliberada: "este" legitimo NO se puede comer --------------
    ("trampa01",
     "Este endpoint devuelve un error quinientos y este otro funciona bien.",
     ["muletilla", "trampa", "corto"]),
    ("trampa02",
     "Bueno es el adjetivo correcto y pues no siempre es muletilla.",
     ["muletilla", "trampa", "corto"]),

    # --- Largos: disparan el nivel 1 (criterio C8.1) ------------------------
    ("largo01",
     "Necesito que revises el flujo completo de registro, desde que el usuario entra "
     "por la pagina hasta que recibe el correo de confirmacion, y me digas en que paso "
     "se esta perdiendo la informacion del formulario porque llevamos tres dias con "
     "reportes raros.",
     ["largo"]),
    ("largo02",
     "Eh, quiero que, o sea, montes un cron que cada manana revise los leads sin responder, "
     "este, y me mande un resumen al Telegram, pues, con los que llevan mas de "
     "veinticuatro horas esperando, sabes, para no dejar a nadie colgado.",
     ["largo", "muletilla"]),

    # --- Cortos: NO deben disparar el nivel 1 (criterio C8.2) --------------
    ("corto01", "Abre el archivo de configuracion.", ["corto"]),
    ("corto02", "Guarda los cambios y cierra.", ["corto"]),

    # --- Pregunta: el LLM debe LIMPIARLA, no contestarla -------------------
    ("pregunta01",
     "Cual es la diferencia entre una migracion de base de datos y un seed, y cuando "
     "conviene usar cada una en un proyecto que ya esta en produccion con clientes reales?",
     ["largo", "pregunta"]),

    # --- Puntuacion y numeros ----------------------------------------------
    ("numeros01",
     "El servidor devolvio un cuatrocientos cuatro en el puerto tres mil, revisa el log.",
     ["corto"]),
]

# Clips sinteticos sin voz, para el criterio C6 (anti-alucinacion).
# NO se generan con Piper: se fabrican con numpy.
SILENCE_CLIPS = [
    ("silencio01", 5.0, 0.0, ["silencio"]),        # silencio digital absoluto
    ("silencio02", 5.0, 0.0008, ["silencio"]),     # ruido de sala muy bajo (~ -62 dBFS)
    ("silencio03", 3.0, 0.004, ["silencio"]),      # ruido audible pero sin voz
]


def resample_linear(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Resampleo por interpolacion lineal. Basta para voz de banda estrecha:
    Piper medium sale a 22050 y Whisper quiere 16000, un ratio suave."""
    if sr_in == sr_out:
        return x
    n_out = int(round(len(x) * sr_out / sr_in))
    t_in = np.arange(len(x), dtype=np.float64)
    t_out = np.linspace(0, len(x) - 1, n_out, dtype=np.float64)
    return np.interp(t_out, t_in, x.astype(np.float64)).astype(np.float32)


def write_wav(path: Path, samples_f32: np.ndarray, sr: int = TARGET_SR) -> float:
    """Escribe WAV int16 mono. Devuelve la duracion en segundos."""
    clipped = np.clip(samples_f32, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return len(pcm) / sr


def main() -> int:
    if not VOICE.exists():
        print(f"ERROR: voz Piper no encontrada en {VOICE}", file=sys.stderr)
        print("Descarga una voz espanola de https://huggingface.co/rhasspy/piper-voices",
              file=sys.stderr)
        print(f"y dejala en {DEFAULT_VOICE}, o apunta WISPI_PIPER_VOICE a su .onnx.",
              file=sys.stderr)
        return 1
    try:
        from piper import PiperVoice
    except Exception as e:
        print(f"ERROR: piper-tts no disponible en este interprete: {e}", file=sys.stderr)
        print("Instalalo con:  pip install piper-tts", file=sys.stderr)
        return 1

    print(f"Cargando voz: {VOICE.name}")
    voice = PiperVoice.load(str(VOICE))
    src_sr = int(getattr(voice.config, "sample_rate", 22050))
    print(f"Sample rate de Piper: {src_sr} Hz -> resampleo a {TARGET_SR} Hz")

    CORPUS.mkdir(parents=True, exist_ok=True)
    ref: list[dict] = []

    # --- clips con voz ----------------------------------------------------
    for clip_id, text, tags in CLIPS:
        chunks = []
        for ch in voice.synthesize(text):
            raw = getattr(ch, "audio_int16_bytes", None)
            if raw is not None:
                chunks.append(np.frombuffer(raw, dtype=np.int16))
            elif hasattr(ch, "audio_int16_array"):
                chunks.append(np.asarray(ch.audio_int16_array, dtype=np.int16))
        if not chunks:
            print(f"  AVISO: {clip_id} no produjo audio, saltado")
            continue
        pcm16 = np.concatenate(chunks)
        f32 = pcm16.astype(np.float32) / 32768.0
        f32 = resample_linear(f32, src_sr, TARGET_SR)
        # Margen de 250 ms al inicio y al final: reproduce lo que hace el ring de
        # pre-roll y la cola de 200 ms en el uso real, y evita que el VAD recorte
        # la primera silaba en las pruebas.
        pad = np.zeros(int(TARGET_SR * 0.25), dtype=np.float32)
        f32 = np.concatenate([pad, f32, pad])
        dur = write_wav(CORPUS / f"{clip_id}.wav", f32)
        peak_rms = float(np.sqrt(np.mean(f32 ** 2)))
        ref.append({
            "id": clip_id, "file": f"{clip_id}.wav", "text": text,
            "tags": tags, "duration_s": round(dur, 3),
            "n_words": len(text.split()), "rms": round(peak_rms, 5),
        })
        print(f"  {clip_id:12s} {dur:5.2f}s  rms={peak_rms:.4f}  {len(text.split()):3d} palabras")

    # --- clips de silencio/ruido -----------------------------------------
    rng = np.random.default_rng(20260809)  # semilla fija: corpus reproducible
    for clip_id, dur_s, noise, tags in SILENCE_CLIPS:
        n = int(TARGET_SR * dur_s)
        f32 = (rng.standard_normal(n).astype(np.float32) * noise) if noise > 0 \
            else np.zeros(n, dtype=np.float32)
        dur = write_wav(CORPUS / f"{clip_id}.wav", f32)
        peak_rms = float(np.sqrt(np.mean(f32 ** 2)))
        ref.append({
            "id": clip_id, "file": f"{clip_id}.wav", "text": "",
            "tags": tags, "duration_s": round(dur, 3), "n_words": 0,
            "rms": round(peak_rms, 6),
        })
        print(f"  {clip_id:12s} {dur:5.2f}s  rms={peak_rms:.6f}  (sin voz)")

    # YAML a mano para no depender de PyYAML: este script puede correr en un
    # interprete distinto al de WISPI (el que tenga piper-tts instalado).
    lines = [
        "# Corpus de referencia de WISPI. Generado por tools/make_corpus.py con Piper.",
        "# NO es voz humana real: sirve para regresiones y latencia, no para dar por",
        "# bueno el WER ni cerrar los criterios que el SPEC marca como HUMANO.",
        "clips:",
    ]
    for r in ref:
        lines.append(f"  - id: {r['id']}")
        lines.append(f"    file: {r['file']}")
        lines.append(f"    text: {json.dumps(r['text'], ensure_ascii=False)}")
        lines.append(f"    tags: [{', '.join(r['tags'])}]")
        lines.append(f"    duration_s: {r['duration_s']}")
        lines.append(f"    n_words: {r['n_words']}")
        lines.append(f"    rms: {r['rms']}")
    REF_PATH.parent.mkdir(parents=True, exist_ok=True)
    REF_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    total = sum(r["duration_s"] for r in ref)
    print(f"\n{len(ref)} clips, {total:.1f}s de audio en total")
    print(f"WAVs  -> {CORPUS}")
    print(f"Ref   -> {REF_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
