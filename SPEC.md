# SPEC — WISPI v1

**Versión:** 1.0 · **Fecha:** 2026-08-09 · **Estado:** en construcción

---

## 1. Objetivo real

**No** es "hacer una app de dictado". Es: **que se pueda desinstalar Wispr Flow y no echarlo de menos.**

Ese es el criterio que decide los empates. Una feature elegante que no acerque a la desinstalación
no entra en v1.

Wispr Flow v1.6.447 está instalado y en uso. Falla en tres puntos concretos, y son los tres que WISPI
tiene que resolver:

| Fallo de Wispr Flow | Evidencia | Qué exige de WISPI |
|---|---|---|
| Todo el audio va a la nube, sin modo offline | *"Transcription always occurs on the cloud"* — wisprflow.ai/data-controls | 100 % local. Sin red, sin API keys, sin cuota |
| Se rompe al dictar a Claude Code en Windows | [claude-code#38620](https://github.com/anthropics/claude-code/issues/38620) | La inyección en Windows Terminal es requisito, no extra |
| No conoce la jerga: destroza Supabase, Vercel, n8n, RLS, commit | Uso diario | Diccionario que **previene** el error en el decoder, no que lo parchee |

Coste actual evitado: $15/mes (Pro) o el límite de 2.000 palabras/semana del plan gratis.

## 2. Fuera de alcance (explícito)

No se construye en v1, y proponerlo no es "mejorar el plan", es desviarlo:

- Command Mode (editar texto seleccionado por voz).
- Snippets por voz.
- Perfiles configurables por aplicación. **Excepción:** la cascada de pegado
  (Ctrl+V / Shift+Insert) sí se construye — es fontanería sin la cual no se puede dictar en terminal,
  no un sistema de perfiles.
- Aprendizaje automático del diccionario. Los términos se añaden a mano.
- Migración del histórico de `flow.sqlite` de Wispr Flow.
- Distribución a terceros: instalador firmado, autoupdate, code signing. Es una herramienta personal.
- ASR en streaming con parciales en vivo.

## 3. Criterios de éxito — verificables sí/no

Contra estos se verifica, **nunca contra el `status` que un agente se ponga a sí mismo.**

### C1 — Aislamiento del entorno
- **C1.1** `.venv\Scripts\python.exe` existe dentro del repo y `where python` sigue apuntando
  al Python de siempre del sistema. WISPI no contamina otro proyecto. ✅ VERIFICADO 2026-08-09
- **C1.2** `uv run python -c "import faster_whisper, sounddevice, pystray"` imprime `ok`. ✅ VERIFICADO
- **C1.3** Con la red apagada, WISPI arranca y transcribe (`local_files_only: true`).

### C2 — Corrección de los tipos Win32
- **C2.1** `sizeof(INPUT) == 40`, `sizeof(KBDLLHOOKSTRUCT) == 24`, `sizeof(LRESULT) == 8`. ✅ VERIFICADO
- **C2.2** `SendInput` devuelve el número de eventos enviados, nunca 0, en las tres rutas.
- **C2.3** `exe_of_pid()` resuelve el ejecutable de la ventana en foco. ✅ VERIFICADO (`claude.exe`)

### C3 — Hotkey (el punto de fallo nº 1)
- **C3.1** Mantener `Ctrl+Win` inicia grabación; soltar cualquiera de las dos la termina.
- **C3.2** Pulsar y soltar `Ctrl+Win` **no abre el menú Inicio**, 20 veces seguidas.
- **C3.3** El p99 de duración del callback del hook es **< 5 ms** tras 1.000 eventos.
  (Presupuesto de Windows: 300 ms. Objetivo interno: < 0,5 ms.)
- **C3.4** El hook ignora input sintético (`LLKHF_INJECTED`) salvo con `accept_injected: true`.
  Sin esto, la propia inyección se auto-dispararía.
- **C3.5** Tras 50 dictados seguidos el hook sigue vivo; `slow_events == 0`.
- **C3.6** `Esc` durante la grabación cancela: **cero** caracteres insertados.
- **C3.7** Doble-toque (< 250 ms cada pulsación, < 400 ms entre ellas) entra en manos libres;
  una pulsación PTT normal (> 500 ms) **no** añade ventana de espera.

### C4 — Inyección de texto (punto de fallo nº 2)
- **C4.1** El texto llega completo y con tildes correctas en: Notepad, Windows Terminal + Claude Code,
  Chrome (WhatsApp Web, Gmail), Obsidian, VS Code.
  🟡 PARCIAL — **18/18 exactos cross-process** contra la ventana diana Tk, con tildes,
  eñes y textos de 28 a 243 caracteres. Un `Text` de Tk **no es** Windows Terminal ni
  Electron: las apps reales siguen 🔴.
- **C4.2** En apps de `terminal_apps` la ruta usada es `shift_insert`, verificable en `latency.jsonl`.
  🟡 PARCIAL — `choose_route()` clasifica bien por nombre de proceso; falta comprobar
  que `Shift+Insert` de verdad pega en Windows Terminal. 🔴
- **C4.3** **Nunca** se inyecta `\n` final. Un prompt dictado a Claude Code no se auto-envía.
  ✅ VERIFICADO — ninguno de los 18 clips insertó salto final.
- **C4.4** Portapapeles restaurado: copiar `CANARIO`, dictar, y `Ctrl+V` manual sigue pegando `CANARIO`.
  ✅ VERIFICADO — marcador de control intacto tras **18 inyecciones seguidas**.
- **C4.5** Todo input emitido lleva `dwExtraInfo == 0x57495350`.
- **C4.6** *(nuevo)* **Nunca se inyecta sin confirmar el destino.** Si la ventana en foco
  no es la esperada, no se escribe. ✅ VERIFICADO en el arnés — ver hallazgo H3.

### C5 — ASR
- **C5.1** `faster-whisper small` int8 CPU transcribe un clip de 10 s en **< 2,0 s**.
  ✅ VERIFICADO — p50 **1584 ms** sobre 18 clips con voz (`tools/e2e_pipeline.py`).
- **C5.2** Si el modelo del primer eslabón no carga, el registry avanza por `fallback_chain` y lo
  registra con WARNING. Nunca degrada en silencio.
- **C5.3** Cambiar `asr.model` en `config.yaml` cambia el modelo sin tocar código.
- **C5.4** El backend recibe **siempre** float32 mono 16 kHz en [-1, 1].

### C6 — Anti-alucinación
- **C6.1** 5 s de silencio con `Ctrl+Win` mantenido insertan **cero** caracteres.
  🔴 exige la tecla; el equivalente sobre corpus sí está verificado (C6.2).
- **C6.2** Se descarta si `peak_rms < 0.012` o duración < 0,35 s, **antes** de llamar al ASR.
  ✅ VERIFICADO — 3/3 clips sin voz descartados (rms 0.00000 / 0.00079 / 0.00399).
- **C6.3** Las frases-basura conocidas ("Subtítulos realizados por la comunidad de Amara.org",
  "Gracias por ver el video", "¡Suscríbete al canal!", "Gracias.") se filtran.

### C7 — Diccionario y nivel 0
- **C7.1** De los 18 términos de jerga del corpus, **≥ 16 salen escritos correctamente**.
  ✅ VERIFICADO — **18/18**. Requirió el hallazgo del formato del prompt (ver abajo);
  con el prompt en lista eran 16/18 **y** se perdía el 23 % del texto.
  El umbral es ≥ 16 y no 18 a propósito: ni Piper ni el decoder son bit a bit
  reproducibles entre corridas, así que la cifra oscila. Cada oscilación destapa una
  variante que faltaba en el diccionario (así entraron "Ossidian" y "Remotin"): cuando
  baje de 18, mira qué término cayó y añádelo en vez de repetir la corrida.
- **C7.2** Las muletillas de la lista desaparecen.
  ✅ VERIFICADO — 3/3 clips limpios, y `tools/test_level0.py` da 21/21.
  Alcance real: las muletillas SEGURAS (no son palabras) caen siempre; las BLANDAS
  ("pues", "este", "digamos") solo cuando la puntuación las delata. "Pues digamos que
  no responde" se queda entero **a propósito**: es español legítimo.
- **C7.3** `level0_ms` p99 **< 5 ms**. ✅ VERIFICADO — **0,434 ms** p99; 0,030 ms/frase
  en el test unitario sobre 420 frases.
- **C7.4** Editar `dictionary.yaml` surte efecto **sin reiniciar** (< 3 s).
- **C7.5** El diccionario alimenta **los dos** sitios: `initial_prompt` (prevención) y regex (cura).
  ✅ VERIFICADO, con una corrección de diseño: la prevención solo funciona si el prompt
  es **prosa española**, no una lista de términos. Ver "Hallazgos" al final.
- **C7.6** *(nuevo)* **Cero falsos positivos** sobre las trampas del corpus: "Bueno es el
  adjetivo correcto", "Este endpoint... y este otro", "padres e hijos", "opciones a, b,
  c, d, e, f", "Funciona o no?". ✅ VERIFICADO — 2/2 en corpus, 21/21 en unitarios.
  Este criterio no estaba en la v1.0 del SPEC y se añadió al descubrir que el nivel 0
  se comía el "Bueno" legítimo de `trampa02`.

### C8 — Nivel 1 (LLM) e inserción optimista
- **C8.1** Un dictado de 40 palabras inserta el crudo en < 2 s y lo parchea en < 2,5 s adicionales.
- **C8.2** Un dictado de 12 palabras **no** llama al LLM (`llm_ms: null` en el jsonl).
- **C8.3** Cambiar de ventana durante la espera **aborta el parche**; el crudo queda intacto y la
  otra ventana no se toca.
- **C8.4** En `no_patch_apps` **nunca** hay parche: espera acotada de 700 ms y **una sola** escritura.
- **C8.5** Con Ollama caído, cae a nivel 0, inserta, registra WARNING y **no** crashea.
- **C8.6** Si la salida del LLM difiere en longitud > ±40 % o trae markdown/preámbulo, se descarta
  y se queda el crudo.

### C9 — Privacidad
- **C9.1** Con `include_text: false` (default), ni `wispi.log` ni `latency.jsonl` contienen texto
  transcrito. Verificable con grep sobre los logs tras 20 dictados.
- **C9.2** El logger **nunca** registra `vkCode` de teclas fuera del combo.

### C10 — Operación
- **C10.1** Tras reiniciar Windows, el icono está en la bandeja **sin ventana de consola** y el
  primer dictado funciona sin tocar nada.
- **C10.2** Con Pausar activo, `Ctrl+Win` no hace nada.
- **C10.3** Editar `config.yaml` recarga en < 3 s sin reiniciar, **y el valor nuevo llega
  al módulo que lo consume**, no solo al objeto `Config`. Las claves de
  `config.py::RESTART_ONLY` son la excepción declarada: se conserva el valor vivo y se
  registra un WARNING. ✅ VERIFICADO — `tools/test_config_reload.py` (12 comprobaciones
  en caliente + identidad de secciones), y en la app en marcha el 2026-08-10:
  `tail_ms`, `silence_threshold`, `restore_clipboard`, `wake.threshold`, los frames
  derivados del segmentador y el volumen de los chimes entran solos; `wake.model` se
  conserva y avisa. Ver H5.
- **C10.4** Desconectar el micro USB en caliente produce error visible, no crash.

### C11 — Palabra de activación ("hey WISPI")

Añadido en v0.3. Objetivo real: **dictar sin tocar nada** cuando las manos no están en el
teclado. No sustituye a `Ctrl+Win`, que sigue siendo el camino rápido y el que nunca falla.

- **C11.1** Con `wake.enabled: false` el detector **no existe**: cero hilos nuevos, cero
  modelos cargados, cero CPU. Verificable en `status()["wake"]["enabled"] == False` y en
  que `wispi.log` no registra ninguna línea de `wake`. El default del **código** es
  `false`; `config.yaml` lo enciende a propósito y lo dice en un comentario, porque un
  micrófono siempre puesto no puede quedar escondido en un valor por defecto.
- **C11.2** Decir "hey WISPI" con WISPI en reposo arranca un dictado en **manos libres**
  (corta solo por silencio), sin tocar teclado ni ratón.
  ✅ VERIFICADO — 2026-08-10, `uv run python -m wispi.selftest --wake`, voz real de
  Junior: **2 activaciones sobre 3 enunciados analizados** (`tiny`, `cpu_threads=2`,
  314 ms de inferencia). Transcripciones reales: *"Hey, Whisby."* (score 0,824) y
  *"¡Hey, Whispy!y."* (score 0,941) — ninguna estaba en el corpus sintético de
  `tools/test_wake.py`; se añadieron después, con el score real, para que un ajuste
  de umbrales futuro no las rompa en silencio.
- **C11.3** **La frase de activación nunca se escribe.** Tras despertar, el texto insertado
  no contiene "hey wispi" ni variantes. Es el criterio que decide el diseño: la grabación
  arranca **sin pre-roll** (`wake.include_preroll: false`). 🔴 **HUMANO** — falta probar
  el dictado completo tras el despertar (`selftest --wake` solo detecta, no dicta).
- **C11.4** El detector **solo escucha en reposo**. Mientras se graba, transcribe, pule o
  está en pausa, está desarmado: no puede auto-dispararse con el propio dictado ni robar
  CPU al ASR real. Verificable en `status()["wake"]["armed"]`.
- **C11.5** **Coste en reposo ≈ 0.** Con la sala en silencio no se llama al ASR ni una vez:
  el reconocedor solo corre sobre *enunciados cortos aislados* (entre `min_speech_s` y
  `max_speech_s`, cerrados por `end_silence_s` de silencio). Una conversación seguida o una
  llamada no producen candidatos. Verificable con `--wake` en `wispi.selftest`:
  `checks` se queda en 0 con la sala callada.
- **C11.6** **Cero falsos positivos** sobre las trampas del corpus de texto:
  "hey wifi", "hey", "whisky", "y esto", "es que sí", "wikipedia".
  Verificable sin voz con `tools/test_wake.py`.
- **C11.7** Las variantes que Whisper produce de verdad para la frase **sí** disparan:
  "Hey, Wispi.", "Ey Wispy", "Hey, Guispi", "Ay, Wispi", "Oye Wispi", "hey wis pi".
  Verificable sin voz con `tools/test_wake.py`.
- **C11.8** Si el modelo del detector no carga, se avisa con WARNING, la palabra de
  activación queda desactivada y **WISPI sigue dictando con `Ctrl+Win`**. Nunca tumba la app.
- **C11.9** Privacidad: con `logging.include_text: false` (default) **nada de lo que oye el
  detector se escribe en ningún log**, ni siquiera lo que descarta. Verificable con grep
  sobre `wispi.log` tras hablar 5 minutos junto al micro.
- **C11.10** Tras un disparo hay `cooldown_s` de refractario: hablar seguido no encadena
  dos activaciones.

## 4. Bloques y checkpoints

| Bloque | Contenido | Checkpoint |
|---|---|---|
| 0 | Andamiaje, contratos, SPEC | ✅ cerrado (C1, C2) |
| 1 | Rebanada vertical: dicto → aparece texto | 🔴 **HUMANO** — exige voz real |
| 2 | Cascada de inyección, cancelar, manos libres, anti-alucinación | 🔴 **HUMANO** — exige apps reales |
| 3 | Diccionario + nivel 0 | 🟡 automatizable con corpus Piper |
| 4 | Bandeja, hot-reload, autoarranque | 🔴 **HUMANO** — exige reinicio |
| 5 | Nivel 1 LLM + inserción optimista | 🟡 automatizable |
| 6 | Medición sobre ≥ 100 dictados reales | 🔴 **HUMANO** — exige uso real |
| 7 | Palabra de activación "hey WISPI" (C11) | 🟡 C11.2 verificado con voz real; falta C11.3 (que no se escriba la frase) |

## 5. Acciones que requieren permiso humano

1. **Instalar el autoarranque elevado.** Un hook de teclado global con privilegios de administrador
   tiene forma de keylogger. El default es sin elevar; la variante elevada es opt-in explícito.
2. **Instalar las libs CUDA** y subir el peldaño 2 de la escalera de migración. Un CUDA a medias
   carga el modelo y revienta al transcribir; ya costó una semana de servicio caído una vez.
3. **Crear el repositorio remoto** y el primer push.
4. **Desinstalar Wispr Flow.** No se toca hasta que los criterios 🔴 estén cerrados: su base de
   datos local tiene histórico que se perdería.

## 6. Hallazgos que cambiaron el diseño

Se registran aquí porque contradicen lo que el plan daba por supuesto, y quien vuelva
a tocar esto necesita saberlo antes de "arreglarlo" de vuelta.

### H1 — El `initial_prompt` en lista destrozaba las transcripciones

El plan asumía que meter la jerga en `initial_prompt` es prevención pura y por tanto
mejor que el regex. **Es cierto solo si el prompt es prosa.** Whisper trata el
`initial_prompt` como texto previo y **continúa su estilo**, no solo su vocabulario:
con una lista `"A, B, C"` el decoder empieza a devolver listas y trunca. Una frase de
13 palabras salía como `"workflow, n8n, webhook, prisman,"`.

Medido (small int8, beam=1, 10 clips, 2026-08-09):

| formato | retención | jerga | p50 |
|---|---|---|---|
| sin prompt | 99 % | 17/31 | 1494 ms |
| **lista** | **77 %** | 27/31 | 1566 ms |
| **prosa** | **100 %** | **30/31** | **1534 ms** |
| prosa + beam=5 | 100 % | 31/31 | 1893 ms |

La prosa gana en los dos ejes por 40 ms sobre no tener prompt. `beam=5` compra un
término más por +359 ms: rechazado. La prosa vive en `dictionary.yaml::prompt_prose`.

Corolario: `initial_prompt_style` quedó **vacío**. La frase meta *"Transcripción técnica
en español con términos en inglés"* no es lenguaje natural y contagiaba el estilo —
con ella puesta, *"Crea un endpoint"* salía como *"Create an endpoint"*.

### H2 — La misma regla de muletillas fallaba en los dos sentidos

`trampa02` perdía el "Bueno" legítimo mientras `muletilla01` conservaba las suyas. La
causa era una sola: la excepción de cópula existía solo en la rama sin coma, y Whisper
puntúa la pausa. Además `"Eh"` se transcribe como `"E,"`, que no estaba en la lista, y
al sobrevivir desplazaba a la siguiente muletilla fuera de la posición inicial.
Arreglado en `level0.py`, con `tools/test_level0.py` como red de regresión.

### H3 — La verificación de inyección puede escribir en apps reales del usuario

La inyección va donde esté el foco. Windows bloquea `SetForegroundWindow` desde
procesos en segundo plano, así que el `focus_force()` de la ventana diana falla a
veces **en silencio**, y el injector reporta éxito porque desde su punto de vista lo
tuvo. En una corrida, 14 de 18 clips fueron a parar a otra ventana.

`tools/e2e_pipeline.py` ahora **comprueba el PID del foco contra el de la diana antes
de inyectar** y, si no coinciden, no inyecta. Es una guarda de seguridad, no del test.

### H4 — Un solo umbral no distingue "hey WISPI" de "hey wifi"

El diseño obvio de la palabra de activación es un umbral de parecido contra la frase
entera. **No funciona.** Medido con `difflib.SequenceMatcher` sobre las cadenas
normalizadas:

| candidato | contra "hey wispi" | contra "wispi" solo | ¿debe disparar? |
|---|---|---|---|
| "hey guispi" | 0,824 | 0,727 | sí |
| **"hey wifi"** | **0,800** | **0,667** | **no** |
| "hey" | 0,545 | — | no |

Con un umbral único, cualquier valor que acepte "guispi" acepta también "wifi": están a
24 milésimas. Lo que los separa es mirar **el nombre por separado**, donde la distancia
se abre a 60 milésimas. De ahí los dos umbrales (`threshold` 0,75 y `name_threshold`
0,70) y de ahí que `name_threshold` no se pueda bajar de 0,65 sin abrir falsos positivos.

Segundo hallazgo del mismo sitio: hizo falta una regla fonética, **-y final → -i**.
"hey guispy" se quedaba en 0,706 y no disparaba, aunque es una transcripción
perfectamente normal de la frase. En español las dos terminaciones suenan igual y el
modelo elige sin criterio; aplicando la regla a los dos lados de la comparación sube a
0,824 sin inventar parecidos. Ninguna de las 18 trampas empeoró.

### H5 — La recarga en caliente llegaba al `Config` y no a los módulos

`maybe_reload()` volvía cada sección a sus defaults con
`setattr(self, 'audio', AudioCfg())`, o sea, **creaba un objeto nuevo**. Pero `audio.py`,
`hotkey.py`, `inject/injector.py` y `wake.py` guardan su sección al construirse, así que
seguían apuntando a la vieja. Medido el 2026-08-10, con `tail_ms` editado a 333:

```
cfg.audio.tail_ms              = 333   <- lo nuevo
app.audio.cfg.tail_ms          = 200   <- lo que USA audio.py
app.audio.cfg is app.cfg.audio -> False
```

C10.3 llevaba meses dándose por bueno porque **se comprobaba en `cfg`, que siempre estuvo
bien**. Es el mismo error de método que H3: verificar el sitio equivocado. Y el fallo no
se ve — no hay excepción ni log, la perilla simplemente no hace nada.

Dos correcciones, no una:

1. **De raíz:** las secciones se mutan campo a campo y su identidad no cambia nunca. Es
   el contrato que todos los consumidores ya asumían sin decirlo. La alternativa que se
   probó primero —repuntar la referencia a mano desde `app.py`, como hacía `_sync_wake`—
   obliga a acordarse en cada módulo nuevo, y olvidarse no da error: da un valor viejo.
2. **Lo que no puede ser caliente se declara y se avisa** (`config.py::RESTART_ONLY`).
   Aplicar `audio.sample_rate` en caliente no es una perilla sin efecto: haría que
   `_to_target()` resampleara a un rate que el ASR no espera, o sea transcripciones
   basura. Se conserva el valor vivo y se registra un WARNING.

Quedan dos cosas que la identidad estable no arregla y hay que sincronizar a mano desde
`WispiApp._on_config_reloaded()`: lo que se **deriva** una vez (los frames del
segmentador de `wake.py`) y lo que se **copia por valor** (`Feedback`, que además
pregenera los tonos). Un módulo nuevo que haga cualquiera de las dos cosas se añade ahí
y a `tools/test_config_reload.py`.

## 7. Regla de verificación

`verificar-spec` contrasta **contra los criterios de arriba**, nunca contra el reporte del agente.
Un `{"status":"completado"}` es afirmación suya, no evidencia. Un `NO CUMPLE` no entra a la ronda
aunque el JSON diga lo contrario.

Los criterios marcados 🔴 **no los puede cerrar un agente**: se quedan en `NO VERIFICABLE` hasta que
un humano los pruebe a mano, con voz y apps reales. Reportarlos como CUMPLE sería falsear el SPEC.
