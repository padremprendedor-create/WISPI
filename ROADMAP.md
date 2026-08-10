# ROADMAP — WISPI

**Última actualización:** 2026-08-10 · **Estado:** v0.2 en uso diario real, **publicado como open source** · v0.3 (palabra de activación) construida, pendiente de prueba con voz

El objetivo sigue siendo el de [SPEC.md](SPEC.md): que Wispr Flow se pueda desinstalar sin
echarlo de menos. Todo lo de aquí se ordena por eso, no por lo interesante que sea.

---

## Hecho

### v0.1 — dictado (commit `7599a5d`)
- [x] Hook `Ctrl+Win`: push-to-talk, doble toque para manos libres, `Esc` cancela
- [x] Captura con ring de pre-roll (300 ms) y cola (200 ms)
- [x] ASR intercambiable tras un Protocol · `faster-whisper small` int8 CPU
- [x] Cascada de inyección: Ctrl+V / Shift+Insert en terminales / Unicode
- [x] Diccionario de jerga: 40 términos técnicos, 305 variantes
- [x] Nivel 0 (muletillas + espaciado + capitalización) y nivel 1 (Ollama)
- [x] Inserción optimista con cuatro compuertas
- [x] Bandeja, chimes, autoarranque, `selftest`, `bench`
- [x] Corpus de 21 clips con Piper + ventana diana Tk para verificar sin voz humana

### v0.2 — interfaz y teclado (commits `de0f01f`, `5192f33`, `84cf2cc`)
- [x] Botón flotante que **no roba el foco** (`WS_EX_NOACTIVATE`)
- [x] Ventana de configuración de 5 pestañas, escribiendo en `config.local.yaml`
- [x] Recarga del motor ASR en caliente
- [x] Teclado táctil: teclas, navegación, 36 símbolos, opciones
- [x] Doble toque para abrir el panel, con cancelación del dictado iniciado
- [x] Auto-repetición en Retroceso, Supr y flechas

### Publicación (2026-08-10)
- [x] Licencia MIT, `SECURITY.md` con el modelo de amenaza del hook global,
      `CONTRIBUTING.md` y plantillas de issue
- [x] README con requisitos de sistema medidos e instalación desde cero, y `README.en.md`
- [x] Diccionario despersonalizado (40 términos universales); los nombres propios salen a
      `dictionary.personal.example.yaml`
- [x] CI en Windows: `uv sync --frozen` + regresión del nivel 0 (lo único que un runner
      puede probar de verdad)
- [x] Historial aplanado a un commit inicial limpio; el anterior queda en la rama local
      `historial-privado`
- [x] **Repo público** desde el 2026-08-10 — github.com/padremprendedor-create/WISPI

### v0.3 — palabra de activación "hey WISPI" (2026-08-10)
- [x] `wispi/wake.py`: segmentador por energía + reconocedor `tiny` aparte, sin
      dependencias nuevas y sin descargas nuevas. **La decisión de diseño**: no se
      corre Whisper sobre una ventana deslizante (coste fijo del encoder = ventiladores
      para siempre), se segmenta primero y solo llega al ASR un enunciado corto y
      aislado. Con la sala callada, cero llamadas
- [x] Emparejado difuso con dos umbrales. El segundo, el del nombre solo, es el que
      tumba "hey wifi" (0,80 contra la frase entera, 0,67 contra el nombre)
- [x] Desarmado fuera de IDLE (no se oye a sí mismo), arranque sin pre-roll (la frase
      de activación no se escribe), refractario de 2 s
- [x] Pestaña *Palabra clave* en configuración, `selftest --wake` para calibrar en vivo
- [x] `tools/test_wake.py`: 18 variantes + 18 trampas + 6 escenarios de segmentación.
      **36/36 y 6/6 sin voz humana**
- [x] Encendida en `config.yaml` para poder probarla; el default del código sigue en
      `false` y el fichero explica por qué y cómo apagarla. Es la única función que
      analiza el micro sin que se lo pidan, así que no puede quedar escondida
- [ ] 🔴 **Falta lo que ningún agente puede cerrar: probarla con voz real** (C11.2). Lo
      verificado es la lógica sobre texto y audio sintético, no que `tiny` oiga "wispi"
      en boca de Junior

A partir de aquí el repositorio lo puede leer cualquiera. Dos consecuencias que
conviene tener presentes al tocarlo:

- Lo que se empuja ya no se puede retirar del todo: alguien puede haberlo clonado.
- Los criterios 🔴 de abajo ahora los lee gente de fuera. Están marcados como lo
  que son —lo que todavía no se ha verificado— y así deben seguir: un criterio que
  se marca verde para que la lista quede bonita es la forma más rápida de que este
  documento deje de servir para nada.

---

## Pendiente de verificación humana (ningún agente puede cerrarlo)

Marcado 🔴 en el SPEC. Son los criterios que exigen voz, manos o un reinicio.
Si usas WISPI en tu máquina y compruebas alguno, cuéntalo en un issue: eso es
exactamente lo que aquí falta.

- [ ] **C3** — el hotkey con voz y dedos reales, 50 dictados seguidos
- [ ] **C4.1/C4.2** — apps reales, sobre todo **Windows Terminal + Claude Code**.
      La diana Tk demuestra que la inyección cross-process funciona, *no* que funcione en
      una terminal ni en Electron
- [ ] **C10.1** — reiniciar Windows y comprobar el autoarranque
      (`scripts/install_autostart.ps1`)
- [ ] **C6.1** — 5 s de silencio con la tecla mantenida → cero caracteres
- [ ] **C11.2/C11.3** — "hey WISPI" con voz real: que despierte, y que la frase de
      activación **no** acabe escrita. Lo demás de C11 está verificado sin voz

---

## Siguiente

### Alto valor
- [ ] **Comandos por voz**: "nueva línea", "punto y aparte", "borra eso", "mayúsculas".
      Ataca el problema por el lado contrario al teclado táctil: menos botones que buscar
      con el dedo. Riesgo a vigilar: falsos positivos al citar a alguien
- [ ] **Historial de dictados**: ver los últimos y reinsertarlos o copiarlos. Es la red
      cuando la inyección falla en una app rara — hoy el texto se pierde
- [ ] **Modo continuo**: dictar párrafos largos sin que corte a los 1,2 s de silencio

### Medio
- [ ] **Snippets**: frases frecuentes de una pulsación
- [ ] **Aprendizaje del diccionario**: proponer variantes nuevas a partir de lo que
      Whisper devuelve. Nada de esto existe todavía — ni el registro de candidatos ni
      la minería. Hoy los términos se añaden a mano
- [ ] Botón que lance el teclado en pantalla de Windows (`TabTip.exe`) para lo que WISPI
      no cubra. **No** reimplementar un teclado completo: semanas para quedar peor

### La decisión aplazada del motor
- [ ] Acumular ≥100 dictados reales en ≥3 días y correr `uv run python -m wispi.bench --analyze`.
      Objetivo p50 ≤ 1200 ms / p90 ≤ 2000 ms. Si se cumple, **no se migra nada**.
      Si no, mirar primero si `asr_ms / ttt_ms > 0,6`: por debajo de eso el cuello no es el
      modelo y cambiarlo no arreglaría nada.
      Escalera, en orden estricto: `base` CPU → `large-v3` GPU → Parakeet ONNX.
      `large-v3-turbo` y `distil` **no entran**: medido, comparten encoder con large-v3.

---

## Revisión adversarial — 2026-08-09

Cuatro lentes sobre `hotkey.py` e `inject/`. **Las cuatro: NO CUMPLE.** 11 hallazgos P0.
Se arreglaron los que causaban pérdida de datos con la app en uso; el resto queda aquí.

### Arreglado (commit de cierre)
- [x] **P0** `press()`/`insert()`/`replace()` no confirmaban el destino. La guarda de C4.6
      existía **solo en el arnés de pruebas**, no en el producto — el criterio estaba
      marcado como verificado sin estarlo
- [x] **P0** La auto-repetición encolaba pulsaciones a 55 ms que el worker consumía mucho
      más despacio: soltar el dedo dejaba retrocesos aplicándose **hasta 3 s después**, ya
      en otra ventana (medido: 15 de 20). Token de generación que las invalida al soltar
- [x] **P0** Ruta `none` devolvía `chars=len(body)` sin escribir nada → el parche
      seleccionaba esos caracteres del documento **real** del usuario
- [x] **P0** Bitmask pegado: un key-up perdido (Win+L, UAC, ventana elevada, el LCtrl falso
      de AltGr) dejaba un bit puesto **para siempre**, y entonces `Ctrl+C` + `Ctrl+V` metía
      a WISPI en manos libres grabando el micrófono. Resincronización en el hilo auxiliar
- [x] **P0** Compuerta 3 ciega a las teclas del propio panel táctil (salen marcadas como
      inyectadas y el hook las filtra) → contador propio `_ui_keys`
- [x] **P1** `is_terminal` ahora implica no-parche siempre, sin depender de que
      `no_patch_apps` esté sincronizada a mano con `terminal_apps`
- [x] **P1** El botón Espacio no escribía nada (`_normalize` trata los blancos como vacío)
- [x] **P1** `ui_insert_text` descartaba el Future: los fallos eran mudos

### Arreglado — segunda ronda (mismo día)
- [x] **P0** `apply_config()` liberaba el trampolín ctypes de un hook aún instalado si el
      re-enganche fallaba. Ahora `self._hook_proc` es la única fuente de verdad de qué
      trampolín sigue vivo, actualizada solo donde `self._hook` cambia de verdad
      (`_run`, `_do_rehook`). Verificado con un fallo de reenganche forzado: el proc viejo
      sigue vivo tras `gc.collect()` (`tools/test_hook_resilience.py`, caso 1)
- [x] **P0** `SetWindowsHookExW` NULL al arranque ya no mata el hilo del hook — sigue en el
      bucle de mensajes (necesario para que `PostThreadMessageW` tenga a quién despertar) y
      el watchdog reintenta con backoff (1, 2, 4… hasta 30 s). `hook.start()` ahora devuelve
      bool; si es `False`, `app.py` no suena "ready", pasa a `State.ERROR` y `_periodic()`
      vigila la recuperación real. Verificado: recupera solo en 1,0 s tras el fallo forzado
      (caso 2 del mismo test)
- [x] **P0** `replace()` ya no confía a ciegas en `inserted_len`: si se da `verify_against`,
      selecciona hacia atrás, copia con Ctrl+C y compara contra lo que se cree que hay ahí
      **antes** de escribir encima. Si no coincide, colapsa la selección y aborta sin tocar
      el documento. `app.py` pasa `d.level0_text` (lo insertado la primera vez) como
      verificación. Verificado en `tools/test_keys.py` (casos 11-13): coincide → parcha;
      no coincide → aborta con el documento intacto; sin verificación → comportamiento
      viejo, para no romper otros llamadores

### Pendiente, por gravedad
- [ ] **P1** La detección de callback lento es **ciega por construcción**: `t0` se toma
      después de adquirir el GIL, así que la espera de GIL —lo único que puede comerse los
      300 ms— queda fuera. Medido: **p99 = 295 ms** de espera pura con la config exacta de
      WISPI, mientras la métrica propia marcaba 0,026 ms. C3.3 y C3.5 se aprueban con un
      instrumento que no ve el fallo que existen para detectar. El dato que lo arreglaría
      (`KBDLLHOOKSTRUCT.time`) ya está declarado y no se lee nunca
- [ ] **P1** Supresión del menú Inicio: el 0xE8 llega **12 ms tarde de mediana** (la
      resolución del temporizador es 15,6 ms) y el orden inverso de suelta **no está
      cubierto en absoluto**. C3.2 refutado con medición. Arreglo: emitirlo al ACTIVARSE el
      combo, no al soltarlo
- [ ] **P1** `maybe_reload()` **sustituye** los objetos de sección en vez de mutarlos, así
      que `hook._cfg` queda huérfano tras la primera recarga. Afecta igual a audio,
      injector y postprocess. C10.3 para esas secciones está declarado, no implementado
- [ ] **P1** Si un lote de `send_shift_left` falla a media ráfaga, la selección queda
      **viva** y la siguiente tecla del usuario la borra
- [ ] **P1** `_via_clipboard` sin `try/finally`: una excepción deja el portapapeles con el
      texto dictado y sin timer de restauración
- [ ] **P1** Carrera entre `_do_restore` y `_take_snapshot`: dos dictados seguidos pueden
      perder el portapapeles original
- [ ] **P1** `press()` no sabe si el destino es terminal: en Claude Code, "Copiar" (Ctrl+C)
      sin selección **interrumpe** y borra el prompt recién dictado; Ctrl+Z es EOF
- [ ] **P2** El diálogo de Estado muestra solo `p99_us` —la métrica ciega— y oculta
      `alive`, `hooked`, `cb_errors`, `n_rehooks`: semáforo en verde sobre un cadáver
- [ ] **P2** El vkCode de **todas** las teclas viaja por la cola y nadie lo usa: superficie
      de keylogger gratis. Los logs sí cumplen C9.2
- [ ] **P2** `EmptyClipboard` destruye todos los formatos; `restore()` solo repone texto:
      copiar una imagen y dictar la pierde
- [ ] **P2** `terminal_apps` no cubre mintty (Git Bash), openconsole, tabby, hyper ni el
      terminal integrado de VS Code

### Compuerta 1 no basta, y eso no tiene arreglo barato
En Chrome, VS Code, Obsidian y cualquier Electron, cambiar de **pestaña** con el ratón
mantiene el mismo `hwnd` y el mismo `pid`. La compuerta da verde sobre un documento
distinto. El arreglo real es anclar el cursor (`GetGUIThreadInfo`, caret rect) o verificar
lo insertado releyéndolo antes de parchear. Mientras tanto, el parche optimista tiene un
riesgo residual que no se puede cerrar con las cuatro compuertas actuales.

## Deuda y riesgos abiertos

- **Revisión externa con Codex** del SPEC completo, como pide el modo de trabajo
- **GPU**: subir el peldaño 2 exige `SubprocessASRBackend` primero, para que un crash de
  CUDA no se lleve por delante el hook de teclado. `get_cuda_device_count()` devuelve 1
  aunque falten las DLLs: **no es un gate válido**
- **Wispr Flow sigue instalado** en la máquina de referencia. Su base de datos local tiene
  histórico que se perdería; desinstalarlo solo cuando los 🔴 estén cerrados
