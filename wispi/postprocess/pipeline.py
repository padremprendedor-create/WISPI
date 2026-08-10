"""Orquestador del post-proceso. Es la unica cara que ve app.py.

POR QUE dos niveles y no uno: son dos productos distintos con dos presupuestos de
latencia distintos.
    Nivel 0 - determinista, microsegundos, SIEMPRE se aplica y su salida es la que se
              inserta de inmediato en la ventana del usuario.
    Nivel 1 - un LLM local, cientos de milisegundos, se aplica solo si el dictado es
              lo bastante largo para que compense, y su salida es un PARCHE opcional
              sobre lo ya insertado.
El contrato de esa asimetria es que `level0()` no puede fallar nunca y `level1()`
puede devolver None cuando quiera. app.py se apoya en eso para la insercion
optimista: escribe el crudo, y solo si llega un parche valido y la ventana sigue
siendo la misma, lo sustituye.

POR QUE `needs_level1` mide sobre el texto POST-nivel-0: el nivel 0 ya borro las
muletillas, asi que las palabras que quedan son las de verdad. Contarlas antes
inflaria el recuento con "eh"s y mandaria al LLM dictados que no lo necesitan,
pagando ~500 ms para nada.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import LLMCfg, PostprocessCfg
from .. import logging_setup
from . import level0 as _level0
from .dictionary import Dictionary
from .hallucinations import is_whisper_hallucination
from .level1_llm import OllamaCleaner


class PostProcessor:
    """Nivel 0 + diccionario + nivel 1, con hot-reload del diccionario."""

    def __init__(self, pp_cfg: PostprocessCfg, llm_cfg: LLMCfg, log: Any = None,
                 dictionary_path: Path | str | None = None) -> None:
        self.pp = pp_cfg
        self.llm_cfg = llm_cfg
        self._log = log or logging_setup.get("postprocess")
        self.dictionary = Dictionary(dictionary_path, log=self._log)
        self.llm = OllamaCleaner(llm_cfg, log=self._log)

    # -- nivel 0 ------------------------------------------------------------
    def level0(self, text: str) -> str:
        """Reglas deterministas. No lanza: ante cualquier fallo devuelve el crudo."""
        if not text:
            return ""
        if not self.pp.enabled:
            return text.strip()
        try:
            return _level0.run(
                text,
                do_fillers=self.pp.strip_fillers,
                do_spacing=self.pp.fix_spacing,
                do_capitalize=self.pp.capitalize,
                do_dictionary=self.pp.apply_dictionary,
                dictionary=self.dictionary,
            )
        except Exception as e:
            # El nivel 0 esta en el camino critico entre el ASR y la insercion. Un
            # regex que peta aqui no puede costarle el dictado al usuario.
            self._log.error("nivel 0 fallo (%s: %s); se inserta el crudo",
                            type(e).__name__, e)
            return text.strip()

    # -- decision del nivel 1 -----------------------------------------------
    def needs_level1(self, text: str) -> bool:
        """True si merece la pena pagar el LLM. Se evalua sobre el texto POST-nivel-0."""
        if not self.llm_cfg.enabled or not text:
            return False
        if self.llm.available is False:
            return False  # Ollama ya se declaro caido; no se vuelve a pagar el timeout
        return len(text.split()) >= self.llm_cfg.min_words

    # -- nivel 1 ------------------------------------------------------------
    def level1(self, text: str) -> str | None:
        """Parche del LLM, o None si se descarta (timeout, caido, guarda C8.6)."""
        if not text:
            return None
        try:
            out = self.llm.clean(text)
        except Exception as e:
            # Cinturon sobre los tirantes: OllamaCleaner ya captura, pero este metodo
            # corre en el hilo del parche y una excepcion suelta lo mataria en silencio.
            self._log.error("nivel 1 fallo (%s: %s); se queda el crudo",
                            type(e).__name__, e)
            return None
        if out is None:
            return None
        # El parche del LLM vuelve a pasar por el diccionario: llama3.1 reescribe
        # "Supabase" como "SupaBase" con bastante alegria, y el canonico manda.
        if self.pp.enabled and self.pp.apply_dictionary:
            out = self.dictionary.apply(out)
        if out.strip() == text.strip():
            return None  # nada que parchear: ahorra una reescritura en la ventana
        return out

    # -- semilla del decoder ------------------------------------------------
    def initial_prompt(self) -> str | None:
        if not self.pp.use_initial_prompt:
            return None
        return self.dictionary.initial_prompt(self.pp.initial_prompt_style)

    def hotwords(self) -> str | None:
        if not self.pp.apply_dictionary:
            return None
        return self.dictionary.hotwords()

    # -- operacion ----------------------------------------------------------
    def reload_dictionary(self) -> bool:
        """Poll de mtime de dictionary.yaml. True si recargo (criterio C7.4)."""
        return self.dictionary.maybe_reload()

    def warmup(self) -> bool:
        """Deja Ollama residente. Llamar al arrancar, en un hilo aparte."""
        return self.llm.warmup()

    def update_config(self, pp_cfg: PostprocessCfg, llm_cfg: LLMCfg) -> None:
        """Aplica un hot-reload de config.yaml sin recrear el objeto."""
        self.pp = pp_cfg
        self.llm_cfg = llm_cfg
        self.llm.update_config(llm_cfg)

    def close(self) -> None:
        self.llm.close()

    # -- utilidad -----------------------------------------------------------
    @staticmethod
    def is_hallucination(text: str) -> bool:
        """Reexport por comodidad; la implementacion vive en hallucinations.py."""
        return is_whisper_hallucination(text)
