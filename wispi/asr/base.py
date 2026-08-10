"""Contrato del backend ASR. Esto es lo que hace intercambiable el motor.

La escalera de migracion completa (small CPU -> base CPU -> large-v3 GPU ->
Parakeet ONNX) se apoya SOLO en este Protocol. Si un backend nuevo lo cumple,
entra sin tocar `app.py`, `audio.py` ni el post-proceso.

Reglas del contrato, en orden de importancia:

1. `audio` es SIEMPRE float32 mono 16 kHz en [-1, 1]. Toda normalizacion y
   resampleo vive en `audio.py`. NINGUN backend resamplea nada. Esto es
   justo lo que permite que Parakeet-ONNX entre sin tocar el pipeline.
2. `hotwords` lo genera `postprocess/dictionary.py`. Un backend que soporte
   sesgo lexico lo usa (faster-whisper -> `initial_prompt`); uno que no, lo
   ignora SIN fallar.
3. `transcribe` es bloqueante y seguro para UN llamador. El
   `ThreadPoolExecutor(max_workers=1)` de `app.py` garantiza esa condicion.
4. `load()` es idempotente. Si falla lanza `ASRLoadError` y el registry avanza
   al siguiente eslabon de `asr.fallback_chain`. NUNCA se degrada en silencio:
   siempre log WARNING + icono en `error`.
5. Ningun backend lee `config.yaml`. Los construye `registry.build()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np


class ASRError(RuntimeError):
    """Base de los fallos de ASR."""


class ASRLoadError(ASRError):
    """No se pudo cargar el modelo. El registry debe probar el siguiente eslabon."""


class ASRTranscribeError(ASRError):
    """Fallo durante la inferencia. El dictado se pierde, la app sigue viva."""


@dataclass(frozen=True)
class ASRResult:
    """Lo que devuelve cualquier backend. Campos opcionales van a None si el
    backend no los expone (Parakeet no da `avg_logprob`, por ejemplo)."""

    text: str
    language: str
    duration_s: float          # duracion del audio de entrada
    infer_ms: float            # inferencia pura, medida por el backend
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    backend: str = ""
    model_id: str = ""
    device: str = ""
    compute_type: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def rtf(self) -> float:
        """Real-time factor: <1 es mas rapido que tiempo real."""
        return (self.infer_ms / 1000.0) / self.duration_s if self.duration_s > 0 else 0.0


@runtime_checkable
class ASRBackend(Protocol):
    name: str

    def load(self) -> None: ...
    def warmup(self) -> None: ...
    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str,
        hotwords: str | None = None,
    ) -> ASRResult: ...
    def unload(self) -> None: ...
    @property
    def is_ready(self) -> bool: ...
    def describe(self) -> dict: ...


def validate_audio(audio: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    """Guarda comun del contrato de entrada. La llaman todos los backends.

    Convierte a float32 contiguo y verifica el rango. Si el audio llega en
    int16 (error de un llamador nuevo) lo normaliza en vez de producir basura
    silenciosa, pero avisa por excepcion si el rango es imposible.
    """
    if audio is None or audio.size == 0:
        raise ASRTranscribeError("audio vacio")
    if audio.ndim > 1:
        audio = audio.reshape(-1)
    if audio.dtype == np.int16:
        audio = audio.astype(np.float32) / 32768.0
    elif audio.dtype != np.float32:
        audio = audio.astype(np.float32)
    peak = float(np.max(np.abs(audio)))
    if peak > 1.5:
        raise ASRTranscribeError(
            f"audio fuera de rango (peak={peak:.2f}); se esperaba float32 en [-1, 1]"
        )
    return np.ascontiguousarray(audio)
