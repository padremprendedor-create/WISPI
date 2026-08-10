"""Nivel 1: limpieza con un LLM local (Ollama). Opcional y desechable por diseno.

POR QUE puede devolver None en casi cualquier punto: el nivel 1 es un LUJO. El texto
crudo del nivel 0 ya esta insertado en la ventana del usuario cuando esto corre; lo
unico que hace el nivel 1 es proponer una version mejor. Si Ollama esta caido, si
tarda mas de lo permitido, o si devuelve algo sospechoso, la respuesta correcta es
"no propongo nada" y quedarse con el crudo. Un fallo del LLM NUNCA puede degradar el
resultado ni tumbar la app (criterios C8.5 y C8.6).

POR QUE el warmup es OBLIGATORIO y no una optimizacion: Ollama descarga el modelo de
RAM cuando pasa el keep_alive. Con el modelo frio, la primera llamada tarda 3-5 s
cargando pesos, o sea que se come entero el timeout de 3 s y el primer dictado del
dia NUNCA se parchea. Se manda un POST con num_predict=1 y keep_alive=-1 al arrancar
para dejarlo residente.

POR QUE el prompt insiste tanto en "no respondas": es el fallo mas probable de un
modelo instruct. Si el usuario dicta "como configuro las policies de RLS", llama3.1
quiere explicarle RLS. La contramedida que de verdad funciona no es pedirlo por
favor, es el ejemplo few-shot con una pregunta dentro, que fija la forma de la
salida. Verificado en el banco de pruebas.
"""
from __future__ import annotations

import math
import re
from typing import Any

import httpx

from ..config import LLMCfg
from .. import logging_setup

# Aproximacion de tokens para espanol. Sirve para acotar num_predict, no para facturar.
CHARS_PER_TOKEN_ES = 3.5
# Suelo de num_predict: sin el, una entrada corta se corta a media frase.
MIN_NUM_PREDICT = 48
# El warmup carga pesos del disco: no puede compartir el timeout del dictado.
WARMUP_TIMEOUT_S = 90.0

_SYSTEM = (
    "Eres un corrector de dictado por voz. Recibes la transcripcion cruda de una "
    "persona hablando y devuelves ESE MISMO mensaje, solo que bien escrito.\n"
    "\n"
    "PROHIBIDO:\n"
    "- Responder al contenido. Si el texto es una pregunta o una orden, devuelves esa "
    "pregunta u orden ya limpia, NUNCA su respuesta.\n"
    "- Anadir, quitar o inventar informacion, saludos, explicaciones o comentarios.\n"
    "- Traducir. La salida va en el mismo idioma que la entrada.\n"
    "- Usar markdown, comillas, vinetas, titulos o bloques de codigo.\n"
    "- Cambiar los terminos tecnicos: se copian letra por letra tal como vienen.\n"
    "\n"
    "PERMITIDO: quitar muletillas y repeticiones, poner puntuacion y mayusculas, "
    "corregir concordancias evidentes.\n"
    "\n"
    "Respondes con el texto limpio y nada mas."
)

# Few-shot con una pregunta y una orden: es lo que impide que el modelo conteste.
_SHOTS = (
    "Entrada: eh o sea como configuro las policies de rls en supabase sabes\n"
    "Salida: \u00bfComo configuro las policies de RLS en Supabase?\n"
    "\n"
    "Entrada: pues nada hazme un commit con el mensaje de arriba y luego push\n"
    "Salida: Hazme un commit con el mensaje de arriba y luego push.\n"
    "\n"
    "Entrada: em cuanto cuesta desplegar esto en vercel\n"
    "Salida: \u00bfCuanto cuesta desplegar esto en Vercel?\n"
    "\n"
)

# --- guardas de salida (criterio C8.6) --------------------------------------
_RE_FENCE = re.compile(r"```|~~~")
_RE_PREAMBLE = re.compile(
    r"^\s*(?:aqui\s+(?:esta|tienes|va)|here\s+is|here's|claro|por\s+supuesto"
    r"|texto\s+limpio|salida|entrada|el\s+texto\s+corregido|corregido)\b"
    r"|^\s*(?:aqui\s+esta|here\s+is)",
    re.IGNORECASE)
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u00ab", "\u00bb"))


class OllamaCleaner:
    """Cliente sincrono de /api/generate con todas las guardas puestas."""

    def __init__(self, cfg: LLMCfg, log: Any = None) -> None:
        self.cfg = cfg
        self._log = log or logging_setup.get("postprocess")
        self._client: httpx.Client | None = None
        self.available: bool | None = None  # None = aun no se ha probado

    # -- ciclo de vida ------------------------------------------------------
    def _get_client(self, timeout: float) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.cfg.base_url.rstrip("/"))
        self._client.timeout = httpx.Timeout(timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def update_config(self, cfg: LLMCfg) -> None:
        """Tras un hot-reload de config.yaml puede haber cambiado la base_url."""
        if cfg.base_url != self.cfg.base_url:
            self.close()
        self.cfg = cfg

    # -- warmup -------------------------------------------------------------
    def warmup(self) -> bool:
        """Deja el modelo residente en RAM. Obligatorio al arrancar."""
        if not self.cfg.enabled:
            return False
        payload = {
            "model": self.cfg.model,
            "prompt": "ok",
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "options": {"num_predict": 1, "temperature": 0.0},
        }
        try:
            r = self._get_client(WARMUP_TIMEOUT_S).post("/api/generate", json=payload)
            r.raise_for_status()
        except httpx.ConnectError as e:
            self.available = False
            self._log.warning("Ollama inalcanzable en %s (%s); nivel 1 desactivado "
                              "hasta que vuelva", self.cfg.base_url, e)
            return False
        except Exception as e:
            self.available = False
            self._log.warning("warmup de Ollama fallido (%s: %s); nivel 1 degradado",
                              type(e).__name__, e)
            return False
        self.available = True
        self._log.info("Ollama listo: modelo %s residente (keep_alive=%s)",
                       self.cfg.model, self.cfg.keep_alive)
        return True

    # -- limpieza -----------------------------------------------------------
    def clean(self, text: str) -> str | None:
        """Devuelve el texto limpio, o None si hay que quedarse con el crudo."""
        if not self.cfg.enabled or not text or not text.strip():
            return None

        # Un limpiador no puede escribir mas que el original: acotar num_predict evita
        # que un modelo que se despista se ponga a redactar durante todo el timeout.
        est_in = len(text) / CHARS_PER_TOKEN_ES
        num_predict = max(MIN_NUM_PREDICT, math.ceil(est_in * 1.3))

        payload = {
            "model": self.cfg.model,
            "system": _SYSTEM,
            "prompt": f"{_SHOTS}Entrada: {text.strip()}\nSalida:",
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "options": {
                "temperature": self.cfg.temperature,
                "num_predict": num_predict,
                "top_p": 1.0,
                "seed": 0,
                "stop": ["\nEntrada:", "Entrada:", "\n\n"],
            },
        }

        try:
            r = self._get_client(self.cfg.timeout_s).post("/api/generate", json=payload)
            r.raise_for_status()
            raw = (r.json() or {}).get("response") or ""
        except httpx.ConnectError as e:
            self.available = False
            self._log.warning("Ollama caido (%s); se queda el crudo del nivel 0", e)
            return None
        except httpx.TimeoutException:
            self._log.warning("Ollama excedio %.1fs; se queda el crudo del nivel 0",
                              self.cfg.timeout_s)
            return None
        except Exception as e:
            self._log.warning("Ollama fallo (%s: %s); se queda el crudo",
                              type(e).__name__, e)
            return None

        self.available = True
        out = self._sanitize(raw, text)
        return out

    # -- guardas ------------------------------------------------------------
    def _sanitize(self, raw: str, original: str) -> str | None:
        """Aplica las guardas del criterio C8.6. None = descartar."""
        out = (raw or "").strip()
        if not out:
            self._log.debug("nivel 1 descartado: salida vacia")
            return None

        # A veces repite la etiqueta del few-shot.
        if out.lower().startswith("salida:"):
            out = out[len("salida:"):].strip()

        # Una sola linea: si el modelo se puso a listar, ya no es una limpieza.
        if "\n" in out:
            self._log.debug("nivel 1 descartado: salida multilinea")
            return None

        if _RE_FENCE.search(out):
            self._log.debug("nivel 1 descartado: trae valla de codigo markdown")
            return None

        if _RE_PREAMBLE.search(out):
            self._log.debug("nivel 1 descartado: trae preambulo conversacional")
            return None

        # Comillas envolventes que el original no tenia: el modelo esta CITANDO el
        # texto en vez de devolverlo, lo que suele venir acompanado de anadidos.
        for op, cl in _QUOTE_PAIRS:
            if out.startswith(op) and out.endswith(cl) and len(out) > 1:
                if not (original.strip().startswith(op) and original.strip().endswith(cl)):
                    self._log.debug("nivel 1 descartado: viene entrecomillado")
                    return None

        base = len(original.strip())
        if base and abs(len(out) - base) / base > self.cfg.max_len_delta:
            self._log.warning(
                "nivel 1 descartado: longitud %d vs %d (delta > %.0f%%)",
                len(out), base, self.cfg.max_len_delta * 100)
            return None

        return out
