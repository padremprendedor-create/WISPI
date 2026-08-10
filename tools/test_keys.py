"""Comprueba que el teclado tactil escribe DE VERDAD en otra aplicacion.

POR QUE CROSS-PROCESS
    Mandar una tecla y ver que SendInput devuelve exito no demuestra nada: el
    injector reporta exito siempre que Windows acepte los eventos, aunque acaben
    en otra ventana o no produzcan nada. Lo unico que vale es leer el contenido
    de la ventana destino y comparar.

GUARDA OBLIGATORIA
    Antes de cada tanda se comprueba que la ventana en foco es la diana. Si no lo
    es, se aborta. Sin eso, este test le estaria escribiendo Enters y retrocesos
    a las ventanas REALES del usuario -y un retroceso repetido en el sitio
    equivocado borra su trabajo.

    uv run python tools/test_keys.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wispi import logging_setup, winapi                    # noqa: E402
from wispi.config import Config                            # noqa: E402
from wispi.inject.injector import Injector                 # noqa: E402
from wispi.inject.target import current_target             # noqa: E402

sys.path.insert(0, str(ROOT / "tools"))
from e2e_pipeline import TargetHarness                     # noqa: E402

CTRL = (winapi.VK_CONTROL,)
SHIFT = (winapi.VK_SHIFT,)


def main() -> int:
    logging_setup.setup("WARNING", console=True)
    cfg = Config.load()
    inj = Injector(cfg.injection, logging_setup.get("inject"))

    h = TargetHarness()
    h.start()
    fallos: list[str] = []

    def preparar(texto: str) -> bool:
        h.clear()
        if not h.ensure_focus():
            t = current_target()
            fallos.append(f"sin foco (lo tiene {t.exe!r}); no se manda nada")
            return False
        if texto:
            inj.insert(texto, current_target())
            time.sleep(0.35)
        return True

    def comprobar(nombre: str, esperado: str) -> None:
        got = h.read()
        if got == esperado:
            print(f"  ok    {nombre:<26} -> {got!r}")
        else:
            fallos.append(f"{nombre}: esperado {esperado!r}, recibido {got!r}")
            print(f"  FALLA {nombre:<26} esperado {esperado!r} recibido {got!r}")

    try:
        print("Teclado tactil, contra la ventana diana:\n")

        # 1. Retroceso borra un caracter, igual que la tecla fisica.
        if preparar("hola mundo"):
            inj.press(winapi.VK_BACK, (), 1, "Retroceso")
            time.sleep(0.3)
            comprobar("Retroceso x1", "hola mund")

        # 2. Retroceso repetido: es como se borra una palabra manteniendolo.
        if preparar("hola mundo"):
            inj.press(winapi.VK_BACK, (), 5, "Retroceso x5")
            time.sleep(0.4)
            comprobar("Retroceso x5", "hola ")

        # 3. Enter mete salto de linea. Es LA tecla que faltaba para poder
        #    enviar un mensaje sin tocar el teclado fisico.
        if preparar("linea uno"):
            inj.press(winapi.VK_RETURN, (), 1, "Enter")
            time.sleep(0.3)
            h_txt = h.read()
            if h_txt == "linea uno\n":
                print(f"  ok    {'Enter':<26} -> {h_txt!r}")
            else:
                fallos.append(f"Enter: esperado 'linea uno\\n', recibido {h_txt!r}")
                print(f"  FALLA Enter -> {h_txt!r}")

        # 4. Inicio + Supr: navegar y borrar por delante.
        if preparar("XYZ resto"):
            inj.press(winapi.VK_HOME, (), 1, "Inicio")
            time.sleep(0.2)
            inj.press(winapi.VK_DELETE, (), 4, "Supr x4")
            time.sleep(0.35)
            comprobar("Inicio + Supr x4", "resto")

        # 5. Seleccionar con Shift+flecha y escribir encima.
        #    Es el flujo de "elegir que borrar" sin teclado fisico.
        if preparar("borra ESTO"):
            inj.press(winapi.VK_LEFT, SHIFT, 4, "sel izq x4")
            time.sleep(0.3)
            inj.press(winapi.VK_BACK, (), 1, "Retroceso")
            time.sleep(0.3)
            comprobar("Shift+izq x4 + Retroceso", "borra ")

        # 6. Ctrl+A selecciona todo, y lo siguiente que se escriba lo reemplaza.
        #
        #    SE COMPRUEBA ASI Y NO CON RETROCESO por una rareza del destino, no
        #    del codigo: el Text de Tk no borra la seleccion con Retroceso (solo
        #    quita el caracter anterior al cursor). Medirlo de esa forma daba
        #    "no funciona" con el codigo correcto.
        #
        #    Reemplazar SI demuestra las dos cosas de golpe: que la combinacion
        #    Ctrl+A llega, y que habia una seleccion viva cuando entro el texto.
        if preparar("todo esto sobra"):
            inj.press(winapi.VK_A, CTRL, 1, "Ctrl+A")
            time.sleep(0.25)
            inj.insert(">", current_target())
            time.sleep(0.3)
            comprobar("Ctrl+A + escribir (reemplaza)", ">")

        # 7. Simbolos, incluidos los que no estan en el teclado ingles.
        if preparar(""):
            for s in ("@", "¿", "€", "\\", "{", "…"):
                inj.insert(s, current_target())
                time.sleep(0.18)
            comprobar("simbolos", "@¿€\\{…")

        # 8. Flechas extendidas con Bloq Num: sin KEYEVENTF_EXTENDEDKEY, una
        #    flecha izquierda escribiria un "4" del teclado numerico.
        if preparar("AB"):
            inj.press(winapi.VK_LEFT, (), 1, "izquierda")
            time.sleep(0.2)
            inj.insert("-", current_target())
            time.sleep(0.3)
            comprobar("flecha izq (no escribe 4)", "A-B")

        # 9. GUARDA DE DESTINO: con un destino que ya no tiene el foco, NO se
        #    escribe nada. Es el arreglo del P0 que encontro la revision
        #    adversarial: una rafaga de Retroceso encolada seguia borrando en la
        #    ventana a la que el usuario acababa de cambiar.
        if preparar("no me toques"):
            from wispi.inject.target import Target
            falso = Target(hwnd=0x7FFFFFFF, pid=999999, exe="fantasma.exe", title="")
            r1 = inj.press(winapi.VK_BACK, (), 5, "Retroceso", falso)
            r2 = inj.insert("intruso", falso)
            time.sleep(0.35)
            got = h.read()
            if r1.ok or r2.ok:
                fallos.append(f"la guarda de destino no aborto (press={r1.ok}, "
                              f"insert={r2.ok})")
            elif got != "no me toques":
                fallos.append(f"se escribio pese a la guarda: {got!r}")
            else:
                print(f"  ok    {'guarda de destino':<26} -> abortado, texto intacto")

        # 10. TOKEN DE GENERACION: lo encolado por una rafaga ya soltada se
        #     descarta en vez de aplicarse tarde.
        if preparar("intacto"):
            gen = inj.bump_generation()
            inj.bump_generation()            # el dedo se levanta: cambia el token
            r = inj.press(winapi.VK_BACK, (), 5, "Retroceso", None, gen)
            time.sleep(0.3)
            got = h.read()
            if r.ok or got != "intacto":
                fallos.append(f"la rafaga cancelada se aplico igual: ok={r.ok} {got!r}")
            else:
                print(f"  ok    {'rafaga cancelada':<26} -> descartada, texto intacto")

        # 11. VERIFICACION DEL PARCHE: si la seleccion coincide con lo esperado,
        #     procede. `inserted_len` solo prueba que SendInput acepto los
        #     eventos del primer Ctrl+V, no que la app pegara exactamente eso;
        #     por eso replace() ahora relee la seleccion antes de escribir
        #     encima. Este caso es el camino feliz: coincide, y el parche entra.
        if preparar("hola mundo"):
            r = inj.replace(len("hola mundo"), "adios mundo",
                            current_target(), verify_against="hola mundo")
            time.sleep(0.3)
            got = h.read()
            if not r.ok or got != "adios mundo":
                fallos.append(f"parche verificado no aplico: ok={r.ok} {got!r}")
            else:
                print(f"  ok    {'parche: verificacion OK':<26} -> {got!r}")

        # 12. VERIFICACION DEL PARCHE: si NO coincide, se aborta SIN TOCAR el
        #     documento. Es el P0 de la revision adversarial: antes replace()
        #     confiaba a ciegas en old_len y podia comerse texto real si la app
        #     habia pegado menos de lo esperado (paste bloqueado, maxlength,
        #     autocorreccion, un salto de linea que en Win32 cuenta como CRLF).
        #     Aqui se simula ese desfase con un verify_against que no es lo que
        #     hay de verdad en el documento.
        if preparar("hola mundo"):
            r = inj.replace(len("hola mundo"), "NUEVO",
                            current_target(), verify_against="esto no es lo que hay")
            time.sleep(0.3)
            got = h.read()
            if r.ok:
                fallos.append("el parche con verificacion fallida se aplico igual")
            elif got != "hola mundo":
                fallos.append(f"el documento cambio pese al aborto: {got!r}")
            else:
                print(f"  ok    {'parche: verificacion FALLA':<26} -> "
                      f"abortado, documento intacto ({got!r})")

            # Y la seleccion tiene que haber quedado colapsada, no viva: un
            # toque inocuo despues NO puede borrar nada. replace() ya manda un
            # VK_RIGHT al abortar (sin Shift, para soltar la seleccion) - NO
            # se manda uno adicional aqui: en un control Win32 real eso
            # colocaria el cursor en el borde derecho, pero el Text de Tk de
            # esta diana no seleccion sigue esa convencion (la misma rareza
            # que ya obligo a probar Ctrl+A reemplazando en vez de borrando:
            # Tk no colapsa al extremo derecho como un Edit nativo). Por eso
            # se comprueba por MULTICONJUNTO y no por posicion: si el "!" se
            # quito y lo que queda es EXACTAMENTE el multiconjunto de
            # caracteres de "hola mundo", no se perdio ni se sobrescribio
            # nada, sea cual sea el punto donde Tk decidiera dejar el cursor.
            inj.insert("!", current_target())
            time.sleep(0.3)
            got2 = h.read()
            resto = got2.replace("!", "", 1)
            if "!" not in got2 or sorted(resto) != sorted("hola mundo"):
                fallos.append(f"se perdio o se sobrescribio texto original tras "
                              f"el aborto: {got2!r}")
            else:
                print(f"  ok    {'seleccion colapsada tras aborto':<26} -> "
                      f"{got2!r} (sin perder caracteres)")

        # 13. Sin verify_against (None) el comportamiento es el de antes: se
        #     parchea sin comprobar. Confirma que no se rompio el caso donde un
        #     llamador no lo pasa.
        if preparar("hola mundo"):
            r = inj.replace(len("hola mundo"), "sin verificar", current_target())
            time.sleep(0.3)
            got = h.read()
            if not r.ok or got != "sin verificar":
                fallos.append(f"parche sin verify_against no aplico: ok={r.ok} {got!r}")
            else:
                print(f"  ok    {'parche sin verify_against':<26} -> {got!r}")

    finally:
        h.stop()

    print()
    if fallos:
        print(f"{len(fallos)} FALLOS:")
        for f in fallos:
            print(f"  - {f}")
        return 1
    print("teclado tactil OK: retroceso, Enter, Supr, navegacion, seleccion y simbolos "
          "llegan a otra aplicacion")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
