# WISPI

**Dictado por voz local, offline y privado para Windows.** Mantienes `Ctrl+Win`, hablas,
sueltas, y el texto aparece donde tengas el cursor. Sin red, sin cuota, sin suscripción,
sin que tu audio salga de la máquina.

[![CI](https://github.com/padremprendedor-create/WISPI/actions/workflows/ci.yml/badge.svg)](https://github.com/padremprendedor-create/WISPI/actions/workflows/ci.yml) ![licencia MIT](https://img.shields.io/badge/licencia-MIT-blue) ![Windows 10/11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6) ![Python 3.11–3.12](https://img.shields.io/badge/Python-3.11–3.12-3776AB) ![100% offline](https://img.shields.io/badge/red-0%20llamadas-success)

*[Read this in English →](README.en.md)*

```bash
uv run python -m wispi --console
```

| | |
|---|---|
| [Por qué existe](#por-qué-existe) · [Cómo funciona](#cómo-funciona) | de dónde sale |
| [Requisitos](#requisitos-del-sistema) · [Instalación](#instalación-desde-cero) · [Primer arranque](#primer-arranque) | ponerlo a andar |
| [Uso](#uso) · [Configuración](#configuración) · [Tu jerga](#tu-jerga-es-la-mitad-del-valor) | usarlo |
| [Rendimiento](#la-medición-que-decidió-el-modelo) · [Limitaciones](#limitaciones-conocidas) · [Problemas](#problemas-frecuentes) | la letra pequeña |

---

## Por qué existe

WISPI nació para reemplazar a [Wispr Flow](https://wisprflow.ai), que es muy bueno y
falla en tres puntos concretos:

| Fallo | Evidencia |
|---|---|
| Todo el audio va a la nube, sin modo offline | *"Transcription always occurs on the cloud"* — [wisprflow.ai/data-controls](https://wisprflow.ai/data-controls) |
| Se rompe al dictar a Claude Code en Windows | [anthropics/claude-code#38620](https://github.com/anthropics/claude-code/issues/38620) |
| No conoce tu jerga: destroza Supabase, Vercel, n8n, RLS, commit | Uso diario |

Más $15/mes de Pro, o el techo de 2.000 palabras/semana del plan gratis.

El objetivo real está escrito en [SPEC.md](SPEC.md) y no es "hacer una app de dictado":
es **que Wispr Flow se pueda desinstalar sin echarlo de menos**. Ese criterio decide los
empates.

Se publica por si le sirve a alguien más. Es una herramienta personal, no un producto:
no hay instalador, ni autoupdate, ni soporte. El código está entero y comentado, que es
lo que de verdad se comparte.

---

## Cómo funciona

```
Ctrl+Win ↓                                                    Ctrl+Win ↑
    │                                                              │
    │  hook WH_KEYBOARD_LL (callback < 0,5 ms)                     │
    ▼                                                              ▼
[ring de pre-roll 300 ms] ── graba ──▶ [+200 ms de cola] ──▶ gate RMS
                                                                │
                    descarta si rms < 0,012 o dur < 0,35 s ◀────┤
                                                                ▼
                                                    faster-whisper small int8
                                                       (+ initial_prompt)
                                                                │
                                       filtro de alucinaciones ◀┤
                                                                ▼
                                            nivel 0: muletillas + diccionario
                                                                │
                            ┌───────── ¿> 25 palabras? ─────────┤
                            │ sí                             no │
                            ▼                                   ▼
              inserta el CRUDO ya  ──────────────────▶  inserta y termina
                            │
                    Ollama en paralelo
                            │
                  4 compuertas ── falla una ──▶ se queda el crudo
                            ▼
                        parchea
```

**Inyección en cascada**, por app en foco: `Ctrl+V` por defecto · `Shift+Insert` en
terminales (ahí `Ctrl+V` no funciona) · `SendInput` Unicode como último recurso.

---

## Requisitos del sistema

### Imprescindible

| | Mínimo | Recomendado |
|---|---|---|
| **Sistema** | Windows 10 21H2 x64 | Windows 11 x64 |
| **CPU** | 4 núcleos físicos con AVX2 | 6+ núcleos físicos |
| **RAM libre** | 2 GB | 4 GB |
| **Disco** | ~750 MB | ~750 MB |
| **Micrófono** | cualquiera | headset con micro de diadema |
| **Python** | 3.11 | 3.11 o 3.12 (**no** 3.13) |

**Solo Windows, y no por pereza.** El hook de teclado, la inyección de texto y el
portapapeles son Win32 puro. No hay versión de macOS ni de Linux ni está previsto que
la haya.

**La CPU es lo que marca la latencia.** Todo el ASR corre en CPU con cuantización int8;
sin AVX2 el rendimiento cae mucho (cualquier CPU de 2015 en adelante lo tiene). Medido
en un i9-10850K de 10 núcleos: **p50 de 1,57 s por dictado**. Con menos núcleos sube;
`wispi.bench` te dice exactamente cuánto en tu máquina en vez de hacerte adivinar.

**Consumo medido** con el modelo `small` cargado: 372 MB de memoria en reposo, 398 MB
tras transcribir, **pico de 720 MB** durante la transcripción.

**Disco**: 258 MB del entorno virtual + 464 MB del modelo `small`. Otros modelos:
`tiny` 75 MB · `base` 141 MB · `large-v3` 2,9 GB.

### Opcional

- **Ollama** con `llama3.1:8b` (4,9 GB en disco, ~6 GB de RAM mientras corre) para la
  limpieza de nivel 1 en dictados largos. **Sin Ollama, WISPI funciona igual**: se queda
  en el nivel 0, que es reglas y diccionario y ya hace la mayor parte del trabajo.
- **GPU NVIDIA** para subir a `large-v3`. No hace falta y no se recomienda de entrada
  (ver [la escalera de migración](#la-escalera-de-migración)).
- **`piper-tts`** y una voz española, solo si vas a regenerar el corpus de pruebas.

### El idioma viene puesto en español

`asr.language: es`, el diccionario y las reglas de muletillas son de español. Para otro
idioma hay que cambiar `asr.language`, reescribir `dictionary.yaml` y repasar las reglas
de `wispi/postprocess/level0.py`. La arquitectura no lo impide; nadie lo ha hecho.

---

## Instalación desde cero

### 1. Instala `uv`

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

`uv` se encarga también de Python: no hace falta que tengas 3.11 instalado a mano.

### 2. Clona y prepara el entorno

```bash
git clone https://github.com/padremprendedor-create/WISPI.git
cd WISPI
uv sync
```

Esto crea `.venv` con las versiones exactas de `uv.lock` (~258 MB). Están clavadas a
propósito: este stack rompe con facilidad entre versiones menores.

### 3. Descarga el modelo de Whisper

WISPI viene con `local_files_only: true`, es decir, **no toca la red**. Para la primera
descarga, elige una de las dos:

**Opción A — bájalo tú** (recomendada: sabes lo que entra y de dónde):

```bash
uv run python -c "from huggingface_hub import snapshot_download; snapshot_download('Systran/faster-whisper-small')"
```

**Opción B — deja que WISPI lo baje**: pon `asr.local_files_only: false` en
`config.yaml`, arranca una vez, y **devuélvelo a `true`** cuando termine.

Va a `~/.cache/huggingface/hub`. Si prefieres otra ruta, apunta `asr.download_root` ahí.

### 4. (Opcional) Ollama para el nivel 1

```bash
ollama pull llama3.1:8b
```

Si no lo instalas, pon `llm.enabled: false` en `config.yaml` y te ahorras el intento de
conexión. WISPI ya avisa por log y sigue funcionando si Ollama no responde.

### 5. Comprueba que todo está en su sitio

```bash
uv run python -m wispi.selftest --all
```

Te dice, por partes, si fallan el audio, el modelo, la inyección o el LLM. **Córrelo
antes de abrir un issue**: su salida es lo que más ahorra.

---

## Primer arranque

```bash
uv run python -m wispi --console
```

Abre el Bloc de notas, pon el cursor dentro, mantén `Ctrl+Win`, di una frase y suelta.
El texto debería aparecer en menos de dos segundos.

Si no aparece nada, ve a [Problemas frecuentes](#problemas-frecuentes).

### Que arranque con Windows

```powershell
.\scripts\install_autostart.ps1              # sin elevar (default)
.\scripts\install_autostart.ps1 -Elevated    # opt-in explícito
```

Registra una tarea programada que lanza WISPI con `pythonw.exe`, sin ventana de consola.
Para quitarla: `.\scripts\uninstall_autostart.ps1`.

`-Elevated` hace que el hook funcione también sobre ventanas de administrador (UIPI
bloquea input de procesos bajos hacia ventanas altas). **Lee
[SECURITY.md](SECURITY.md#el-modo-elevado-lo-que-aceptas) antes**: un hook de teclado
global corriendo como admin tiene la misma forma técnica que un keylogger, y la única
contramedida real es que el código no escriba lo que ve. El default es sin elevar por eso.

---

## Uso

| Gesto | Qué hace |
|---|---|
| Mantener `Ctrl+Win` | Graba mientras la sujetas (push-to-talk) |
| Doble toque rápido de `Ctrl+Win` | Manos libres: corta solo tras 1,2 s de silencio |
| `Esc` mientras graba | Cancela. Cero caracteres insertados |
| **Un toque en el botón flotante** | Empieza o termina el dictado |
| **Doble toque en el botón** | Abre el teclado táctil |
| Arrastrar el botón | Moverlo; la posición se guarda sola |

```bash
uv run python -m wispi                      # bandeja, sin consola
uv run python -m wispi --console            # consola, para desarrollo
uv run python -m wispi.selftest --all       # diagnóstico por partes
uv run python -m wispi.bench --analyze      # ¿toca migrar de motor?
uv run python tools/e2e_pipeline.py         # 21 pruebas automáticas, sin voz humana
```

### Teclado táctil (sin tocar el teclado físico)

Doble toque en el botón abre un panel con cuatro pestañas:

- **Teclas** — Enter, Tab, Esc, Retroceso, Supr, Espacio. Retroceso y Supr **se
  repiten al mantenerlos**, como una tecla de verdad.
- **Mover** — flechas, Inicio/Fin, `Sel ←/→`, seleccionar palabra, seleccionar todo,
  copiar/pegar/cortar, deshacer/rehacer.
- **Símbolos** — 36 símbolos que cuesta dictar: `@ # / \ | { } < > ¿ ¡ € …`
- **Opciones** — pausar, estado, configuración, salir.

**Enter es la tecla que convierte esto en un sustituto del teclado.** WISPI nunca
inyecta un salto de línea al dictar —para que un prompt dictado a Claude Code no se
autoenvíe— así que sin este botón dictabas un mensaje y necesitabas el teclado igual
para mandarlo.

**Para borrar no hay magia: es Retroceso.** Nada de "deshacer el último dictado"
adivinando qué caracteres fueron nuestros. Borra lo que hay antes del cursor, igual
que la tecla física, y si quieres quitar más lo seleccionas primero con `Sel ←`.
Mismo modelo mental que un teclado, cero sorpresas y cero riesgo de que WISPI se
lleve por delante algo que escribiste tú.

### Sobre el combo `Ctrl+Win`

Es el de Wispr Flow, a propósito: se migra sin reaprender. Es seguro porque `Win` solo
abre el menú Inicio al soltarlo, y con `Ctrl` pulsado Windows no lo abre. WISPI **nunca**
se traga esas teclas: solo observa.

> **Teclado español:** nunca configures `Right Alt` como hotkey — es AltGr y genera
> `@ # ~ [ ] \`. `Ctrl+Alt+<tecla>` tampoco: Windows lo trata como AltGr+tecla.

---

## Configuración

`config.yaml` se recarga en caliente (< 3 s, sin reiniciar). `dictionary.yaml` también.
Lo que escribe la ventana de configuración va a `config.local.yaml`, que **gana** sobre
`config.yaml` (por eso, si tocas una clave a mano y no ves el efecto, mira ahí).

Las perillas que más vas a tocar:

```yaml
asr:
  model: small              # tiny | base | small | large-v3
  cpu_threads: 10           # PON AQUÍ TUS NÚCLEOS FÍSICOS
audio:
  silence_threshold: 0.012  # súbelo si tu sala es ruidosa y se cuelan dictados vacíos
llm:
  enabled: true             # false si no tienes Ollama
  min_words: 25             # por debajo de esto no se llama al LLM
```

### Tu jerga es la mitad del valor

`dictionary.yaml` trae 40 términos técnicos de uso general (Supabase, commit, n8n,
RLS...) con 305 variantes de cómo Whisper-es los destroza. Ataca el problema por los
dos lados: los canónicos alimentan el `initial_prompt` (**previene** el error dentro del
decoder) y las variantes se compilan en un regex (**cura** lo que se escapa).

Lo que **no** trae, porque nadie más lo dice, son los nombres propios de tus proyectos,
tus clientes y tu equipo — que son justo los que Whisper peor escribe, porque no estaban
en su corpus. Añadirlos es diez minutos y es donde más se nota:
**[`dictionary.personal.example.yaml`](dictionary.personal.example.yaml)** tiene el
formato, el método para sacar las variantes midiendo en vez de inventando, y las trampas.

La regla principal, que está comentada en el propio fichero: **ninguna variante puede ser
una palabra española corriente**. Un falso positivo (comerse una palabra buena) es mucho
peor que un falso negativo (dejar un "comit" sin arreglar), porque no lo ves venir.

---

## La medición que decidió el modelo

Antes de escribir una línea se midió el ASR (i9-10850K, int8, `cpu_threads=10`,
`beam_size=1`):

| modelo | piso por dictado |
|---|---|
| `large-v3` | **6,26 s** |
| `small` | **1,24 s** |
| `base` | 0,36 s |
| `tiny` | 0,21 s |

**El costo es fijo, no proporcional al audio**: un clip de 5 s y uno de 10 s dan el mismo
número. Whisper rellena a una ventana de 30 s y corre **una pasada de encoder** pase lo
que pase. En dictados cortos —que es todo lo que hace WISPI— el encoder *es* la latencia.

Dos corolarios verificados, no opinados:

- **`large-v3-turbo` y `distil-large-v3` no ayudan.** Recortan el *decoder* (32→4 capas) y
  conservan el encoder de large-v3 intacto. 1,6 GB de descarga para seguir en ~6,3 s.
- **`chunk_length` es palanca muerta.** Probado `None`/15/10 → 6,39 / 6,53 / 6,43 s.

Por eso el default es `small`, y la pérdida de calidad se compensa por donde toca: el
diccionario alimentando `initial_prompt` **previene** el error dentro del decoder en vez de
parchearlo después.

### La escalera de migración

`large-v3` no se abandona — pero la medición demuestra que su única vía es la GPU.
Cuándo y cómo se sube ese peldaño lo decide `wispi.bench`, no una corazonada:

```bash
uv run python -m wispi.bench --analyze
```

Objetivo: **p50 ≤ 1200 ms, p90 ≤ 2000 ms** sobre ≥ 100 dictados reales en ≥ 3 días. Si se
cumple, no se migra nada. Si no, primero mira si `asr_ms / ttt_ms > 0,6`: por debajo de eso
el cuello **no es el modelo** y cambiarlo no arreglaría nada.

Si acabas subiendo a GPU: `uv sync --extra gpu`, y **en un venv aparte** (`.venv-gpu`).
Si el CUDA queda a medias, el modelo carga y revienta al transcribir; mejor que eso no te
deje sin el entorno que funciona.

## El formato del prompt importa más que su contenido

El plan daba por supuesto que meter la jerga en `initial_prompt` es prevención pura y por
tanto mejor que el regex. **Es cierto solo si el prompt es prosa.**

Whisper trata `initial_prompt` como texto previo y **continúa su estilo**, no solo su
vocabulario. Con una lista `"Supabase, Vercel, Next.js, ..."` el decoder empieza a devolver
listas y trunca: una frase de 13 palabras salía como `"workflow, n8n, webhook, prisman,"`.

| formato del prompt | retención | jerga | p50 |
|---|---|---|---|
| sin prompt | 99 % | 17/31 | 1494 ms |
| **lista** | **77 %** | 27/31 | 1566 ms |
| **prosa** (elegido) | **100 %** | **30/31** | **1534 ms** |
| prosa + `beam=5` | 100 % | 31/31 | 1893 ms |

La prosa gana en los dos ejes a la vez y cuesta 40 ms sobre no tener prompt. `beam=5`
compra un término más por +359 ms: rechazado.

La prosa vive en `dictionary.yaml::prompt_prose` y se edita a mano. Si la borras, se genera
automáticamente desde los `canonical` — funciona, pero peor. Y `initial_prompt_style` está
**vacío a propósito**: una frase meta como *"Transcripción técnica en español con términos
en inglés"* no es lenguaje natural y contagia; con ella puesta, *"Crea un endpoint"* salía
como *"Create an endpoint"*.

> Si algún día cambias el prompt, corre `uv run python tools/e2e_pipeline.py` antes de
> darlo por bueno. Este fallo no se ve leyendo el código: se ve midiendo.

---

## Privacidad

- **Nada sale de la máquina.** ASR local, LLM local (Ollama), cero API keys, cero
  telemetría, cero cuentas.
- `logs/latency.jsonl` guarda tiempos y longitudes, **nunca el texto transcrito**
  (`logging.include_text: false`).
- El logger nunca registra el `vkCode` de teclas fuera del combo.
- Con `local_files_only: true`, WISPI arranca y transcribe sin red.

WISPI instala un hook de teclado global, lee el portapapeles e inyecta pulsaciones. Eso
merece más de un párrafo: el modelo de amenaza completo, qué comprobar y en qué fichero,
está en **[SECURITY.md](SECURITY.md)**.

---

## Limitaciones conocidas

Lo que no hace, para que no lo descubras a mitad de una frase:

- **Solo Windows.** No hay planes de portarlo.
- **Solo español de fábrica.** Ver [arriba](#el-idioma-viene-puesto-en-español).
- **Nada de streaming.** No ves palabras mientras hablas: el texto aparece entero al
  soltar. Es consecuencia de que el encoder de Whisper corre una vez sobre todo el clip.
- **Si la inyección falla en una app rara, el texto se pierde.** No hay historial de
  dictados todavía (está en el [ROADMAP](ROADMAP.md)).
- **`Ctrl+V` usa el portapapeles.** Se restaura después, pero durante unos 400 ms el
  contenido es tu dictado.
- **Sin instalador ni firma de código.** Se instala clonando el repo. Tu antivirus
  probablemente pregunte; ver [SECURITY.md](SECURITY.md#tu-antivirus-lo-va-a-mirar-raro).
- **Probado a fondo en una máquina y una voz.** Los criterios que exigen voz real, apps
  reales o un reinicio están marcados 🔴 en [SPEC.md](SPEC.md): son declaraciones honestas
  de lo que aún no se ha verificado, no descuidos.

---

## Problemas frecuentes

**No aparece nada al dictar.**
`uv run python -m wispi.selftest --all` y mira qué parte falla. Lo más común: el micro
equivocado en `audio.input_device`, o hablar demasiado bajo para
`audio.silence_threshold`.

**Funciona en el Bloc de notas pero no en mi terminal.**
`Ctrl+V` no pega en muchas terminales. Añade el `.exe` a `injection.terminal_apps` en
`config.yaml` para que use `Shift+Insert`.

**No funciona sobre una ventana de administrador.**
Es UIPI, no un bug. Necesitas el autoarranque elevado — lee antes
[SECURITY.md](SECURITY.md#el-modo-elevado-lo-que-aceptas).

**Se traga la primera sílaba.**
Sube `audio.preroll_ms`. Y comprueba que no se está cortando por
`audio.start_grace_s`.

**Descarta dictados buenos como si fueran silencio.**
Baja `audio.silence_threshold` (por defecto 0,012). Si es al revés y se cuelan dictados
vacíos, súbelo.

**El modelo no carga / error de `local_files_only`.**
No has descargado el modelo todavía. Ver [paso 3 de la instalación](#3-descarga-el-modelo-de-whisper).

**De repente dejó de responder al hotkey.**
Windows aplica `LowLevelHooksTimeout` y desengancha hooks lentos en silencio. Hay un
watchdog que reinstala cada 5 minutos; si te pasa a menudo, es un fallo: ábrelo como
issue con la salida del selftest.

**Escribe "comit", "supa base", "n ocho n".**
Es exactamente lo que arregla el diccionario. Ver
[Tu jerga es la mitad del valor](#tu-jerga-es-la-mitad-del-valor).

---

## Estructura

```
wispi/
  app.py           máquina de estados — ÚNICO sitio con lógica de orquestación
  winapi.py        TODAS las declaraciones ctypes (los bugs de Win64 son de tipos)
  hotkey.py        hook WH_KEYBOARD_LL + watchdog          ← punto de fallo nº 1
  audio.py         InputStream permanente + ring de pre-roll
  asr/base.py      el Protocol que hace intercambiable el motor
  inject/          cascada Ctrl+V / Shift+Insert / Unicode ← punto de fallo nº 2
  postprocess/     nivel 0 (reglas + diccionario) y nivel 1 (Ollama)
  metrics.py       instrumentación → logs/latency.jsonl
  bench.py         la regla de decisión de la migración
tools/
  make_corpus.py   genera el corpus con Piper (no hace falta voz humana)
  target_window.py ventana diana para probar la inyección cross-process
  e2e_pipeline.py  las 21 pruebas end-to-end
```

## Los dos sitios donde esto se puede romper

1. **`hotkey.py`.** Windows aplica `LowLevelHooksTimeout` (300 ms) y si el callback se pasa
   **desengancha el hook en silencio, sin avisar**. En Python el riesgo real no es tardar,
   es no poder *empezar* porque otro hilo tiene el GIL. De ahí el callback de < 0,5 ms que
   solo hace `put_nowait`, el `setswitchinterval(0.001)`, y el watchdog que reinstala a
   ciegas cada 5 min.

2. **`inject/`.** La inyección de texto es frágil por diseño en Windows. El incidente de
   Wispr Flow con Claude Code demuestra que ni con financiación se resuelve del todo. Por
   eso la cascada existe desde el día 1 y no como parche, y por eso el parche optimista
   tiene **cuatro compuertas**: mismo HWND y PID, < 1,5 s, cero teclas del usuario desde la
   inserción, y app fuera de `no_patch_apps`. Falla una y se queda el crudo. Perder la
   limpieza es un fastidio; corromper un documento es inaceptable.

---

## Estado y contribuir

[SPEC.md](SPEC.md) tiene los criterios de éxito verificables y cuáles siguen abiertos.
[ROADMAP.md](ROADMAP.md), lo hecho y lo siguiente.

Lo que más ayuda no es una PR: es **decir qué te falló, en qué app y con qué máquina**.
WISPI se probó a fondo en una sola máquina y con una sola voz. Ver
[CONTRIBUTING.md](CONTRIBUTING.md).

## Licencia

[MIT](LICENSE). Haz lo que quieras con esto.
