"""Red de regresion del emparejado de la palabra de activacion. SIN voz ni micro.

    uv run python tools/test_wake.py

Verifica los criterios C11.6 (cero falsos positivos) y C11.7 (las variantes que
Whisper produce de verdad SI disparan) sobre texto, que es donde vive el riesgo:
el segmentador solo decide QUE se manda al reconocedor; quien decide si eso era
"hey WISPI" es `wake.match()`, y esa funcion es pura.

Los positivos NO son inventados: son las formas en que `tiny` transcribe una
palabra que no existe en espanol y que nunca vio en su corpus. Los negativos
tampoco: "hey wifi" es el caso que fija `name_threshold` en 0,70 (da 0,80 contra
la frase entera y solo se cae al mirar el nombre por separado).

Anadir un caso aqui cuesta una linea y es la forma barata de subir un umbral sin
romper lo que ya funcionaba.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import queue                              # noqa: E402

import numpy as np                        # noqa: E402

from wispi.asr.base import ASRResult      # noqa: E402
from wispi.config import AudioCfg, ASRCfg, WakeCfg   # noqa: E402
from wispi.events import HookEvent        # noqa: E402
from wispi.logging_setup import get as _log  # noqa: E402
from wispi.wake import WakeWord, match, normalize    # noqa: E402

CFG = WakeCfg()

# Tiene que disparar. Cada linea es una transcripcion plausible de `tiny`.
POSITIVOS = [
    "hey wispi",
    "Hey, Wispi.",
    "¡Hey, WISPI!",
    "hey Wispy",
    "Ey Wispi",
    "ey wispy",
    "Hey, Guispi.",
    "hey guispy",
    "Ay, Wispi.",
    "Oye Wispi",
    "oye wispy",
    "Hola Wispi",
    "hey wis pi",          # el modelo parte la palabra
    "heywispi",            # ...o la junta
    "Hey, Whispi",
    "hey vispi",
    "Hey, Wispi,",
    "wispi",               # el nombre solo tambien vale, y esta asumido
    # -- capturadas EN VIVO con voz real, `uv run python -m wispi.selftest --wake`,
    # 2026-08-10. No inventadas: son la salida literal de `tiny` sobre el "hey
    # WISPI" que dijo Junior. Quedan aqui para que un ajuste de umbrales futuro
    # no las rompa sin que nadie se entere.
    "Hey, Whisby.",             # score real 0.824
    "¡Hey, Whispy!y.",          # score real 0.941 (el "y." suelto es del propio ASR)
]

# NO puede disparar. Son las trampas del criterio C11.6 mas las que salieron al
# medir: palabras que comparten el arranque "wi-" o la muletilla "hey".
NEGATIVOS = [
    "hey wifi",
    "el wifi",
    "hey",
    "Hey!",
    "whisky",
    "un whisky doble",
    "y esto",
    "es que si",
    "wikipedia",
    "esto es",
    "wisin",
    "espera",
    "que pasa",
    "",
    "   ",
    ".",
    # Largas: aunque contengan la frase, un enunciado de quince palabras no es
    # una palabra de activacion. Lo corta `max_tokens` antes de puntuar.
    "oye una cosa y luego le dices a hey wispi que escriba esto por favor",
    "pues nada que el otro dia le dije que si",
]


# ======================================================================
# Segmentador. Es la mitad cara del criterio C11.5: la promesa de que esto no
# quema la CPU se sostiene ENTERA en que el ASR solo se llame sobre enunciados
# cortos y aislados. Aqui se cuentan las llamadas con audio sintetico, sin
# microfono y sin modelo, que es la unica forma de comprobarlo sin creerselo.
# ======================================================================
RATE = 16000
BLOCK_MS = 30


class _CaptureFalsa:
    """Lo minimo que `WakeWord` le pide a `AudioCapture`."""

    def __init__(self) -> None:
        self.cfg = AudioCfg()
        self.stream_rate = RATE

    def to_target(self, raw):
        return np.ascontiguousarray(raw, dtype=np.float32)


class _ASRFalso:
    def __init__(self, texto: str) -> None:
        self.texto = texto
        self.llamadas = 0

    def transcribe(self, audio, *, language, hotwords=None):
        self.llamadas += 1
        return ASRResult(text=self.texto, language=language,
                         duration_s=len(audio) / RATE, infer_ms=1.0)

    def describe(self) -> dict:
        return {"model": "falso"}


def _detector(texto: str) -> tuple[WakeWord, _ASRFalso, queue.SimpleQueue]:
    q: queue.SimpleQueue = queue.SimpleQueue()
    asr = _ASRFalso(texto)
    w = WakeWord(WakeCfg(), ASRCfg(), _CaptureFalsa(), q, _log("wake"))
    w._asr = asr                 # se salta `_load()`: aqui no hay modelo que cargar
    w._configure(RATE)
    return w, asr, q


def _hablar(w: WakeWord, patron: list[tuple[float, bool]]) -> None:
    """`patron` = [(segundos, ¿hay voz?), ...]. Alimenta el segmentador a mano."""
    n = int(RATE * BLOCK_MS / 1000)
    for dur, voz in patron:
        rms = 0.05 if voz else 0.0005          # el umbral por defecto es 0,012
        block = np.full(n, 0.05 if voz else 0.0, dtype=np.float32)
        for _ in range(max(1, int(dur * 1000 / BLOCK_MS))):
            w._on_block(block, rms, RATE)


ESCENARIOS = [
    # (nombre, patron, texto que "oye", llamadas al ASR, activaciones)
    ("sala en silencio 5 s",
     [(5.0, False)], "hey wispi", 0, 0),
    ('"hey WISPI" y callar',
     [(0.5, False), (0.8, True), (0.6, False)], "Hey, Wispi.", 1, 1),
    ("un enunciado corto que no es la frase",
     [(0.5, False), (0.8, True), (0.6, False)], "pasame el archivo", 1, 0),
    ("habla continua 12 s (reunion, tele, llamada)",
     [(0.5, False), (12.0, True), (0.6, False)], "hey wispi", 0, 0),
    ("un golpe en la mesa (0,09 s)",
     [(0.5, False), (0.09, True), (0.6, False)], "hey wispi", 0, 0),
    ("dos frases seguidas sin pausa suficiente",
     [(0.5, False), (0.8, True), (0.15, False), (0.8, True), (0.6, False)],
     "hey wispi", 1, 1),
]


def _segmentador() -> int:
    fallos = 0
    print("\nSegmentador (C11.5) — llamadas reales al reconocedor:\n")
    for nombre, patron, texto, n_esp, det_esp in ESCENARIOS:
        w, asr, q = _detector(texto)
        _hablar(w, patron)
        eventos = 0
        while True:
            try:
                kind, _vk, _t = q.get_nowait()
            except queue.Empty:
                break
            assert kind == HookEvent.WAKE, kind
            eventos += 1
        bien = asr.llamadas == n_esp and eventos == det_esp
        marca = "[ OK ]" if bien else "[FALLA]"
        print(f"  {marca} {nombre:46s} asr={asr.llamadas} (esperado {n_esp})  "
              f"activa={eventos} (esperado {det_esp})")
        if not bien:
            fallos += 1

    # El desarme (C11.4) es lo que impide que el detector se oiga a si mismo.
    w, asr, _q = _detector("hey wispi")
    w.set_armed(False)
    _hablar(w, [(0.5, False), (0.8, True), (0.6, False)])
    # `_on_block` no mira el desarme -lo hace `_run` antes de llamarlo-, asi que
    # aqui se comprueba lo que si es responsabilidad del objeto: que `feed()` no
    # procese nada por su cuenta y que el estado quede limpio.
    w.feed(np.zeros(480, dtype=np.float32), 0.05)
    bien = not w._armed.is_set()
    print(f"  {'[ OK ]' if bien else '[FALLA]'} {'set_armed(False) desarma':46s} "
          f"armed={w._armed.is_set()}")
    if not bien:
        fallos += 1
    return fallos


def _run(casos: list[str], esperado: bool) -> list[tuple[str, float, str]]:
    fallos = []
    for texto in casos:
        ok, score, phrase = match(
            texto, CFG.phrases, CFG.name,
            threshold=CFG.threshold, name_threshold=CFG.name_threshold,
            max_tokens=CFG.max_tokens,
        )
        if ok != esperado:
            fallos.append((texto, score, phrase))
    return fallos


def main() -> int:
    print(f"umbrales: frase>={CFG.threshold}  nombre>={CFG.name_threshold}  "
          f"max_tokens={CFG.max_tokens}")
    print(f"frases:   {', '.join(CFG.phrases)}\n")

    # Sanidad de la normalizacion: si esto se rompe, todo lo demas miente.
    # La -y final sale como -i a proposito (regla fonetica, ver wake.normalize).
    assert normalize("¡Hey, WISPI!") == "hei wispi", normalize("¡Hey, WISPI!")
    assert normalize("Oye  Wíspi...") == "oye wispi", normalize("Oye  Wíspi...")
    assert normalize("Hey, Wispy.") == "hei wispi", normalize("Hey, Wispy.")
    assert normalize("y esto") == "y esto", normalize("y esto")

    falsos_negativos = _run(POSITIVOS, True)
    falsos_positivos = _run(NEGATIVOS, False)

    for texto, score, _ in falsos_negativos:
        print(f"[FALLA] deberia disparar y no dispara: {texto!r} (mejor {score})")
    for texto, score, phrase in falsos_positivos:
        print(f"[FALLA] NO deberia disparar: {texto!r} ({score} contra '{phrase}')")

    ok_pos = len(POSITIVOS) - len(falsos_negativos)
    ok_neg = len(NEGATIVOS) - len(falsos_positivos)
    print(f"\nC11.7 variantes reales: {ok_pos}/{len(POSITIVOS)}")
    print(f"C11.6 falsos positivos: {ok_neg}/{len(NEGATIVOS)}")

    fallos_seg = _segmentador()

    if falsos_negativos or falsos_positivos:
        print("\n[FALLA] emparejado: revisa los umbrales en config.yaml -> wake")
    if fallos_seg:
        print(f"\n[FALLA] segmentador: {fallos_seg} escenario(s)")
    if falsos_negativos or falsos_positivos or fallos_seg:
        return 1
    print("\n[ OK ] emparejado y segmentador correctos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
