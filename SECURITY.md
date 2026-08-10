# Seguridad y modelo de amenaza

WISPI instala un **hook de teclado global**, lee y escribe el **portapapeles** e
**inyecta pulsaciones** en la ventana que tengas delante. Técnicamente es la misma
forma que un keylogger. Este documento existe para que no tengas que confiar en
nuestra palabra: dice exactamente qué hace, qué no hace, y en qué línea del código
comprobarlo.

Si no te convence, no lo instales. Es la respuesta correcta.

---

## Lo que WISPI hace

| Capacidad | Por qué la necesita | Dónde está |
|---|---|---|
| `WH_KEYBOARD_LL` (hook de teclado de bajo nivel) | Detectar que mantienes `Ctrl+Win` sin robarle las teclas a nadie | [`wispi/hotkey.py`](wispi/hotkey.py) |
| Micrófono siempre abierto | El pre-roll de 300 ms: sin él se pierde la primera sílaba | [`wispi/audio.py`](wispi/audio.py) |
| Escribir y restaurar el portapapeles | La ruta de inyección más fiable en Windows es `Ctrl+V` | [`wispi/inject/clipboard.py`](wispi/inject/clipboard.py) |
| `SendInput` | Escribir el texto en la app en foco | [`wispi/inject/sendinput.py`](wispi/inject/sendinput.py) |
| Leer el nombre del proceso en foco | Elegir ruta de pegado y no parchear en terminales | [`wispi/inject/target.py`](wispi/inject/target.py) |

## Lo que WISPI no hace

Cada punto es verificable, no una promesa:

- **No registra las teclas que pulsas.** El hook solo mira si el `vkCode` pertenece al
  combo configurado; cualquier otra tecla se descarta sin registrarse.
  Ver [`wispi/hotkey.py`](wispi/hotkey.py) y la regla al principio de
  [`wispi/logging_setup.py`](wispi/logging_setup.py).
- **No guarda lo que dictas.** Con `logging.include_text: false` (el default), ni
  `logs/wispi.log` ni `logs/latency.jsonl` contienen el texto transcrito: solo tiempos
  y longitudes. Compruébalo con un `grep` sobre tus logs.
- **No guarda el audio.** Los buffers viven en memoria y se descartan tras transcribir.
  No hay escritura de WAV en el camino del dictado.
- **No manda nada a internet.** La única conexión saliente es a `llm.base_url`
  (por defecto `http://127.0.0.1:11434`, tu Ollama local). No hay telemetría, ni
  analítica, ni claves de API, ni cuentas.
- **No se actualiza solo.** No hay autoupdate ni proceso que descargue código.

**La única excepción de red** es la primera descarga de los modelos de Whisper desde
Hugging Face, que ocurre solo si pones `asr.local_files_only: false`. Con `true`
(el default) WISPI no toca la red ni aunque quiera.

## El modo elevado: lo que aceptas

`scripts/install_autostart.ps1 -Elevated` registra WISPI como administrador.

**Qué ganas:** el hook también funciona cuando tienes en primer plano una ventana
elevada (Administrador de tareas, una terminal de admin). Sin elevar, UIPI bloquea el
input de procesos bajos hacia ventanas altas y WISPI parece "no funcionar" ahí.

**Qué aceptas:** un hook de teclado global corriendo como administrador ve *todas* las
teclas del sistema, incluidas las de las ventanas elevadas. La única contramedida real
es que el código no escriba lo que ve. Si activas la elevación, esa promesa —y tu
lectura del código— es lo único que te protege.

**El default es sin elevar, a propósito.** Actívalo solo si de verdad dictas dentro de
ventanas de administrador.

## Tu antivirus lo va a mirar raro

Un hook de teclado global + `SendInput` + acceso al portapapeles es exactamente la
firma de comportamiento que buscan los EDR. Es normal que Defender o tu antivirus
pidan confirmación o pongan el proceso en cuarentena.

No pidas una excepción a ciegas: lee antes [`wispi/hotkey.py`](wispi/hotkey.py) y
[`wispi/inject/`](wispi/inject/), que es donde está todo lo que justifica la alerta.

## Superficie que sí deberías vigilar si modificas el código

- **`logging.include_text: true`** escribe tus dictados en disco en claro. Es útil para
  depurar el diccionario y es una fuga de privacidad si se te olvida quitarlo.
- **`hotkey.accept_injected: true`** hace que WISPI acepte input sintético. Está para los
  tests; en uso normal permite que otro programa dispare dictados.
- **`injection.restore_clipboard: false`** deja tu texto dictado en el portapapeles.
- Cualquier cambio en `inject/` puede acabar escribiendo en una ventana que no era.
  Las cuatro compuertas del parche optimista existen por eso; ver el README.

## Reportar una vulnerabilidad

**No abras un issue público** para un fallo de seguridad.

Usa el reporte privado de GitHub: pestaña **Security → Report a vulnerability** del
repositorio. Si esa opción no está disponible, abre un issue pidiendo un canal privado,
sin detalles del fallo.

Incluye: versión (commit), Windows, pasos para reproducir e impacto. Se responde en la
medida en que se pueda — esto es un proyecto personal publicado por si le sirve a
alguien, sin equipo detrás ni SLA.

## Versiones soportadas

Solo la rama `main`. No hay backports ni ramas de mantenimiento.
