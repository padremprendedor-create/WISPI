"""Chimes de estado. Sin ellos el usuario no sabe si le esta escuchando.

DECISIONES
  - Se GENERAN con numpy, no se cargan de WAV. Son cuatro senos con envolvente:
    embarcar ficheros binarios en el repo para esto seria absurdo.
  - Se pregeneran AL ARRANCAR, no en cada uso. Sintetizar 120 ms de audio cuesta
    poco, pero el chime de inicio suena justo cuando el usuario pulsa la tecla y
    ahi no sobra ni un milisegundo.
  - Se reproducen en un OutputStream aparte y NO bloqueante. Si el chime falla,
    el dictado sigue: es feedback, no funcionalidad.
  - Volumen bajo por defecto (0.15). Esto suena decenas de veces al dia; un
    chime estridente se desactiva el primer dia y entonces no sirve para nada.
"""
from __future__ import annotations

import threading

import numpy as np

from . import logging_setup

log = logging_setup.get("feedback")
SR = 24000


def _tone(freqs: list[float], dur_s: float, volume: float,
          attack_s: float = 0.008, release_s: float = 0.05) -> np.ndarray:
    """Suma de senos con envolvente ADSR simplificada.

    La envolvente no es estetica: un seno que arranca y para en seco produce un
    'click' audible (discontinuidad de primera derivada) que suena a fallo."""
    n = int(SR * dur_s)
    t = np.arange(n, dtype=np.float32) / SR
    wave = np.zeros(n, dtype=np.float32)
    for i, f in enumerate(freqs):
        wave += np.sin(2 * np.pi * f * t).astype(np.float32) / (i + 1)
    env = np.ones(n, dtype=np.float32)
    # Attack y release se ACOTAN a la duracion del tono. El chime "ready" dura
    # 45 ms y el release por defecto son 50 ms: sin este tope, la rampa es mas
    # larga que el propio tono y numpy revienta al asignar. Costo de no acotarlo:
    # la app no arranca, y con pythonw ni siquiera se ve el error.
    a = max(1, min(int(SR * attack_s), n // 2))
    r = max(1, min(int(SR * release_s), n - a))
    env[:a] = np.linspace(0, 1, a, dtype=np.float32)
    env[-r:] = np.linspace(1, 0, r, dtype=np.float32)
    return (wave * env * volume).astype(np.float32)


class Feedback:
    def __init__(self, enabled: bool = True, volume: float = 0.15) -> None:
        self.enabled = enabled
        self.volume = float(volume)
        self._sounds: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()
        if enabled:
            self._build()

    def _build(self) -> None:
        v = self.volume
        # Ascendente = "te escucho"; descendente = "termine"; grave = "cancelado".
        # La direccion de la melodia comunica mas rapido que el timbre.
        self._sounds = {
            "start":  np.concatenate([_tone([880.0], 0.055, v), _tone([1174.0], 0.070, v)]),
            "stop":   np.concatenate([_tone([1174.0], 0.050, v), _tone([880.0], 0.065, v)]),
            "cancel": _tone([330.0, 220.0], 0.130, v * 0.9),
            "error":  np.concatenate([_tone([220.0], 0.09, v), _tone([180.0], 0.13, v)]),
            "ready":  np.concatenate([_tone([660.0], 0.045, v * 0.7),
                                      _tone([880.0], 0.045, v * 0.7),
                                      _tone([1320.0], 0.075, v * 0.7)]),
        }

    def play(self, name: str) -> None:
        """No bloquea y no puede tumbar nada. El feedback es prescindible."""
        if not self.enabled:
            return
        buf = self._sounds.get(name)
        if buf is None:
            return
        threading.Thread(target=self._play_blocking, args=(buf,),
                         name="wispi-chime", daemon=True).start()

    def _play_blocking(self, buf: np.ndarray) -> None:
        try:
            import sounddevice as sd
            with self._lock:          # evita solapar dos chimes en el mismo device
                sd.play(buf, SR, blocking=True)
        except Exception as e:
            # Un fallo de audio de salida NUNCA puede afectar al dictado.
            log.debug("chime fallo: %s", e)

    def set_volume(self, v: float) -> None:
        self.volume = float(v)
        if self.enabled:
            self._build()
