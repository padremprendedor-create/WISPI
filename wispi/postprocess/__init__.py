"""Post-proceso de WISPI: nivel 0 determinista + diccionario + nivel 1 con LLM."""
from .dictionary import Dictionary, fold
from .hallucinations import is_whisper_hallucination
from .level1_llm import OllamaCleaner
from .pipeline import PostProcessor

__all__ = [
    "Dictionary",
    "OllamaCleaner",
    "PostProcessor",
    "fold",
    "is_whisper_hallucination",
]
