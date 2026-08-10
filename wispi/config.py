"""Carga y validacion de config.yaml a dataclasses tipadas.

Contrato con el resto del programa:
    - Todo valor tiene default AQUI. `config.yaml` solo lleva lo que se cambia.
    - Ningun modulo lee el YAML por su cuenta ni conoce rutas de fichero.
    - `Config.maybe_reload()` hace poll de mtime; devuelve True si recargo.
      Se llama desde el hilo de estado, nunca desde el callback del hook.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"

# Capa de overrides que escribe el editor grafico.
#
# POR QUE NO ESCRIBIR DIRECTAMENTE EN config.yaml: PyYAML no preserva comentarios.
# La primera vez que la interfaz guardara, se llevaria por delante todas las
# explicaciones de por que cada valor es el que es -que es la parte cara del
# fichero- y las dejaria como un diccionario mudo. Asi config.yaml sigue siendo la
# referencia legible y editable a mano, y esto de aqui es una capa fina encima.
LOCAL_PATH = ROOT / "config.local.yaml"


@dataclass
class AudioCfg:
    sample_rate: int = 16000
    input_device: int | str | None = None
    block_ms: int = 30
    preroll_ms: int = 300
    tail_ms: int = 200
    max_duration_s: float = 60.0
    min_duration_s: float = 0.35
    silence_threshold: float = 0.012
    silence_duration_s: float = 1.2
    start_grace_s: float = 0.3

    @property
    def blocksize(self) -> int:
        return int(self.sample_rate * self.block_ms / 1000)

    @property
    def preroll_blocks(self) -> int:
        return max(1, round(self.preroll_ms / self.block_ms))


@dataclass
class HotkeyCfg:
    combo: list[str] = field(default_factory=lambda: ["ctrl", "win"])
    double_tap_ms: int = 400
    tap_max_ms: int = 250
    cancel_key: str = "esc"
    suppress_start_menu: bool = True
    rehook_interval_s: int = 300
    slow_callback_ms: int = 50
    accept_injected: bool = False


@dataclass
class WakeCfg:
    """Palabra de activacion. Ver `wispi/wake.py` para el porque de cada numero.

    Desactivada por defecto A PROPOSITO: es la unica funcion de WISPI que analiza
    el microfono sin que el usuario haya pedido nada. Todo sigue siendo local,
    pero encenderla tiene que ser una decision, no una sorpresa.
    """

    enabled: bool = False
    # Varias frases porque `tiny` no oye "hey" de una sola manera. El emparejado
    # es difuso, asi que esta lista es de FORMAS, no de ortografias exactas.
    phrases: list[str] = field(default_factory=lambda: [
        "hey wispi", "ey wispi", "oye wispi", "hola wispi",
    ])
    name: str = "wispi"       # se comprueba aparte y con mas dureza que la frase
    threshold: float = 0.75   # parecido minimo de la frase entera
    name_threshold: float = 0.70  # ...y del nombre solo. Es el que tumba "hey wifi"
    max_tokens: int = 5       # un enunciado mas largo no es una palabra de activacion

    # Modelo del detector, independiente del de dictado. `tiny` = 0,21 s MEDIDOS.
    model: str = "tiny"
    cpu_threads: int = 2      # NO los 10 del ASR: esto corre mientras no dictas
    fallback_chain: list[dict] = field(default_factory=lambda: [
        {"model": "base"}, {"model": "small"},
    ])
    # Vacio por defecto. Sesgar el decoder hacia "WISPI" mejora los aciertos pero
    # tambien hace que lo escupa sobre audio dudoso: mas falsos positivos.
    initial_prompt: str = ""

    # -- segmentacion: que se considera un candidato --------------------------
    rms_threshold: float | None = None   # None = usar audio.silence_threshold
    min_speech_s: float = 0.25
    max_speech_s: float = 2.0
    end_silence_s: float = 0.35
    preroll_ms: int = 200
    cooldown_s: float = 2.0

    # -- que pasa al despertar -------------------------------------------------
    # Sin pre-roll: el ring de 300 ms contiene el final de "hey WISPI" y acabaria
    # dictado. Criterio C11.3.
    include_preroll: bool = False
    # Mas gracia que un dictado normal: entre que suena el tono y el usuario
    # empieza a hablar pasa mas tiempo que cuando el dedo esta en la tecla.
    start_grace_s: float = 1.5


@dataclass
class ASRCfg:
    backend: str = "faster-whisper"
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    cpu_threads: int = 10
    beam_size: int = 1
    language: str = "es"
    without_timestamps: bool = True
    condition_on_previous_text: bool = False
    temperature: float = 0.0
    vad_filter: bool = True
    no_speech_threshold: float = 0.6
    local_files_only: bool = True
    download_root: str | None = None
    fallback_chain: list[dict] = field(default_factory=list)


@dataclass
class PostprocessCfg:
    enabled: bool = True
    strip_fillers: bool = True
    fix_spacing: bool = True
    capitalize: bool = True
    apply_dictionary: bool = True
    use_initial_prompt: bool = True
    initial_prompt_style: str = "Transcripcion tecnica en espanol con terminos en ingles."


@dataclass
class LLMCfg:
    enabled: bool = True
    provider: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = "llama3.1:8b"
    min_words: int = 25
    timeout_s: float = 3.0
    temperature: float = 0.0
    keep_alive: int = -1
    max_len_delta: float = 0.4


@dataclass
class InjectionCfg:
    route: str = "auto"
    restore_clipboard: bool = True
    restore_delay_ms: int = 400
    settle_ms: int = 25
    modifier_release_timeout_ms: int = 200
    never_send_enter: bool = True
    unicode_chunk: int = 20
    terminal_apps: list[str] = field(
        default_factory=lambda: [
            "windowsterminal.exe", "cmd.exe", "powershell.exe",
            "pwsh.exe", "conhost.exe", "wsl.exe",
        ]
    )
    no_patch_apps: list[str] = field(
        default_factory=lambda: [
            "windowsterminal.exe", "cmd.exe", "powershell.exe",
            "pwsh.exe", "conhost.exe", "wsl.exe",
        ]
    )
    patch_max_wait_ms: int = 1500
    terminal_llm_wait_ms: int = 700


@dataclass
class LoggingCfg:
    level: str = "INFO"
    include_text: bool = False
    latency_jsonl: bool = True
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 3


@dataclass
class UICfg:
    chime_enabled: bool = True
    chime_volume: float = 0.15
    tray_enabled: bool = True
    # Boton flotante siempre encima. -1 en la posicion = "aun sin colocar", se
    # centra abajo a la derecha la primera vez.
    overlay_enabled: bool = True
    overlay_x: int = -1
    overlay_y: int = -1
    # 72 px, no 58: Windows recomienda ~9 mm de blanco tactil minimo y con 58 el
    # dedo falla. Sigue siendo discreto con raton y se ajusta en Configuracion.
    overlay_size: int = 72
    overlay_opacity: float = 0.92
    # Doble toque abre el panel tactil.
    #
    # El PRIMER toque dispara el dictado al instante y, si llega un segundo dentro
    # de esta ventana, se cancela y se abre el panel. Lo contrario -esperar la
    # ventana antes de actuar- meteria este retardo en CADA dictado, que es la
    # accion que se hace cincuenta veces al dia. Cancelar es gratis: el audio de
    # 300 ms cae por debajo de min_duration_s y no se inserta nada.
    double_tap_ms: int = 350
    # Umbral para distinguir toque de arrastre. Mas alto que para raton porque el
    # dedo tiembla: con 4 px, un toque en tactil se registraba como arrastre y el
    # dictado no arrancaba.
    drag_threshold_px: int = 10
    # El panel se cierra solo si nadie lo toca. 0 = no cerrar.
    panel_autoclose_ms: int = 8000


@dataclass
class Config:
    audio: AudioCfg = field(default_factory=AudioCfg)
    hotkey: HotkeyCfg = field(default_factory=HotkeyCfg)
    wake: WakeCfg = field(default_factory=WakeCfg)
    asr: ASRCfg = field(default_factory=ASRCfg)
    postprocess: PostprocessCfg = field(default_factory=PostprocessCfg)
    llm: LLMCfg = field(default_factory=LLMCfg)
    injection: InjectionCfg = field(default_factory=InjectionCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)
    ui: UICfg = field(default_factory=UICfg)

    path: Path = CONFIG_PATH
    local_path: Path = LOCAL_PATH
    _mtime: float = 0.0
    _local_mtime: float = 0.0

    # -- carga -------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        p = Path(path) if path else CONFIG_PATH
        cfg = cls(path=p)
        cfg._apply_all()
        return cfg

    def _apply_all(self) -> list[str]:
        """defaults -> config.yaml -> config.local.yaml (el ultimo gana)."""
        warns = self._apply_file(self.path, "config.yaml")
        warns += self._apply_file(self.local_path, "config.local.yaml")
        try:
            self._local_mtime = self.local_path.stat().st_mtime
        except OSError:
            self._local_mtime = 0.0
        return warns

    def _apply_file(self, path: Path | None = None, etiqueta: str = "") -> list[str]:
        """Aplica un YAML encima de lo que ya hay. Devuelve avisos."""
        path = path or self.path
        etiqueta = etiqueta or path.name
        warnings: list[str] = []
        if not path.exists():
            return warnings
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as e:  # YAML roto: no tumbar la app, seguir con lo anterior
            return [f"{etiqueta} ilegible ({e}); se mantienen los valores previos"]
        if not isinstance(raw, dict):
            return [f"{etiqueta} no es un mapa; ignorado"]

        for f in fields(self):
            if f.name.startswith("_") or f.name in ("path", "local_path"):
                continue
            section = raw.get(f.name)
            if section is None:
                continue
            current = getattr(self, f.name)
            if not is_dataclass(current) or not isinstance(section, dict):
                continue
            valid = {sf.name for sf in fields(current)}
            for k, v in section.items():
                if k in valid:
                    setattr(current, k, v)
                else:
                    warnings.append(f"{etiqueta}: {f.name}.{k} no existe; ignorada")
        if path == self.path:
            try:
                self._mtime = path.stat().st_mtime
            except OSError:
                pass
        return warnings

    # -- hot reload --------------------------------------------------------
    def maybe_reload(self) -> tuple[bool, list[str]]:
        """Recarga si cambio ALGUNO de los dos ficheros. Solo desde el hilo de estado."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = self._mtime
        try:
            lmtime = self.local_path.stat().st_mtime
        except OSError:
            lmtime = 0.0
        if mtime == self._mtime and lmtime == self._local_mtime:
            return False, []
        # Volver a los defaults antes de aplicar, para que borrar una clave del
        # YAML tambien surta efecto (si no, quedaria pegado el valor viejo).
        for f in fields(self):
            if f.name.startswith("_") or f.name in ("path", "local_path"):
                continue
            if f.default_factory is not None:  # type: ignore[misc]
                setattr(self, f.name, f.default_factory())  # type: ignore[misc]
        return True, self._apply_all()

    # -- escritura de overrides -------------------------------------------
    def read_overrides(self) -> dict[str, Any]:
        """Contenido actual de config.local.yaml (lo que la interfaz ha cambiado)."""
        if not self.local_path.exists():
            return {}
        try:
            raw = yaml.safe_load(self.local_path.read_text(encoding="utf-8")) or {}
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def write_overrides(self, cambios: dict[str, dict[str, Any]]) -> None:
        """Fusiona `cambios` en config.local.yaml y lo reescribe.

        Escritura atomica (tmp + replace): el hilo de estado esta haciendo poll de
        mtime cada segundo y no puede llegar a leer un fichero a medio escribir.
        """
        actual = self.read_overrides()
        for seccion, valores in cambios.items():
            actual.setdefault(seccion, {})
            actual[seccion].update(valores)
        cabecera = (
            "# Overrides de WISPI escritos por la interfaz de configuracion.\n"
            "#\n"
            "# Se aplica ENCIMA de config.yaml, asi que lo que haya aqui GANA. Si tocas\n"
            "# a mano una clave en config.yaml y no ves el efecto, mira si esta aqui.\n"
            "# Borrar una clave de este fichero devuelve el control a config.yaml.\n"
            "#\n"
            "# config.yaml es la referencia documentada y se edita a mano; este fichero\n"
            "# lo genera la interfaz y no lleva comentarios a proposito.\n\n"
        )
        tmp = self.local_path.with_suffix(".yaml.tmp")
        tmp.write_text(cabecera + yaml.safe_dump(actual, allow_unicode=True,
                                                 sort_keys=True, default_flow_style=False),
                       encoding="utf-8")
        os.replace(tmp, self.local_path)
        try:
            self._local_mtime = self.local_path.stat().st_mtime
        except OSError:
            pass

    def clear_override(self, seccion: str, clave: str) -> bool:
        """Quita un override concreto. True si existia."""
        actual = self.read_overrides()
        if seccion not in actual or clave not in actual[seccion]:
            return False
        del actual[seccion][clave]
        if not actual[seccion]:
            del actual[seccion]
        tmp = self.local_path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(actual, allow_unicode=True, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, self.local_path)
        return True

    def snapshot(self) -> "Config":
        """Copia profunda, para leer config estable durante un dictado en vuelo."""
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name.startswith("_") or f.name in ("path", "local_path"):
                continue
            v = getattr(self, f.name)
            out[f.name] = {sf.name: getattr(v, sf.name) for sf in fields(v)} if is_dataclass(v) else v
        return out
