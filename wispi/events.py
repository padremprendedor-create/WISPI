"""Contrato de eventos entre hilos y estados de la maquina.

Regla dura: lo que viaja del hilo del hook al hilo de estado tiene que ser BARATO
de construir. El callback del hook tiene un presupuesto de 300 ms (timeout de
`LowLevelHooksTimeout`) y en Python el riesgo real no es tardar, sino no poder
EMPEZAR porque otro hilo tiene el GIL. Por eso el hook encola TUPLAS PLANAS, no
dataclasses: construir una tupla es una sola operacion de bytecode.

Las dataclasses de aqui son para el hilo de estado, que si puede permitirselas.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class HookEvent(enum.IntEnum):
    """Tipo de evento que el hook encola. Se usa como primer campo de la tupla."""

    COMBO_DOWN = 1      # el combo completo (Ctrl+Win) acaba de quedar pulsado
    COMBO_UP = 2        # se solto la primera tecla del combo
    CANCEL = 3          # Esc pulsado
    USER_KEY = 4        # cualquier otra tecla del usuario (invalida el parche optimista)
    HOOK_SLOW = 5       # el propio callback tardo mas de lo tolerable -> re-enganchar
    # El hook NUNCA emite esto: lo pone el boton flotante. Viaja por la misma cola
    # a proposito, para que la maquina de estados siga teniendo UNA sola entrada y
    # no haya dos caminos que puedan divergir.
    UI_TOGGLE = 6
    # Tampoco lo emite el hook: lo pone `wake.py` al oir "hey WISPI". Misma cola,
    # misma razon. La diferencia con UI_TOGGLE es que este NUNCA corta un dictado
    # en curso (el detector esta desarmado mientras se graba) y arranca sin
    # pre-roll, para que la propia frase de activacion no acabe escrita.
    WAKE = 7


# Lo que viaja por la cola: (HookEvent, vk, t_ns). Tupla plana, sin allocs extra.
HookTuple = tuple[int, int, int]


class State(enum.Enum):
    """Estados de la maquina. Solo `app.py` los transiciona."""

    IDLE = "idle"
    RECORDING = "recording"          # push-to-talk: la tecla sigue abajo
    HANDS_FREE = "hands_free"        # toggle: se corta por VAD
    TRANSCRIBING = "transcribing"
    POLISHING = "polishing"          # nivel 1 en vuelo, crudo ya insertado
    PAUSED = "paused"                # el usuario pauso desde la bandeja
    ERROR = "error"


@dataclass
class Dictation:
    """Todo lo que se sabe de un dictado en curso. Vive en el hilo de estado."""

    seq: int
    t_keydown_ns: int
    t_keyup_ns: int = 0
    audio_ms: float = 0.0
    raw_text: str = ""
    level0_text: str = ""
    final_text: str = ""
    # Ventana destino capturada en el momento del key-up. Las cuatro compuertas
    # del parche optimista comparan contra esto.
    target_hwnd: int = 0
    target_pid: int = 0
    target_exe: str = ""
    route: str = ""
    inserted_len: int = 0
    patched: bool = False
    discarded_reason: str | None = None
    # Contador de teclas del usuario en el momento de insertar el crudo. Si al
    # volver el LLM ha cambiado, el usuario siguio escribiendo -> no se parchea.
    user_keys_at_insert: int = 0
    extra: dict = field(default_factory=dict)
