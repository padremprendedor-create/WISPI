# Cómo contribuir a WISPI

Gracias por mirar. Antes de nada, lo que más ayuda no es una PR: es **decir qué te
falló, en qué app y con qué máquina**. WISPI se probó a fondo en una sola máquina y
con una sola voz. Cada reporte de alguien distinto es información que aquí no existe.

---

## Lo primero: WISPI tiene un objetivo escrito

Está en [SPEC.md](SPEC.md), y no es "hacer una app de dictado". Es que quien lo use
pueda desinstalar Wispr Flow y no lo eche de menos. Ese criterio decide los empates.

Una función elegante que no acerque a eso probablemente no entre, aunque esté bien
hecha. No es nada personal contra tu idea: es que un dictado que tarda 100 ms más
molesta todos los días, y una función que usas una vez al mes no lo compensa.

En [SPEC.md](SPEC.md) §2 está la lista explícita de lo que quedó **fuera de alcance**.
Si tu propuesta está ahí, abre igualmente el issue, pero di por qué debería cambiar la
decisión.

## Reportar un fallo

Lo que hace un reporte útil:

1. **La salida del diagnóstico.** Es lo que más ahorra:
   ```bash
   uv run python -m wispi.selftest --all
   ```
2. **La app concreta** donde falló (nombre del `.exe`, no "mi terminal") y si la
   ventana estaba elevada.
3. **Tu máquina**: versión de Windows, CPU, si el teclado es español u otro.
4. **Qué esperabas y qué salió.** Si es un problema de transcripción, pega el texto que
   salió y el que debía salir.

**No pegues logs sin mirarlos.** Si alguna vez activaste `logging.include_text: true`,
tus logs contienen lo que dictaste.

Para fallos de **seguridad**, no abras un issue: ver [SECURITY.md](SECURITY.md).

## Antes de mandar una PR

### Corre las pruebas que no necesitan voz

```bash
uv run python tools/test_level0.py      # reglas de nivel 0 y diccionario
uv run python tools/e2e_pipeline.py     # pipeline completo sobre el corpus
uv run python -m wispi.selftest --all   # diagnóstico por partes
```

`e2e_pipeline.py` necesita el corpus. Si no lo tienes, genéralo (requiere `piper-tts`
y una voz española; ver el docstring de `tools/make_corpus.py`):

```bash
python tools/make_corpus.py
```

### Si tocas el diccionario o el nivel 0

`e2e_pipeline.py` es la red de seguridad. Un cambio que baje `C7.1` (términos de jerga
escritos correctamente) o que rompa `TRAMPAS` (falsos positivos) no entra.

Y la regla del diccionario no se negocia: **ninguna variante puede ser una palabra
española corriente**. Un falso positivo se come una palabra buena sin que el usuario lo
vea venir; un falso negativo solo deja un "comit" sin arreglar.

### Si tocas el `initial_prompt`

Mídelo. El formato del prompt cambia el comportamiento del decoder más que su contenido
—una lista en vez de prosa nos costó el 23 % del texto— y **eso no se ve leyendo el
código**. Ver la sección correspondiente del [README](README.md#el-formato-del-prompt-importa-más-que-su-contenido).

### Si tocas `hotkey.py` o `inject/`

Son los dos puntos de fallo del proyecto y los dos sitios donde una PR puede romper algo
que no se nota en las pruebas:

- **`hotkey.py`**: el callback tiene un presupuesto de 300 ms de Windows y un objetivo
  interno de < 0,5 ms. Si metes trabajo dentro del callback, Windows desengancha el hook
  **en silencio**. Todo lo que no sea `put_nowait` va en otro hilo.
- **`inject/`**: la inyección va donde esté el foco. Un fallo aquí no da un error: escribe
  en la ventana equivocada. Las cuatro compuertas del parche optimista (mismo HWND y PID,
  < 1,5 s, cero teclas del usuario, app fuera de `no_patch_apps`) están para eso y no se
  relajan.

Cambios en estas dos zonas necesitan verificación humana con voz y apps reales. Dilo en
la PR: qué probaste, en qué apps, cuántas veces.

## Estilo

- **Español en comentarios y documentación**, ASCII en el código fuente (sin tildes en
  comentarios de `.py`: evita sorpresas de codepage en consolas de Windows).
- Los comentarios explican **por qué**, no qué. El repo está lleno de decisiones que
  parecen raras hasta que lees el motivo; mantén esa costumbre.
- Sin dependencias nuevas salvo que no haya alternativa. Cada una es algo más que puede
  romper el arranque en la máquina de otro.
- Las versiones van clavadas (`==`), no en rango. Es deliberado.

## Lo que se acepta con más ganas

- **Soporte de otro idioma.** Hoy el diccionario y las reglas de muletillas son de
  español. La arquitectura no lo impide; nadie lo ha hecho.
- **Rutas de inyección para apps que fallan.** Si encuentras una app donde ni `Ctrl+V`
  ni `Shift+Insert` ni Unicode funcionan, eso es oro.
- **Verificación en hardware distinto.** Otra CPU, otro micro, otro teclado. Corre
  `uv run python -m wispi.bench --analyze` y cuenta qué salió.
- Las ideas de la sección "Siguiente" del [ROADMAP](ROADMAP.md).

## Lo que probablemente no

- Instalador, autoupdate, firma de código. Ver [SPEC.md](SPEC.md) §2.
- Nube, cuentas, sincronización. El proyecto entero existe para no tener eso.
- Refactors grandes sin un fallo detrás.

---

Al contribuir aceptas que tu aportación se publique bajo la licencia
[MIT](LICENSE) del proyecto.
