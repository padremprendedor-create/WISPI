"""Pruebas del nivel 0. Rapidas (sin ASR, sin red): corren en menos de un segundo.

POR QUE APARTE DEL e2e
    El e2e mide el sistema con audio real y tarda un minuto. Estas son
    transformaciones de cadena puras y su modo de fallo tipico es una regresion
    silenciosa al tocar un regex. Merecen un test directo que se pueda correr en
    cada edicion.

EL CONTRATO QUE SE VERIFICA (del docstring de level0.py):
    - Muletillas SEGURAS (no son palabras): caen SIEMPRE, esten donde esten.
    - Muletillas BLANDAS (son palabras legitimas): caen SOLO cuando la puntuacion
      las delata. Sin comas alrededor se quedan, y eso es CORRECTO, no un fallo:
      "pues digamos que si" es espanol normal.
    - Ningun falso positivo. Comerse una palabra buena es peor que dejar una
      muletilla, porque el usuario no lo ve venir.

    uv run python tools/test_level0.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wispi.postprocess.level0 import run, strip_fillers  # noqa: E402

# (etiqueta, entrada, comprobacion)
#   "==" -> igualdad exacta      "in" -> tiene que contener      "!in" -> no puede contener
CASOS: list[tuple[str, str, str, str]] = [
    # --- SEGURAS: caen siempre -------------------------------------------
    ("segura eh inicial",      "Eh, necesito revisar el deploy.",   "!in", "Eh,"),
    ("segura o sea",           "El endpoint, o sea, no responde.",  "!in", "o sea"),
    ("segura mmm",             "Mmm, abre el archivo.",             "!in", "Mmm"),
    ("segura este este",       "Este este endpoint falla.",         "!in", "este este"),

    # --- "e," que en realidad es "eh" ------------------------------------
    ("e inicial",              "E, bueno, lo que quiero es esto.",  "!in", "E,"),
    ("e en medio",             "No responde e, cuando hay carga.",  "!in", " e,"),

    # --- REGRESION: enumerar letras NO puede perder la "e" ---------------
    # Este es el caso que casi rompe la regla anterior. Un dev dictando
    # "opciones a, b, c, d, e, f" tiene que conservarlas todas.
    ("letras enumeradas",      "Las opciones son a, b, c, d, e, f.", "in", "d, e, f"),

    # --- BLANDAS con puntuacion: caen ------------------------------------
    ("blanda entre comas",     "El deploy, digamos, tardo mucho.",  "!in", "digamos"),
    ("blanda inicial + coma",  "Pues, abre el archivo.",            "!in", "Pues,"),
    ("bueno inicial + coma",   "Bueno, necesito el reporte.",       "!in", "Bueno,"),
    ("bueno inicial sin coma", "Bueno necesito el reporte.",        "!in", "Bueno "),

    # --- BLANDAS sin puntuacion: SE QUEDAN, y esta bien ------------------
    ("este demostrativo",      "Este endpoint devuelve un error.",  "in",  "Este endpoint"),
    ("pues digamos que",       "Pues digamos que no responde.",     "in",  "digamos"),
    ("pues claro",             "Pues claro que funciona.",          "in",  "Pues claro"),

    # --- FALSOS POSITIVOS que hay que evitar -----------------------------
    ("bueno copula",           "Bueno es el adjetivo correcto.",    "in",  "Bueno es"),
    ("bueno copula con coma",  "Bueno, es el adjetivo correcto.",   "in",  "Bueno"),
    ("este otro",              "Este otro funciona bien.",          "in",  "Este otro"),
    ("conjuncion e",           "Son padres e hijos.",               "in",  "padres e hijos"),
    ("coletilla vs pregunta",  "Funciona o no?",                    "in",  "no"),

    # --- coletillas finales ----------------------------------------------
    ("coletilla sabes",        "El deploy salio bien, sabes?",      "!in", "sabes"),
    ("coletilla verdad",       "Ya esta desplegado, verdad?",       "!in", "verdad"),
]


def main() -> int:
    ancho = max(len(c[0]) for c in CASOS)
    fallos = []
    for etiqueta, entrada, modo, esperado in CASOS:
        got = strip_fillers(entrada)
        if modo == "==":
            ok = got == esperado
        elif modo == "in":
            ok = esperado.lower() in got.lower()
        else:
            ok = esperado.lower() not in got.lower()
        marca = "ok  " if ok else "FALLA"
        if not ok:
            fallos.append((etiqueta, entrada, got, modo, esperado))
        print(f"  {marca} {etiqueta:<{ancho}}  {entrada!r}\n       -> {got!r}")

    print()
    if fallos:
        print(f"{len(fallos)} FALLOS:")
        for etiqueta, entrada, got, modo, esperado in fallos:
            rel = {"==": "deberia ser", "in": "deberia contener",
                   "!in": "NO deberia contener"}[modo]
            print(f"  - {etiqueta}: {rel} {esperado!r}")
            print(f"      entrada: {entrada!r}")
            print(f"      salida : {got!r}")
        return 1
    print(f"{len(CASOS)}/{len(CASOS)} OK")

    # Latencia: criterio C7.3 del SPEC, p99 < 5 ms.
    import time
    frases = [c[1] for c in CASOS] * 20
    t0 = time.perf_counter()
    for f in frases:
        run(f)
    ms = (time.perf_counter() - t0) * 1000 / len(frases)
    print(f"nivel 0 completo: {ms:.3f} ms/frase sobre {len(frases)} frases "
          f"{'OK' if ms < 5 else 'FALLA (C7.3)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
