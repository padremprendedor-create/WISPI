"""Backend faster-whisper (CTranslate2). Es el motor por defecto de WISPI.

POR QUE ESTE MODULO EXISTE TAL Y COMO ESTA:

1. El costo del encoder de Whisper es FIJO, no proporcional al audio. Whisper
   rellena SIEMPRE a una ventana de 30 s y corre una pasada de encoder pase lo
   que pase. Medido en esta maquina (i9-10850K, int8, cpu_threads=10,
   beam_size=1, without_timestamps=True):
       large-v3 -> 6.26 s por dictado | small -> 1.24 s | base -> 0.36 s | tiny -> 0.21 s
   Un clip de 5 s y uno de 10 s dan el MISMO numero. Por eso large-v3-turbo y
   distil-large-v3 no ayudan (recortan el decoder, no el encoder) y `chunk_length`
   es palanca muerta. El default es `small` y no se toca sin volver a medir.

2. `hotwords` se mapea a `initial_prompt`. Es PREVENCION: sesga el decoder para
   que produzca el token correcto de entrada, en vez de parchear el error
   despues con regex. Con `small` vale mucho mas que el reemplazo posterior.

3. El backend NO toma decisiones de producto. Si el audio es silencio,
   devuelve el ASRResult con `no_speech_prob` puesto y que decida `app.py`.
"""
from __future__ import annotations

import gc
import os
import threading

import numpy as np

from ..logging_setup import get
from .base import ASRLoadError, ASRResult, ASRTranscribeError, validate_audio

log = get("asr.faster_whisper")

SAMPLE_RATE = 16000

# Ventana de prompt de Whisper. El decoder acepta max_length//2 - 1 tokens de
# contexto previo (448//2-1 = 223); usamos 224 como tope nominal y truncamos
# antes de que CTranslate2 lo recorte por su cuenta y en silencio.
PROMPT_MAX_TOKENS = 224

# Umbral propio de faster-whisper para declarar un decode ROTO. Con
# `temperature` escalar (nuestro caso: 0.0) su fallback de temperatura queda
# inerte y el umbral NUNCA se aplica; lo aplicamos nosotros. Ver
# `_repair_prompt_loop` para el incidente que lo motiva.
COMPRESSION_RATIO_MAX = 2.4


def _max_compression_ratio(segments) -> float | None:
    """El peor segmento manda: un solo segmento en bucle ya arruina el dictado."""
    ratios = [s.compression_ratio for s in segments if s.compression_ratio is not None]
    return float(max(ratios)) if ratios else None


class FasterWhisperBackend:
    """Implementacion del Protocol `ASRBackend` sobre faster-whisper 1.2.x."""

    name = "faster-whisper"

    def __init__(
        self,
        model: str,
        device: str,
        compute_type: str,
        *,
        cpu_threads: int = 10,
        beam_size: int = 1,
        without_timestamps: bool = True,
        condition_on_previous_text: bool = False,
        temperature: float = 0.0,
        vad_filter: bool = True,
        no_speech_threshold: float = 0.6,
        local_files_only: bool = True,
        download_root: str | None = None,
    ) -> None:
        self.model_id = model
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.beam_size = beam_size
        self.without_timestamps = without_timestamps
        self.condition_on_previous_text = condition_on_previous_text
        self.temperature = temperature
        self.vad_filter = vad_filter
        self.no_speech_threshold = no_speech_threshold
        self.local_files_only = local_files_only
        self.download_root = download_root

        self._model = None
        # `load()` puede llamarse desde el hilo de arranque y desde el watchdog;
        # el lock hace la idempotencia real, no solo aparente.
        self._lock = threading.RLock()

    # -- ciclo de vida -----------------------------------------------------
    def load(self) -> None:
        """Idempotente. Lanza ASRLoadError para que el registry pase al siguiente eslabon."""
        with self._lock:
            if self._model is not None:
                return

            try:
                from faster_whisper import WhisperModel
            except Exception as e:  # pragma: no cover - dependencia dura del entorno
                raise ASRLoadError(f"no se pudo importar faster_whisper: {e}") from e

            try:
                model = WhisperModel(
                    self.model_id,
                    device=self.device,
                    compute_type=self.compute_type,
                    cpu_threads=self.cpu_threads,
                    download_root=self.download_root,
                    local_files_only=self.local_files_only,
                )
            except Exception as e:
                raise ASRLoadError(
                    f"no se pudo cargar el modelo '{self.model_id}' "
                    f"(device={self.device}, compute_type={self.compute_type}); "
                    f"buscado en {self._search_hint()}; "
                    f"local_files_only={self.local_files_only}. Causa: {type(e).__name__}: {e}"
                ) from e

            self._model = model

            # INCIDENTE REAL: ctranslate2.get_cuda_device_count() puede devolver 1
            # AUNQUE falte cublas64_12.dll. El modelo CARGA en GPU sin quejarse y
            # REVIENTA en la primera transcripcion. Ese falso positivo dejo un
            # servicio caido una semana. Cargar el modelo NO es prueba de nada:
            # la unica prueba valida es transcribir. Si el probe revienta, esto NO
            # es un backend cargado y el registry debe pasar al siguiente eslabon.
            if str(self.device).lower().startswith("cuda"):
                try:
                    self._raw_transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32),
                                         language=None, initial_prompt=None)
                except Exception as e:
                    self.unload()
                    raise ASRLoadError(
                        f"'{self.model_id}' cargo en CUDA pero revienta al transcribir "
                        f"(falso positivo de get_cuda_device_count; suele faltar "
                        f"cublas64_12.dll o cudnn en el PATH). Causa: {type(e).__name__}: {e}"
                    ) from e

            log.info(
                "modelo cargado: %s device=%s compute_type=%s cpu_threads=%d beam=%d",
                self.model_id, self.device, self.compute_type, self.cpu_threads, self.beam_size,
            )

    def warmup(self) -> None:
        """Paga los allocs perezosos de CTranslate2 antes del primer dictado real.

        No propaga: un warmup fallido es una optimizacion perdida, no un motor
        roto (y en CUDA el fallo real ya lo caza el probe de `load()`). Pero se
        registra con WARNING: degradar en silencio esta prohibido.
        """
        with self._lock:
            if self._model is None:
                raise ASRLoadError("warmup() sin load() previo")
            try:
                self._raw_transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32),
                                     language=None, initial_prompt=None)
            except Exception as e:
                log.warning("warmup fallido en '%s': %s: %s", self.model_id, type(e).__name__, e)

    def unload(self) -> None:
        with self._lock:
            self._model = None
            gc.collect()

    @property
    def is_ready(self) -> bool:
        return self._model is not None

    def describe(self) -> dict:
        return {
            "backend": self.name,
            "model": self.model_id,
            "device": self.device,
            "compute_type": self.compute_type,
            "cpu_threads": self.cpu_threads,
            "beam_size": self.beam_size,
            "vad_filter": self.vad_filter,
            "local_files_only": self.local_files_only,
            "download_root": self.download_root,
            "ready": self.is_ready,
        }

    # -- inferencia --------------------------------------------------------
    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str,
        hotwords: str | None = None,
    ) -> ASRResult:
        if self._model is None:
            raise ASRTranscribeError("transcribe() sin load() previo")

        audio = validate_audio(audio, SAMPLE_RATE)
        duration_s = len(audio) / float(SAMPLE_RATE)
        lang = language if language and language.lower() != "auto" else None
        initial_prompt = self._clip_prompt(hotwords)

        try:
            segments, info, infer_ms = self._raw_transcribe(
                audio, language=lang, initial_prompt=initial_prompt, timed=True
            )
            segments, info, infer_ms, retried = self._repair_prompt_loop(
                audio, lang, initial_prompt, segments, info, infer_ms
            )
        except Exception as e:
            raise ASRTranscribeError(
                f"fallo de inferencia en '{self.model_id}': {type(e).__name__}: {e}"
            ) from e

        text = "".join(s.text for s in segments).strip()

        # avg_logprob se promedia (es una media por segmento, no acumulativa).
        # no_speech_prob se toma del PRIMER segmento: es la probabilidad de que
        # la ventana inicial sea silencio, que es justo lo que mira el filtro
        # anti-alucinacion. Promediarla la diluiria con segmentos posteriores.
        avg_logprob = (
            float(sum(s.avg_logprob for s in segments) / len(segments)) if segments else None
        )
        no_speech_prob = float(segments[0].no_speech_prob) if segments else None

        return ASRResult(
            text=text,
            language=getattr(info, "language", None) or (lang or ""),
            duration_s=duration_s,
            infer_ms=infer_ms,
            avg_logprob=avg_logprob,
            no_speech_prob=no_speech_prob,
            backend=self.name,
            model_id=self.model_id,
            device=self.device,
            compute_type=self.compute_type,
            extra={
                "n_segments": len(segments),
                "language_probability": getattr(info, "language_probability", None),
                "duration_after_vad": getattr(info, "duration_after_vad", None),
                "prompt_used": bool(initial_prompt),
                "prompt_retry": retried,
                # El UNICO detector fiable de bucle de repeticion: en un bucle
                # `avg_logprob` sale sanisimo (-0.06 medido) y solo esto se
                # dispara (37.29 medido, contra ~0.9 normal). El modulo
                # anti-alucinacion lo necesita; ASRResult no tiene campo propio,
                # asi que viaja en `extra`.
                "compression_ratio": _max_compression_ratio(segments),
                # Umbral informativo: quien decide descartar es app.py, no el backend.
                "no_speech_threshold": self.no_speech_threshold,
            },
        )

    # -- interno -----------------------------------------------------------
    def _repair_prompt_loop(self, audio, lang, initial_prompt, segments, info, infer_ms):
        """Reintenta SIN prompt si el prompt metio al decoder en un bucle.

        MEDIDO (jerga05.wav, small int8, initial_prompt = lista de terminos
        separados por comas): el decoder entra en bucle y devuelve 895 chars de
        "Toyota, Toyota, Toyota, ..." de forma determinista. Eso son 895
        caracteres de basura inyectados en la ventana del usuario.

        Por que hace falta este guardarrail y no basta con lo que ya trae
        faster-whisper:
          - `condition_on_previous_text=False` NO lo evita: el bucle lo induce
            el propio `initial_prompt`, que impone el formato "termino, termino,".
          - `avg_logprob` NO lo detecta: sale -0.062, mejor que una frase buena.
          - `compression_ratio_threshold=2.4` SI lo detecta (37.29), pero
            faster-whisper solo lo aplica dentro de su fallback de temperatura,
            y ese fallback esta inerte con `temperature` escalar (0.0).
          - Pasar la escalera de temperaturas lo arregla pero cuesta 26,8 s
            MEDIDOS en un dictado de 5 s. Inaceptable con un presupuesto de 2 s.
          - `no_repeat_ngram_size=6` y `repetition_penalty=1.1`: probados, NO lo
            arreglan (siguen saliendo "Toyota, Toyota...").

        Reintentar sin prompt cuesta ~1,5 s MEDIDOS y devuelve texto correcto.
        Solo se paga en el caso roto. Esto no es una decision de producto: es
        reparar el mecanismo que introduce este propio backend al mapear
        `hotwords` a `initial_prompt`.
        """
        if not initial_prompt or not segments:
            return segments, info, infer_ms, False
        cr = _max_compression_ratio(segments)
        if cr is None or cr <= COMPRESSION_RATIO_MAX:
            return segments, info, infer_ms, False

        log.warning(
            "bucle de repeticion con initial_prompt (compression_ratio=%.2f > %.1f); "
            "se reintenta sin sesgo lexico", cr, COMPRESSION_RATIO_MAX,
        )
        segs2, info2, ms2 = self._raw_transcribe(
            audio, language=lang, initial_prompt=None, timed=True
        )
        # infer_ms suma los dos intentos: latency.jsonl debe ver el coste real.
        return segs2, info2, infer_ms + ms2, True

    def _raw_transcribe(self, audio: np.ndarray, *, language: str | None,
                        initial_prompt: str | None, timed: bool = False):
        """Llamada cruda al modelo. Sin validaciones ni post-proceso.

        `model.transcribe()` devuelve un GENERADOR: el trabajo real ocurre al
        consumirlo. Por eso el cronometro envuelve tambien el `list(...)`; medir
        solo la llamada daria ~0 ms y una mentira en latency.jsonl.
        """
        from ..metrics import now_ns

        t0 = now_ns()
        gen, info = self._model.transcribe(
            audio,
            language=language,
            beam_size=self.beam_size,
            temperature=self.temperature,
            without_timestamps=self.without_timestamps,
            condition_on_previous_text=self.condition_on_previous_text,
            vad_filter=self.vad_filter,
            no_speech_threshold=self.no_speech_threshold,
            initial_prompt=initial_prompt,
        )
        segments = list(gen)
        infer_ms = (now_ns() - t0) / 1e6
        if timed:
            return segments, info, infer_ms
        return segments, info

    def _clip_prompt(self, hotwords: str | None) -> str | None:
        """Recorta el sesgo lexico a la ventana de prompt de Whisper.

        Mandar mas de 224 tokens no es un error visible: CTranslate2 recorta por
        su cuenta y el usuario nunca se entera de que la mitad de su diccionario
        no llego al decoder. Preferimos recortar aqui y dejar rastro.
        """
        if not hotwords or not hotwords.strip():
            return None
        prompt = hotwords.strip()
        tok = getattr(self._model, "hf_tokenizer", None)
        if tok is None:
            return prompt
        try:
            ids = tok.encode(prompt, add_special_tokens=False).ids
        except Exception:
            return prompt
        if len(ids) <= PROMPT_MAX_TOKENS:
            return prompt
        clipped = tok.decode(ids[:PROMPT_MAX_TOKENS])
        # Cortar en el ultimo espacio para no dejar media palabra sesgando al decoder.
        if " " in clipped:
            clipped = clipped[: clipped.rindex(" ")]
        clipped = clipped.strip()
        log.warning(
            "initial_prompt truncado: %d tokens > %d; se envian %d chars de %d",
            len(ids), PROMPT_MAX_TOKENS, len(clipped), len(prompt),
        )
        return clipped or None

    def _search_hint(self) -> str:
        """Donde se busco el modelo, para que el error diga QUE falta y DONDE."""
        if os.path.sep in self.model_id or (os.path.altsep and os.path.altsep in self.model_id):
            return self.model_id
        root = self.download_root or "~/.cache/huggingface/hub (default de HF)"
        return os.path.join(str(root), f"models--Systran--faster-whisper-{self.model_id}")
