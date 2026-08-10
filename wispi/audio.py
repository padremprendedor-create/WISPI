"""Captura de microfono con ring de pre-roll.

POR QUE ESTE MODULO EXISTE TAL COMO ESTA:

1. El InputStream se abre UNA vez y no se cierra hasta salir. Abrirlo en cada
   key-down cuesta 100-300 ms en MME y eso se traduce en comerse la primera
   silaba del usuario. El stream vive siempre; lo que cambia es a donde van los
   bloques.

2. Mientras nadie graba, el callback empuja a un ring acotado
   (`deque(maxlen=preroll_blocks)`, 10 bloques = 300 ms). Al empezar a grabar
   ese ring se vuelca DELANTE del buffer, asi que el audio entregado al ASR
   empieza ~300 ms ANTES del key-down. Es lo que compra el "no me corta el
   principio".

3. Al soltar la tecla se esperan `tail_ms` (200 ms) antes de cerrar el buffer,
   para no cortar la ultima silaba. Son 200 ms DIRECTOS del presupuesto de
   latencia (KPI ttt_ms = t9_inject_out - t2_keyup): medibles y ajustables
   desde config.yaml. Si algun dia sobra calidad y falta velocidad, este es el
   primer numero que se toca.

4. Toda la conversion de formato vive aqui. El contrato con el ASR
   (`wispi/asr/base.py`) es float32 mono 16 kHz en [-1, 1] contiguo, y ningun
   backend resamplea nunca. Si el dispositivo no acepta 16 kHz mono se abre al
   rate/canales nativos y se convierte AQUI, en vez de fallar.

REGLA DEL CALLBACK: corre en un hilo de PortAudio, no de Python. Ahi dentro no
hay locks, ni logging (hace I/O de fichero y puede bloquear), ni excepciones que
escapen (sounddevice aborta el stream si el callback lanza). Solo `deque.append`,
`list.append` y asignaciones a atributos, que son atomicos bajo el GIL.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any

import numpy as np
import sounddevice as sd

from .config import AudioCfg
from .logging_setup import get as _default_logger

# Rate de salida fijado por el contrato del ASR. No es configurable por capricho:
# whisper trabaja a 16 kHz y `cfg.sample_rate` esta ahi para el caso raro de un
# backend futuro que pida otra cosa.
_TARGET_DTYPE = np.float32


class AudioCapture:
    """Microfono siempre abierto + ring de pre-roll + grabacion bajo demanda.

    Ciclo de vida:
        start()             -> abre el stream permanente (idempotente, reintentable)
        begin_recording()   -> vuelca el ring y empieza a acumular
        end_recording()     -> espera tail_ms y devuelve float32 mono 16 kHz
        cancel_recording()  -> tira lo acumulado
        stop()              -> cierra el stream

    Los contadores del gate (`peak_rms`, `recorded_s()`) se resetean en
    `begin_recording()`, NO en `end_recording()`: app.py necesita leerlos
    justo DESPUES de terminar para decidir si descarta el dictado sin llamar
    al ASR (criterio C6.2 del SPEC).
    """

    def __init__(self, cfg: AudioCfg, log: Any = None) -> None:
        self.cfg = cfg
        self.log = log if log is not None else _default_logger("audio")

        # -- stream ---------------------------------------------------------
        self._stream: sd.InputStream | None = None
        self._device_index: int | None = None
        self._device_dict: dict = {}
        self._rate: int = int(cfg.sample_rate)      # rate REAL del stream abierto
        self._channels: int = 1                     # canales REALES del stream
        self._blocksize: int = int(cfg.blocksize)
        self._latency_mode: str = "low"
        self._stream_ok: bool = False
        self._closing: bool = False
        self._last_error: str | None = None

        # -- buffers ---------------------------------------------------------
        # El ring solo se alimenta cuando NO se graba; asi un bloque nunca acaba
        # duplicado (una vez en el ring y otra en el buffer de grabacion).
        self._ring: deque[tuple[np.ndarray, float]] = deque(maxlen=cfg.preroll_blocks)
        self._frames: list[np.ndarray] = []

        # -- estado de grabacion ---------------------------------------------
        # `_rec_frames` y `_silence_frames` los escribe SOLO el callback.
        # `_preroll_frames` lo escribe SOLO begin_recording(). Un unico escritor
        # por variable = no hace falta lock.
        self._recording: bool = False
        self._rec_frames: int = 0
        self._preroll_frames: int = 0
        self._silence_frames: int = 0
        self._peak_rms: float = 0.0
        self._current_rms: float = 0.0
        self._truncated: bool = False
        # Gracia efectiva de ESTA grabacion. Normalmente `cfg.start_grace_s`, pero
        # la palabra de activacion pide mas: entre el tono y la primera palabra
        # pasa mas tiempo que cuando ya tienes el dedo en la tecla.
        self._grace_s: float = float(cfg.start_grace_s)

        # -- derivacion para el detector de palabra de activacion --------------
        # Un unico callable, o None. El callback de PortAudio le pasa cada bloque
        # que NO va a un dictado. Lo pone `app.py`; `audio.py` no sabe que hay al
        # otro lado ni le importa.
        self._wake_sink = None

        # -- diagnostico ------------------------------------------------------
        self._overflows: int = 0
        self._callback_errors: int = 0

    # ------------------------------------------------------------------ ciclo
    def start(self) -> None:
        """Abre el stream permanente. Idempotente y reintentable.

        Si el stream anterior murio (micro USB desconectado en caliente), lo
        cierra y abre uno nuevo. Esto es lo que permite recuperarse sin
        reiniciar la app: app.py puede volver a llamar a start() cuando ve
        `last_error`.
        """
        if self._stream is not None and self._stream_ok:
            try:
                if self._stream.active:
                    return
            except Exception:
                pass  # stream zombie: se cierra abajo y se reabre
        self._close_stream()

        try:
            self._device_index, self._device_dict = self._resolve_device()
            self._rate, self._channels = self._negotiate_format()
        except Exception as e:
            self._last_error = f"no hay dispositivo de entrada usable: {e}"
            self.log.error("audio: %s", self._last_error)
            raise

        # blocksize en MUESTRAS del rate real, no del rate objetivo: block_ms es
        # lo que se mantiene constante (30 ms), no el numero de muestras.
        self._blocksize = max(1, round(self._rate * self.cfg.block_ms / 1000))

        stream = None
        for latency in ("low", None):
            kwargs: dict = dict(
                device=self._device_index,
                samplerate=self._rate,
                channels=self._channels,
                dtype="float32",
                blocksize=self._blocksize,
                callback=self._callback,
                finished_callback=self._on_finished,
            )
            if latency is not None:
                kwargs["latency"] = latency
            try:
                stream = sd.InputStream(**kwargs)
                stream.start()
                self._latency_mode = latency or "default"
                break
            except Exception as e:
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                    stream = None
                if latency is None:
                    self._last_error = f"no se pudo abrir el microfono: {e}"
                    self.log.error("audio: %s", self._last_error)
                    raise RuntimeError(self._last_error) from e
                # 'low' no siempre existe en MME: se reintenta con la latencia
                # por defecto antes de darse por vencido.
                self.log.warning("audio: latency='low' rechazada (%s); reintento con default", e)

        self._stream = stream
        self._stream_ok = True
        self._closing = False
        self._last_error = None
        self._ring.clear()
        self.log.info(
            "audio: stream abierto dev=%s '%s' rate=%d ch=%d block=%d latency=%s%s",
            self._device_index, self._device_dict.get("name", "?"), self._rate,
            self._channels, self._blocksize, self._latency_mode,
            "" if self._rate == self.cfg.sample_rate else f" (resampleo -> {self.cfg.sample_rate})",
        )

    def stop(self) -> None:
        """Cierra el stream. Seguro de llamar dos veces."""
        self._recording = False
        self._close_stream()
        self._ring.clear()
        self._frames = []

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        self._stream_ok = False
        if stream is None:
            return
        self._closing = True
        try:
            stream.stop()
        except Exception as e:
            self.log.debug("audio: stop() del stream fallo: %s", e)
        try:
            stream.close()
        except Exception as e:
            self.log.debug("audio: close() del stream fallo: %s", e)

    # -------------------------------------------------------------- grabacion
    def begin_recording(self, include_preroll: bool = True,
                        grace_s: float | None = None) -> None:
        """Vuelca el ring de pre-roll y empieza a acumular.

        Orden deliberado: primero se arma el buffer vacio y se levanta la
        bandera (el callback deja de alimentar el ring y empieza a alimentar
        `_frames`), y SOLO despues se drena el ring y se antepone. Al reves
        habria una ventana en la que el audio no va a ningun sitio.

        `include_preroll=False` tira el ring en vez de anteponerlo. Lo usa la
        palabra de activacion: ahi el pre-roll contiene el final de "hey WISPI",
        y anteponerlo seria escribir la propia frase de activacion (C11.3).
        """
        if self._recording:
            return
        if not self._stream_ok:
            # No se levanta excepcion: app.py debe poder seguir viva con el
            # micro caido. El gate de duracion descartara el dictado vacio.
            self.log.warning("audio: begin_recording sin stream vivo (%s)", self._last_error)

        self._frames = []
        self._rec_frames = 0
        self._preroll_frames = 0
        self._silence_frames = 0
        self._peak_rms = 0.0
        self._current_rms = 0.0
        self._truncated = False
        self._grace_s = float(self.cfg.start_grace_s if grace_s is None else grace_s)
        self._recording = True

        if not include_preroll:
            self._ring.clear()
            return

        # Drenar con popleft() y no con list(deque): iterar un deque acotado
        # mientras otro hilo hace append puede lanzar RuntimeError. popleft()
        # es atomico y ademas deja el ring vacio, que es justo lo que se quiere.
        preroll: list[np.ndarray] = []
        peak = 0.0
        frames = 0
        while True:
            try:
                block, rms = self._ring.popleft()
            except IndexError:
                break
            preroll.append(block)
            frames += block.shape[0]
            if rms > peak:
                peak = rms
        # Descarta el bloque que hubiera podido colarse en el ring en la ventana
        # de nanosegundos entre levantar la bandera y drenar: preferible perder
        # 30 ms que entregar audio desordenado.
        self._ring.clear()

        if preroll:
            # Asignacion de slice: una sola llamada C, atomica bajo el GIL
            # frente al append del callback, y conserva el orden cronologico.
            self._frames[0:0] = preroll
            self._preroll_frames = frames
            if peak > self._peak_rms:
                self._peak_rms = peak

    def end_recording(self) -> np.ndarray:
        """Espera `tail_ms`, cierra el buffer y devuelve float32 mono 16 kHz.

        Los `tail_ms` son tiempo de espera puro que entra en el KPI de latencia;
        estan aqui y no en app.py para que el buffer siga creciendo durante la
        espera (si se parase antes, la cola no serviria de nada).
        """
        if not self._recording:
            return np.zeros(0, dtype=_TARGET_DTYPE)

        tail_s = max(0.0, self.cfg.tail_ms / 1000.0)
        if tail_s:
            time.sleep(tail_s)

        self._recording = False
        frames, self._frames = self._frames, []

        if self._overflows:
            self.log.warning("audio: %d overflows de PortAudio durante la sesion", self._overflows)
        if self._truncated:
            self.log.warning("audio: grabacion recortada a max_duration_s=%.1f",
                             self.cfg.max_duration_s)
        if self._callback_errors:
            self.log.warning("audio: %d errores en el callback (ultimo: %s)",
                             self._callback_errors, self._last_error)

        if not frames:
            return np.zeros(0, dtype=_TARGET_DTYPE)

        try:
            raw = np.concatenate(frames, axis=0)
        except ValueError as e:  # bloques de forma incompatible: no tumbar la app
            self._last_error = f"buffer inconsistente: {e}"
            self.log.error("audio: %s", self._last_error)
            return np.zeros(0, dtype=_TARGET_DTYPE)

        out = self._to_target(raw)
        return out

    def cancel_recording(self) -> None:
        """Descarta lo acumulado. Cero muestras salen de aqui (criterio C3.6)."""
        self._recording = False
        self._frames = []
        self._rec_frames = 0
        self._preroll_frames = 0
        self._silence_frames = 0
        self._peak_rms = 0.0
        self._current_rms = 0.0
        self._truncated = False

    # ------------------------------------------------------- palabra de activacion
    def set_wake_sink(self, sink) -> None:
        """Deriva al detector cada bloque que NO va a un dictado. None lo apaga.

        Es una asignacion de atributo (atomica bajo el GIL), asi que se puede
        encender y apagar en caliente sin parar el stream.
        """
        self._wake_sink = sink

    def to_target(self, raw: np.ndarray) -> np.ndarray:
        """float32 mono contiguo a `cfg.sample_rate`. Publico para el detector.

        El detector acumula bloques al rate REAL del stream (que puede ser 48 kHz
        si el dispositivo rechaza 16) y convierte el enunciado ENTERO de una vez.
        Convertir bloque a bloque metria un artefacto en cada juntura.
        """
        return self._to_target(raw)

    @property
    def stream_rate(self) -> int:
        """Rate REAL del stream abierto, no el del contrato del ASR."""
        return self._rate

    # -------------------------------------------------------------- consultas
    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def peak_rms(self) -> float:
        """RMS pico de la grabacion (incluido el pre-roll).

        Es la mitad del gate anti-alucinacion: por debajo de
        `cfg.silence_threshold` (0.012) el dictado se tira SIN llamar al ASR.
        Sin esto, whisper large-v3 escribe " Gracias." sobre ruido a -54 dB.
        """
        return self._peak_rms

    @property
    def current_rms(self) -> float:
        """RMS del ultimo bloque. Para feedback visual, no para decisiones."""
        return self._current_rms

    def silence_elapsed_s(self) -> float:
        """Segundos de silencio CONTINUO bajo `cfg.silence_threshold`.

        app.py lo consulta en modo manos libres para cortar tras
        `cfg.silence_duration_s`. Durante `cfg.start_grace_s` devuelve 0: si no,
        el silencio previo a la primera palabra cortaria el dictado antes de
        empezar.
        """
        if not self._recording or self._rate <= 0:
            return 0.0
        return self._silence_frames / float(self._rate)

    def recorded_s(self) -> float:
        """Segundos capturados, pre-roll incluido. Valido tambien tras end_recording()."""
        if self._rate <= 0:
            return 0.0
        return (self._preroll_frames + self._rec_frames) / float(self._rate)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def stream_ok(self) -> bool:
        """False si el stream murio (micro desconectado). start() lo reintenta."""
        return self._stream_ok

    @property
    def overflow_count(self) -> int:
        return self._overflows

    def device_info(self) -> dict:
        active = False
        if self._stream is not None:
            try:
                active = bool(self._stream.active)
            except Exception:
                active = False
        d = self._device_dict or {}
        hostapi = ""
        try:
            if "hostapi" in d:
                hostapi = sd.query_hostapis(d["hostapi"])["name"]
        except Exception:
            pass
        return {
            "index": self._device_index,
            "name": d.get("name", ""),
            "hostapi": hostapi,
            "max_input_channels": d.get("max_input_channels"),
            "default_samplerate": d.get("default_samplerate"),
            "rate_open": self._rate,
            "rate_out": int(self.cfg.sample_rate),
            "channels_open": self._channels,
            "blocksize": self._blocksize,
            "latency": self._latency_mode,
            "resampling": self._rate != int(self.cfg.sample_rate),
            "downmixing": self._channels > 1,
            "active": active,
            "stream_ok": self._stream_ok,
            "overflows": self._overflows,
            "callback_errors": self._callback_errors,
            "last_error": self._last_error,
        }

    # ------------------------------------------------------ callback (PortAudio)
    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        """Hilo de PortAudio. Sin locks, sin logging, sin excepciones que salgan."""
        try:
            if status:
                # `input_overflow` significa que el sistema no vino a por los
                # datos a tiempo: se cuenta y se sigue. Levantar aqui mataria
                # el stream por un hipo del planificador.
                self._overflows += 1

            # indata se REUTILIZA entre llamadas: sin copy() se corrompe el
            # audio ya guardado. El downmix a mono ya produce un array nuevo.
            if self._channels > 1:
                block = np.mean(indata, axis=1, dtype=np.float32)
            else:
                block = indata[:, 0].copy()

            # RMS del bloque: barato y suficiente como detector de voz.
            rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2, dtype=np.float32)))
            self._current_rms = rms

            if not self._recording:
                self._ring.append((block, rms))
                # La palabra de activacion, si esta encendida. Un `deque.append`
                # al otro lado y nada mas: la regla del callback (ni locks, ni
                # logging, ni excepciones que escapen) tambien aplica ahi.
                sink = self._wake_sink
                if sink is not None:
                    sink(block, rms)
                return

            n = block.shape[0]
            total = self._preroll_frames + self._rec_frames
            max_frames = int(self.cfg.max_duration_s * self._rate)
            if max_frames > 0 and total >= max_frames:
                # Tope duro: se deja de acumular para acotar la memoria aunque
                # app.py se olvide de cortar. No se corta el stream.
                self._truncated = True
                return

            self._frames.append(block)
            self._rec_frames += n

            if rms > self._peak_rms:
                self._peak_rms = rms

            # Silencio continuo para manos libres. Durante la gracia inicial se
            # mantiene a cero: el usuario aun no ha empezado a hablar.
            #
            # _rec_frames, NO total. `total` incluye el pre-roll, que es audio de
            # ANTES de pulsar la tecla, y la gracia tiene que contarse desde el
            # key-down. Con los valores reales (preroll 300 ms, gracia 300 ms) el
            # ring aporta 4800 frames y el umbral es exactamente 4800: la condicion
            # salia FALSA ya en el primer bloque y la gracia efectiva era CERO
            # (start_grace_s - preroll_ms/1000). Consecuencia: en manos libres el
            # corte por silencio caia a 1.2 s del key-down en vez de a 1.5 s.
            # Lo detecto la verificacion adversarial del bloque audio; el
            # constructor lo habia medido con el ring VACIO, estado que la app real
            # solo alcanza en los primeros 300 ms tras start() y nunca en un dictado.
            #
            # _rec_frames ya viene incrementado con n en la linea de arriba.
            if self._rec_frames < int(self._grace_s * self._rate):
                self._silence_frames = 0
            elif rms < self.cfg.silence_threshold:
                self._silence_frames += n
            else:
                self._silence_frames = 0
        except Exception as e:  # jamas propagar: sounddevice abortaria el stream
            self._callback_errors += 1
            self._last_error = f"callback: {type(e).__name__}: {e}"

    def _on_finished(self) -> None:
        """PortAudio da el stream por terminado. Si no lo cerramos nosotros, murio."""
        if self._closing:
            return
        self._stream_ok = False
        if not self._last_error:
            self._last_error = "el stream de audio termino solo (microfono desconectado?)"

    # ------------------------------------------------------------- conversion
    def _resolve_device(self) -> tuple[int | None, dict]:
        """Traduce `cfg.input_device` (None | indice | nombre) a indice + info."""
        want = self.cfg.input_device
        if want is None:
            info = sd.query_devices(kind="input")
            idx = info.get("index", sd.default.device[0])
            return (int(idx) if idx is not None else None, dict(info))
        if isinstance(want, int):
            return int(want), dict(sd.query_devices(want))
        # nombre: coincidencia por subcadena, insensible a mayusculas
        needle = str(want).lower()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and needle in d["name"].lower():
                return i, dict(d)
        raise ValueError(f"no hay dispositivo de entrada que contenga '{want}'")

    def _negotiate_format(self) -> tuple[int, int]:
        """Elige (rate, canales) que el dispositivo ACEPTE, prefiriendo 16 kHz mono.

        WASAPI en este equipo rechaza 16 kHz (solo 48 kHz), asi que la ruta de
        rate nativo + resampleo local no es teorica: se usa en cuanto se fija
        `input_device: 18` en config.yaml. Fallar ahi seria peor que resamplear.
        """
        target = int(self.cfg.sample_rate)
        native = int(round(float(self._device_dict.get("default_samplerate") or target)))
        max_ch = int(self._device_dict.get("max_input_channels") or 1) or 1
        candidates: list[tuple[int, int]] = []
        for rate in (target, native):
            for ch in (1, max_ch):
                if (rate, ch) not in candidates:
                    candidates.append((rate, ch))
        errors = []
        for rate, ch in candidates:
            try:
                sd.check_input_settings(device=self._device_index, channels=ch,
                                        samplerate=rate, dtype="float32")
                return rate, ch
            except Exception as e:
                errors.append(f"{rate}Hz/{ch}ch: {e}")
        raise RuntimeError("ningun formato aceptado (" + "; ".join(errors) + ")")

    def _to_target(self, raw: np.ndarray) -> np.ndarray:
        """float32 mono contiguo a `cfg.sample_rate`, en [-1, 1]. Contrato duro."""
        x = np.asarray(raw, dtype=_TARGET_DTYPE)
        if x.ndim > 1:
            x = x.reshape(-1)
        target = int(self.cfg.sample_rate)
        if self._rate != target:
            x = self._resample(x, self._rate, target)
        # Algun driver entrega picos justo por encima de 1.0; el validador del
        # ASR rechaza |x| > 1.5 y no queremos perder un dictado por eso.
        np.clip(x, -1.0, 1.0, out=x)
        return np.ascontiguousarray(x, dtype=_TARGET_DTYPE)

    @staticmethod
    def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
        """Reduccion de rate por interpolacion lineal, con prefiltro de media movil.

        Interpolar sin filtrar pliega todo lo que hay por encima de 8 kHz sobre
        la banda de voz. La media movil de `src/dst` muestras es un pasa-bajos
        pobre pero O(n) y elimina lo peor del alias; para voz es de sobra y
        cuesta ~20 ms en un dictado de 60 s. Solo se recorre esta ruta cuando el
        dispositivo no acepta 16 kHz.
        """
        if src == dst or x.size == 0:
            return x
        ratio = src / float(dst)
        w = int(ratio)
        if w >= 2:
            kernel = np.ones(w, dtype=np.float32) / np.float32(w)
            x = np.convolve(x, kernel, mode="same").astype(_TARGET_DTYPE)
        n_out = int(round(x.size * dst / float(src)))
        if n_out <= 0:
            return np.zeros(0, dtype=_TARGET_DTYPE)
        pos = np.arange(n_out, dtype=np.float64) * ratio
        return np.interp(pos, np.arange(x.size, dtype=np.float64), x).astype(_TARGET_DTYPE)
