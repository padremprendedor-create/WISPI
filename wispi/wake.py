"""Palabra de activacion: "hey WISPI" arranca un dictado sin tocar nada.

## POR QUE NO HAY UNA DEPENDENCIA NUEVA

Lo obvio seria meter un detector especializado (Porcupine, openWakeWord, Vosk).
Ninguno de los tres encaja aqui:

  - **Porcupine** exige una AccessKey de Picovoice, o sea una cuenta. WISPI no
    tiene cuentas ni claves: es su razon de existir frente a Wispr Flow.
  - **openWakeWord** trae modelos preentrenados en ingles ("hey jarvis",
    "alexa"). "hey wispi" no esta, y entrenarlo son horas de pipeline sintetico.
  - **Vosk** necesita un modelo aparte de 40 MB y, peor, su gramatica restringida
    solo admite palabras de su lexico: "wispi" no existe en el lexico espanol y
    no se puede anadir sin recompilar el grafo.

Aqui ya hay un ASR cargado, un stream de microfono siempre abierto y un umbral
de RMS calibrado. Con eso se hace un detector honesto y sin descargas nuevas.

## POR QUE ESTO NO QUEMA LA CPU

La trampa evidente de "whisper como wake word" es correrlo sobre una ventana
deslizante cada N milisegundos. El coste del encoder es FIJO (`tiny` = 0,21 s
MEDIDOS en esta maquina, pase lo que pase), asi que una pasada por segundo son
ventiladores para siempre.

Este modulo no hace eso. Segmenta primero y reconoce despues:


    silencio ──▶ [voz ──▶ silencio de 350 ms] ──▶ candidato ──▶ ASR

Solo llega al ASR un **enunciado corto y aislado**: entre `min_speech_s` y
`max_speech_s` de voz, cerrado por `end_silence_s` de silencio. Eso es
exactamente la forma que tiene decir "hey WISPI" y soltar. Con la sala callada
el ASR no se llama NUNCA (el coste es un RMS por bloque de 30 ms, que el
callback de audio ya calculaba). Y una conversacion seguida, una llamada o la
tele tampoco producen candidatos: no hay enunciado corto aislado, hay habla
continua, y esa se descarta por `max_speech_s` sin mirar el contenido.

MEDIDO en esta maquina (i9-10850K, `tiny` int8): 440 ms por candidato con
`cpu_threads: 2`, 210 ms con 10. El coste es FIJO -whisper rellena a 30 s pase
lo que pase-, asi que un clip de 0,8 s y uno de 2 s cuestan lo mismo. La espera
percibida entre callar y oir el tono es `end_silence_s` (350 ms) + eso.

## POR QUE EL EMPAREJADO ES DIFUSO Y NO EXACTO

"wispi" no es una palabra espanola y `tiny` no la ha visto nunca. Lo que
devuelve de verdad es "Hey, Wispi.", "Ey Wispy", "Hey, Guispi", "Ay, Wispi" o
"hey wis pi". Exigir la cadena exacta seria exigir que el modelo acierte una
palabra inventada: no pasa. Se compara por similitud, con DOS umbrales:

  1. la frase entera contra la frase configurada (`threshold`), y
  2. **el nombre solo** contra el mejor sufijo del candidato (`name_threshold`).

El segundo es el que hace el trabajo fino. Sin el, "hey wifi" pasa: da 0,80
contra "hey wispi" porque comparten "hey" y "wi". Con el, "wifi" contra "wispi"
da 0,67 y se cae. Ese caso concreto es el que fija `name_threshold` en 0,70.

## HILOS

    PortAudio cb  -> `feed()`: un `deque.append` y nada mas. La regla del
                     callback de audio.py (ni locks, ni logging, ni excepciones)
                     tambien manda aqui.
    wispi-wake    -> ESTE bucle. Carga el modelo, segmenta, reconoce, decide.
                     Puede bloquear: es suyo.

El detector se DESARMA en cuanto la maquina de estados sale de IDLE. Sin eso se
oiria a si mismo -el dictado del usuario pasaria por el segmentador- y
competiria por la CPU justo con el ASR de verdad.
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections import deque
from dataclasses import replace
from difflib import SequenceMatcher
from typing import Any

import numpy as np

from .config import ASRCfg, WakeCfg
from .events import HookEvent
from .metrics import now_ns

# El hilo duerme esto cuando la cola esta vacia. 15 ms es medio bloque de audio:
# ni se acumula latencia ni se hace busy-wait.
POLL_S = 0.015

# Tope de la cola entre el callback y el hilo. ~4 s de audio. Es acotada A
# PROPOSITO: mientras el ASR trabaja (200-400 ms) los bloques nuevos se
# descartan por el extremo viejo, que es justo lo que se quiere -no interesa
# analizar el audio que ocurrio mientras se analizaba el anterior.
QUEUE_BLOCKS = 140


# ======================================================================
# Emparejado difuso. Puro, sin estado: es lo que hace `tools/test_wake.py`
# capaz de verificar C11.6 y C11.7 sin voz ni microfono.
# ======================================================================
def normalize(text: str) -> str:
    """A minusculas, sin tildes y sin puntuacion. 'Hey, Wispi!' -> 'hey wispi'.

    Con una regla fonetica: la -y final pasa a -i. En espanol suenan igual y el
    modelo elige una u otra sin criterio ("Wispi" / "Wispy" / "Guispy" salen las
    tres del mismo audio). Se aplica a los DOS lados de la comparacion, asi que
    no inventa parecidos: solo deja de castigar una diferencia que no es de
    pronunciacion sino de ortografia. MEDIDO: sin esto, "hey guispy" se queda en
    0,706 y no dispara; con esto, 0,824.
    """
    t = unicodedata.normalize("NFKD", str(text).lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return " ".join(w[:-1] + "i" if len(w) > 1 and w.endswith("y") else w
                    for w in t.split())


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _name_score(window: str, name: str) -> float:
    """Mejor parecido del nombre contra CUALQUIER sufijo del candidato.

    Por sufijos y no por tokens porque el modelo parte la palabra donde quiere:
    "hey wis pi" tiene el nombre repartido en dos tokens y ninguno de los dos,
    por separado, se parece a "wispi".
    """
    best = 0.0
    for i in range(len(window)):
        # Un sufijo mas corto que la mitad del nombre no puede dar un ratio util
        # y solo anade ruido al maximo.
        if len(window) - i < len(name) // 2:
            break
        r = _ratio(window[i:], name)
        if r > best:
            best = r
    return best


def match(text: str, phrases: list[str], name: str, *, threshold: float = 0.75,
          name_threshold: float = 0.70, max_tokens: int = 5) -> tuple[bool, float, str]:
    """(¿es la palabra de activacion?, mejor puntuacion, frase que gano).

    `max_tokens` corta por lo sano los enunciados largos: "hey wispi" son dos
    palabras y buscarla dentro de una frase de quince es pedir un falso positivo.
    """
    norm = normalize(text)
    if not norm:
        return False, 0.0, ""
    tokens = norm.split()
    if len(tokens) > max_tokens:
        return False, 0.0, ""

    name_n = normalize(name)
    best_score, best_phrase, ok = 0.0, "", False

    for phrase in phrases:
        target = normalize(phrase).replace(" ", "")
        if not target:
            continue
        n = len(normalize(phrase).split())
        # Ventanas de n-1, n y n+1 tokens: el modelo une ("heywispi") o parte
        # ("wis pi") la frase con total libertad. Se comparan sin espacios, asi
        # que donde caigan los cortes deja de importar.
        for size in {max(1, n - 1), n, n + 1}:
            for i in range(0, max(1, len(tokens) - size + 1)):
                window = "".join(tokens[i:i + size])
                if not window:
                    continue
                full = _ratio(window, target)
                if full > best_score:
                    best_score, best_phrase = full, phrase
                if full >= threshold and _name_score(window, name_n) >= name_threshold:
                    ok = True
    return ok, round(best_score, 3), best_phrase


# ======================================================================
class WakeWord:
    """Detector de palabra de activacion. Vive en su propio hilo.

    Contrato con `app.py`:
        start()            -> arranca el hilo (carga el modelo dentro, no bloquea)
        feed(block, rms)   -> lo llama el callback de PortAudio. Barato o nada.
        set_armed(bool)    -> solo escucha cuando la maquina de estados esta IDLE
        stop()
    Al detectar, encola `(HookEvent.WAKE, 0, t_ns)` en la MISMA cola que el hook.
    Una sola entrada a la maquina de estados; aqui no se decide nada de producto.
    """

    def __init__(self, cfg: WakeCfg, asr_cfg: ASRCfg, capture: Any, events_q: Any,
                 log: Any, *, log_text: bool = False) -> None:
        self.cfg = cfg
        self.asr_cfg = asr_cfg
        self.capture = capture          # AudioCapture: rate real + conversion a 16 kHz
        self.events_q = events_q
        self.log = log
        self.log_text = log_text

        self._q: deque = deque(maxlen=QUEUE_BLOCKS)
        self._armed = threading.Event()
        self._running = threading.Event()
        self._thread: threading.Thread | None = None
        self._asr = None
        self._error: str | None = None
        self._cooldown_until = 0.0

        # -- segmentacion (solo el hilo wispi-wake toca esto) ----------------
        self._pre: deque = deque()
        self._buf: list[np.ndarray] = []
        self._in_speech = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._holdoff = False           # habla continua: esperar silencio real
        # Los pone `_configure()` en cuanto se conoce el rate REAL del stream (el
        # dispositivo puede rechazar 16 kHz y abrirse a 48). Se declaran aqui para
        # que el objeto sea inspeccionable antes de que llegue el primer bloque.
        self._min_speech = 0
        self._max_speech = 0
        self._end_silence = 0
        self._rms_threshold = float(cfg.rms_threshold or 0.012)
        self._needs_reconfigure = False

        # -- diagnostico -----------------------------------------------------
        self.checks = 0                 # veces que se ha llamado al ASR
        self.detections = 0
        self.last_score = 0.0
        self.last_text = ""
        self.last_infer_ms = 0.0

    # ------------------------------------------------------------------ ciclo
    def start(self) -> bool:
        if not self.cfg.enabled:
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._running.set()
        self._thread = threading.Thread(target=self._run, name="wispi-wake", daemon=True)
        self._thread.start()
        return True

    def stop(self, join: bool = True) -> None:
        """`join=False` para apagarlo desde el hilo de estado.

        El hilo puede estar cargando el modelo (segundos) y ese hilo es el que
        atiende Ctrl+Win: esperarlo ahi dejaria el teclado mudo mientras tanto.
        Es daemon y sale solo en cuanto ve `_running` bajado.
        """
        self._running.clear()
        self._armed.clear()
        t = self._thread
        if join and t and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        asr, self._asr = self._asr, None
        if asr is not None:
            try:
                asr.unload()
            except Exception:
                pass

    def set_armed(self, armed: bool) -> None:
        """Lo llama `app.py` en cada cambio de estado. Barato a proposito."""
        if armed:
            self._armed.set()
        else:
            self._armed.clear()

    def reconfigure(self) -> None:
        """Pide re-derivar los frames del segmentador. Lo llama `app.py` al recargar.

        `min_speech_s` y compania se traducen a FRAMES una sola vez, asi que
        cambiarlos en config.yaml no se notaria aunque `self.cfg` ya sea el valor
        nuevo. Se hace por bandera y no aqui mismo porque quien tiene que
        recalcular es el hilo de wake: tocar `_pre` desde fuera pisaria al unico
        escritor que tiene ese deque.
        """
        self._needs_reconfigure = True

    # ------------------------------------------------------- callback de audio
    def feed(self, block: np.ndarray, rms: float) -> None:
        """Hilo de PortAudio. UNA operacion: `deque.append` es atomica bajo el GIL.

        Ni siquiera se comprueba si esta armado: eso es un `Event.is_set()`, que
        toma un lock, y en el callback no se toman locks. Los bloques sobrantes
        los tira el hilo de wake, que si puede permitirselo.
        """
        self._q.append((block, rms))

    # ------------------------------------------------------------------- hilo
    def _run(self) -> None:
        if not self._load():
            return
        rate = 0
        while self._running.is_set():
            try:
                block, rms = self._q.popleft()
            except IndexError:
                time.sleep(POLL_S)
                continue

            if not self._armed.is_set() or time.monotonic() < self._cooldown_until:
                # Desarmado: se drena y se olvida. Sin resetear aqui, la primera
                # palabra del usuario tras un dictado quedaria pegada al principio
                # del siguiente enunciado candidato.
                if self._in_speech or self._buf:
                    self._reset_segment()
                continue

            # Se relee en cada vuelta y no una sola vez: si el micro USB se
            # desconecta y vuelve, `AudioCapture.start()` puede reabrir el stream
            # a OTRO rate (48 kHz si el nuevo dispositivo rechaza 16), y un
            # segmentador con los frames calculados para el rate viejo mide los
            # silencios con una regla que ya no vale.
            now_rate = int(getattr(self.capture, "stream_rate", 0) or 0)
            if now_rate <= 0:
                continue
            if now_rate != rate or self._needs_reconfigure:
                self._needs_reconfigure = False
                rate = now_rate
                self._reset_segment()
                self._configure(rate)

            try:
                self._on_block(block, rms, rate)
            except Exception:
                self.log.exception("wake: fallo procesando un bloque")
                self._reset_segment()

    def _load(self) -> bool:
        """Construye el modelo del detector. Nunca propaga: sin wake se sigue dictando."""
        from .asr.registry import build_with_fallback

        # Se hereda del bloque `asr` (idioma, download_root, local_files_only) y se
        # sobrescribe lo que distingue a un detector de un transcriptor: modelo
        # minusculo, pocos hilos -no puede robarle los 10 nucleos al ASR real- y
        # `vad_filter=False`, porque el recorte de voz ya lo hizo el segmentador y
        # Silero sobre un clip de 1 s a veces devuelve la nada.
        cfg = replace(
            self.asr_cfg,
            model=self.cfg.model,
            cpu_threads=max(1, int(self.cfg.cpu_threads)),
            beam_size=1,
            temperature=0.0,
            without_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=False,
            fallback_chain=list(self.cfg.fallback_chain or []),
        )
        t0 = now_ns()
        try:
            self._asr, warns = build_with_fallback(cfg, self.log)
            self._asr.warmup()
        except Exception as e:
            # C11.8: sin detector se pierde la palabra de activacion, no el dictado.
            self._error = f"{type(e).__name__}: {e}"
            self.log.warning(
                "wake: no pude cargar el detector (%s). La palabra de activacion queda "
                "DESACTIVADA; Ctrl+Win y el boton flotante siguen funcionando. Si el "
                "modelo '%s' no esta en disco, pon asr.local_files_only en false una vez "
                "para que se descargue.", self._error, self.cfg.model)
            return False
        for w in warns:
            self.log.warning("wake: %s", w)
        d = self._asr.describe()
        self.log.info("wake: detector listo (%s, %d hilos, %.0f ms de carga) | frases=%s",
                      d.get("model"), cfg.cpu_threads, (now_ns() - t0) / 1e6,
                      ", ".join(self.cfg.phrases))
        return True

    def _configure(self, rate: int) -> None:
        """Traduce los segundos de config a frames del rate REAL del stream."""
        self._min_speech = int(self.cfg.min_speech_s * rate)
        self._max_speech = int(self.cfg.max_speech_s * rate)
        self._end_silence = int(self.cfg.end_silence_s * rate)
        self._pre = deque(maxlen=max(1, round(self.cfg.preroll_ms / 30)))
        self._rms_threshold = (self.cfg.rms_threshold if self.cfg.rms_threshold is not None
                               else self.capture.cfg.silence_threshold)
        self.log.debug("wake: segmentador a %d Hz (min=%d max=%d fin=%d frames, rms=%.4f)",
                       rate, self._min_speech, self._max_speech, self._end_silence,
                       self._rms_threshold)

    def _reset_segment(self) -> None:
        self._buf = []
        self._in_speech = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._pre.clear()

    # --------------------------------------------------------- segmentacion
    def _on_block(self, block: np.ndarray, rms: float, rate: int) -> None:
        n = block.shape[0]
        voz = rms >= self._rms_threshold

        if self._holdoff:
            # Se venia de habla continua (una frase larga, una llamada, la tele).
            # No se vuelve a considerar nada hasta que haya un silencio de verdad:
            # si no, cada `max_speech_s` se cortaria un trozo del discurso y se
            # mandaria al ASR, que es justo el gasto que este modulo evita.
            self._silence_frames = 0 if voz else self._silence_frames + n
            if self._silence_frames >= self._end_silence:
                self._holdoff = False
                self._reset_segment()
            return

        if not self._in_speech:
            if not voz:
                self._pre.append(block)
                return
            # Arranca el enunciado. El pre-roll corto evita comerse la 'h' de "hey".
            self._buf = list(self._pre)
            self._pre.clear()
            self._buf.append(block)
            self._in_speech = True
            self._speech_frames = n
            self._silence_frames = 0
            return

        self._buf.append(block)
        if voz:
            self._speech_frames += n
            self._silence_frames = 0
            if self._speech_frames > self._max_speech:
                # Demasiado largo para ser "hey WISPI": ni se mira el contenido.
                self._holdoff = True
                self._buf = []
                self._speech_frames = 0
                self._silence_frames = 0
            return

        self._silence_frames += n
        if self._silence_frames < self._end_silence:
            return

        speech_frames, buf = self._speech_frames, self._buf
        self._reset_segment()
        if speech_frames >= self._min_speech and buf:
            self._recognize(buf, rate)

    # ----------------------------------------------------------- reconocimiento
    def _recognize(self, buf: list[np.ndarray], rate: int) -> None:
        try:
            audio = self.capture.to_target(np.concatenate(buf, axis=0))
        except Exception as e:
            self.log.debug("wake: no pude preparar el audio: %s", e)
            return
        if audio.size == 0:
            return

        self.checks += 1
        try:
            res = self._asr.transcribe(
                audio, language=self.asr_cfg.language,
                hotwords=self.cfg.initial_prompt or None,
            )
        except Exception as e:
            self.log.debug("wake: el detector fallo al transcribir: %s", e)
            return

        self.last_infer_ms = res.infer_ms
        ok, score, phrase = match(
            res.text, self.cfg.phrases, self.cfg.name,
            threshold=self.cfg.threshold, name_threshold=self.cfg.name_threshold,
            max_tokens=self.cfg.max_tokens,
        )
        self.last_score = score
        # C11.9: lo que oye el detector NO se guarda salvo que el usuario haya
        # pedido texto en los logs a proposito. Es un microfono siempre puesto;
        # dejar rastro de todo lo que descarta seria peor que no tener la feature.
        self.last_text = res.text.strip() if self.log_text else ""
        self.log.debug("wake: candidato %.2f s -> %.3f (%s) [%s]",
                       audio.size / 16000, score, "SI" if ok else "no",
                       res.text.strip() if self.log_text else "oculto")

        if not ok:
            return
        self.detections += 1
        self._cooldown_until = time.monotonic() + self.cfg.cooldown_s
        self._q.clear()     # el audio de la propia frase no entra al siguiente ciclo
        self.log.info("wake: activado por voz (%.3f contra '%s', %.0f ms)",
                      score, phrase, res.infer_ms)
        self.events_q.put((HookEvent.WAKE, 0, now_ns()))

    # ------------------------------------------------------------------ estado
    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stats(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "alive": self.is_alive,
            "armed": self._armed.is_set(),
            "ready": self._asr is not None,
            "model": (self._asr.describe().get("model") if self._asr else None),
            "phrases": list(self.cfg.phrases),
            "checks": self.checks,
            "detections": self.detections,
            "last_score": self.last_score,
            "last_infer_ms": round(self.last_infer_ms),
            "error": self._error,
        }
