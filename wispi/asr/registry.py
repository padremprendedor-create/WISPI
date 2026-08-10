"""Fabrica de backends ASR y cadena de degradacion.

POR QUE HAY UN REGISTRY Y NO UN `WhisperModel(...)` en app.py:

La escalera de migracion (small CPU -> base CPU -> large-v3 GPU -> Parakeet ONNX)
se apoya solo en el Protocol `ASRBackend`. Aqui es el UNICO sitio que sabe que
nombre de backend corresponde a que clase, y el unico que lee `ASRCfg`. Meter un
motor nuevo es anadir una entrada a `_BUILDERS`.

REGLA DURA: no se degrada en silencio. Cada salto de eslabon deja un WARNING en
el log Y un aviso en la lista que devuelve `build_with_fallback`, para que la
bandeja pueda pintar el icono de error. Un usuario que cree estar usando
`large-v3` y en realidad esta con `base` es un bug de producto, no una
optimizacion.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from ..config import ASRCfg
from ..logging_setup import get
from .base import ASRBackend, ASRLoadError

_log = get("asr.registry")

# Claves de ASRCfg que un eslabon de fallback_chain puede sobrescribir. Lo demas
# (language, cpu_threads, download_root...) se hereda del bloque `asr` principal.
_OVERRIDABLE = {
    "backend", "model", "device", "compute_type", "cpu_threads", "beam_size",
    "without_timestamps", "condition_on_previous_text", "temperature",
    "vad_filter", "no_speech_threshold", "local_files_only", "download_root",
}


def _build_faster_whisper(cfg: ASRCfg) -> ASRBackend:
    # Import perezoso: `import faster_whisper` cuesta segundos y arrastra
    # ctranslate2/onnxruntime. Importar el registry debe ser gratis.
    from .faster_whisper_backend import FasterWhisperBackend

    return FasterWhisperBackend(
        model=cfg.model,
        device=cfg.device,
        compute_type=cfg.compute_type,
        cpu_threads=cfg.cpu_threads,
        beam_size=cfg.beam_size,
        without_timestamps=cfg.without_timestamps,
        condition_on_previous_text=cfg.condition_on_previous_text,
        temperature=cfg.temperature,
        vad_filter=cfg.vad_filter,
        no_speech_threshold=cfg.no_speech_threshold,
        local_files_only=cfg.local_files_only,
        download_root=cfg.download_root,
    )


# Anadir un motor nuevo = una linea aqui + un modulo que cumpla el Protocol.
_BUILDERS: dict[str, Callable[[ASRCfg], ASRBackend]] = {
    "faster-whisper": _build_faster_whisper,
    "faster_whisper": _build_faster_whisper,  # alias tolerante a guion bajo
}


def build(cfg: ASRCfg) -> ASRBackend:
    """Construye el backend descrito por `cfg`. NO llama a `load()`.

    Separar construir de cargar deja que el llamador decida cuando pagar los
    segundos del modelo (arranque perezoso desde la bandeja, por ejemplo).
    """
    key = str(cfg.backend).strip().lower()
    builder = _BUILDERS.get(key)
    if builder is None:
        raise ASRLoadError(
            f"backend ASR desconocido: '{cfg.backend}'. Disponibles: "
            f"{sorted(set(_BUILDERS))}"
        )
    return builder(cfg)


def _merge(cfg: ASRCfg, entry: dict[str, Any]) -> ASRCfg:
    """Un eslabon de la cadena es un parche parcial sobre el bloque `asr`."""
    patch = {k: v for k, v in (entry or {}).items() if k in _OVERRIDABLE}
    return replace(cfg, **patch, fallback_chain=[])


def _key(cfg: ASRCfg) -> tuple:
    return (str(cfg.backend).lower(), cfg.model, cfg.device, cfg.compute_type)


def _label(cfg: ASRCfg) -> str:
    return f"{cfg.backend}/{cfg.model}/{cfg.device}/{cfg.compute_type}"


def chain_of(cfg: ASRCfg) -> list[ASRCfg]:
    """Cadena efectiva: el bloque principal primero, luego `fallback_chain`.

    Se deduplica porque en `config.yaml` el primer eslabon suele repetir el
    modelo principal a proposito (documenta la cadena completa); intentarlo dos
    veces solo pagaria dos veces el fallo.
    """
    out: list[ASRCfg] = []
    seen: set[tuple] = set()
    for candidate in [replace(cfg, fallback_chain=[])] + [
        _merge(cfg, e) for e in (cfg.fallback_chain or []) if isinstance(e, dict)
    ]:
        k = _key(candidate)
        if k in seen:
            continue
        seen.add(k)
        out.append(candidate)
    return out


def build_with_fallback(cfg: ASRCfg, log=None) -> tuple[ASRBackend, list[str]]:
    """Recorre la cadena hasta que un backend carga de verdad.

    Devuelve (backend_ya_cargado, avisos). `avisos` esta vacio si cargo el
    primero; si no, lleva una linea por salto. Si NINGUN eslabon carga, lanza
    ASRLoadError con el historial completo: sin ASR no hay producto, callar eso
    seria peor que morir.
    """
    lg = log if log is not None else _log
    chain = chain_of(cfg)
    warnings: list[str] = []
    failures: list[str] = []

    for i, candidate in enumerate(chain):
        label = _label(candidate)
        try:
            backend = build(candidate)
            backend.load()
        except ASRLoadError as e:
            reason = str(e)
        except Exception as e:  # un backend nuevo puede reventar de otra forma
            reason = f"{type(e).__name__}: {e}"
        else:
            if warnings:
                lg.warning("ASR degradado: en uso '%s' (no era el eslabon preferido)", label)
            else:
                lg.info("ASR listo: '%s'", label)
            return backend, warnings

        failures.append(f"{label}: {reason}")
        nxt = _label(chain[i + 1]) if i + 1 < len(chain) else None
        if nxt:
            aviso = f"ASR '{label}' no cargo ({reason}); se prueba el siguiente eslabon: '{nxt}'"
            lg.warning("%s", aviso)
            warnings.append(aviso)
        else:
            lg.error("ASR '%s' no cargo y no queda ningun eslabon: %s", label, reason)

    raise ASRLoadError(
        "ningun eslabon de asr.fallback_chain pudo cargar:\n  - "
        + "\n  - ".join(failures)
    )
