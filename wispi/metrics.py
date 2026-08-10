"""Instrumentacion de latencia. Es el insumo de la decision de migracion de motor.

KPI primario:   ttt_ms  = t9_inject_out - t2_keyup   "tiempo hasta texto"
KPI secundario: ttct_ms = t12_patch_out - t2_keyup   "tiempo hasta texto limpio"

`t2_keyup` es el origen de todo lo que le importa al usuario: lo que pasa antes
de soltar la tecla no lo percibe como espera.

PRIVACIDAD: con `logging.include_text: false` (default) NUNCA se escribe el
transcrito. Solo longitudes, conteos y tiempos.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LATENCY_PATH = LOG_DIR / "latency.jsonl"

# Orden canonico de las marcas. Renombrarlas rompe el analisis historico.
MARKS = (
    "t0_keydown", "t1_capture_on", "t2_keyup", "t3_audio_final", "t4_gate",
    "t5_asr_in", "t6_asr_out", "t7_level0", "t8_inject_in", "t9_inject_out",
    "t10_llm_in", "t11_llm_out", "t12_patch_out",
)


def now_ns() -> int:
    return time.perf_counter_ns()


@dataclass
class DictationTrace:
    """Traza de UN dictado. Se crea en el key-down y se sella al final."""

    seq: int
    marks: dict[str, int] = field(default_factory=dict)
    fields: dict = field(default_factory=dict)

    def mark(self, name: str, t_ns: int | None = None) -> None:
        if name not in MARKS:
            raise KeyError(f"marca desconocida: {name}")
        self.marks[name] = t_ns if t_ns is not None else now_ns()

    def set(self, **kw) -> None:
        self.fields.update(kw)

    def _delta_ms(self, a: str, b: str) -> float | None:
        if a in self.marks and b in self.marks:
            return round((self.marks[b] - self.marks[a]) / 1e6, 2)
        return None

    def to_record(self) -> dict:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "seq": self.seq,
            # Etapas
            "capture_ms": self._delta_ms("t2_keyup", "t3_audio_final"),
            "gate_ms": self._delta_ms("t3_audio_final", "t4_gate"),
            "asr_ms": self._delta_ms("t5_asr_in", "t6_asr_out"),
            "level0_ms": self._delta_ms("t6_asr_out", "t7_level0"),
            "inject_ms": self._delta_ms("t8_inject_in", "t9_inject_out"),
            "llm_ms": self._delta_ms("t10_llm_in", "t11_llm_out"),
            "patch_ms": self._delta_ms("t11_llm_out", "t12_patch_out"),
            # KPIs
            "ttt_ms": self._delta_ms("t2_keyup", "t9_inject_out"),
            "ttct_ms": self._delta_ms("t2_keyup", "t12_patch_out"),
            "hold_ms": self._delta_ms("t0_keydown", "t2_keyup"),
        }
        rec.update(self.fields)
        return rec


class Metrics:
    """Escritor de latency.jsonl + histograma del callback del hook.

    El histograma es la senal de alarma numero 1: si el p99 del callback sube,
    Windows esta a punto de desengancharnos el hook.
    """

    def __init__(self, enabled: bool = True, path: Path = LATENCY_PATH) -> None:
        self.enabled = enabled
        self.path = path
        self._lock = threading.Lock()
        self._seq = 0
        self._cb_us: deque[int] = deque(maxlen=5000)
        self._cb_slow_count = 0
        self._cb_max_us = 0

    def next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    # -- histograma del callback del hook ---------------------------------
    def record_callback(self, duration_us: int) -> None:
        """Llamado DESDE el hilo del hook, fuera del camino critico del callback.
        `deque.append` con maxlen es O(1) y no realoca."""
        self._cb_us.append(duration_us)
        if duration_us > self._cb_max_us:
            self._cb_max_us = duration_us

    def note_slow_callback(self) -> None:
        self._cb_slow_count += 1

    def callback_stats(self) -> dict:
        if not self._cb_us:
            return {"n": 0}
        s = sorted(self._cb_us)
        n = len(s)
        return {
            "n": n,
            "p50_us": s[n // 2],
            "p90_us": s[min(n - 1, int(n * 0.90))],
            "p99_us": s[min(n - 1, int(n * 0.99))],
            "max_us": self._cb_max_us,
            "slow_events": self._cb_slow_count,
        }

    # -- escritura ---------------------------------------------------------
    def write(self, trace: DictationTrace) -> dict:
        rec = trace.to_record()
        rec["hook_cb_p99_us"] = self.callback_stats().get("p99_us")
        if not self.enabled:
            return rec
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(rec, ensure_ascii=False)
            with self._lock, self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass  # la telemetria nunca debe tumbar un dictado
        return rec
