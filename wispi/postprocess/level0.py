"""Nivel 0: reglas deterministas. Sin red, sin modelo, sin estado.

POR QUE existe habiendo un LLM detras: el nivel 1 cuesta cientos de milisegundos y
puede no estar disponible; esto cuesta microsegundos y siempre esta. Cubre las cuatro
cosas que el ASR hace mal SIEMPRE (muletillas, espaciado, mayuscula inicial, jerga) y
por eso se aplica a todos los dictados, incluidos los que nunca llegan al LLM.
Presupuesto: p99 < 5 ms (criterio C7.3). Todos los regex se compilan al importar.

POR QUE PREFIERE EL FALSO NEGATIVO:
    "este", "bueno", "pues", "digamos", "sabes" y "like" son a la vez muletillas y
    palabras legitimas. Comerse una palabra buena ("este endpoint" -> "endpoint") es
    un error que el usuario NO ve venir y que le obliga a reescribir; dejar un "pues"
    suelto es un error que ve y que le cuesta una tecla. Por eso estas palabras solo
    se borran cuando la PUNTUACION delata que son muletilla: aisladas entre comas, o
    al principio de frase seguidas de coma. Fuera de esos dos moldes se quedan.
    Las unicas que se borran en cualquier posicion son las que no son palabras
    ("eh", "em", "mmm", "um", "uh") y "o sea", que en la practica nunca es literal.

POR QUE ESTE ORDEN (espaciado -> muletillas -> limpieza -> diccionario -> mayusculas):
    1. El espaciado va primero porque el detector de muletillas se apoya en las comas;
       con espacios dobles o " ," los moldes no casan.
    2. El diccionario va ANTES de capitalizar. Si se capitalizara primero, "n8n" a
       principio de frase seria "N8n" y "shadcn" seria "Shadcn"; despues del
       diccionario, en cambio, esos canonicos estan marcados como protegidos y la
       capitalizacion los salta. Es la unica forma de que camelCase y minusculas de
       marca sobrevivan.
    3. La segunda pasada de espaciado solo LIMPIA (colapsa, quita comas huerfanas);
       no vuelve a insertar espacios tras puntuacion, porque a esas alturas el texto
       ya contiene "Next.js" y "acme.ai" y una regla de "espacio tras el punto" los
       partiria.

POR QUE EL FUENTE ES ASCII PURO: los signos espanoles y las comillas tipograficas van
como escapes \\uXXXX. Este modulo se importa desde una consola Windows que puede estar
en cp1252; un literal acentuado ahi es un UnicodeDecodeError que no aporta nada.
"""
from __future__ import annotations

import re

from .dictionary import Dictionary

# --- alfabeto no-ASCII que necesitan los patrones ---------------------------
_INV_Q = "\u00bf"        # ?  invertido
_INV_B = "\u00a1"        # !  invertido
_LAQUO = "\u00ab"        # <<
_RAQUO = "\u00bb"        # >>
_LDQUO = "\u201c"        # comilla tipografica de apertura
_RDQUO = "\u201d"        # comilla tipografica de cierre
_ELLIP = "\u2026"        # puntos suspensivos en un solo caracter
_EMDASH = "\u2014"
_NBSP = "\u00a0"
_NNBSP = "\u202f"
_UPPER_ACCENTED = "\u00c0-\u00dd"   # mayusculas latinas acentuadas

_CLOSERS = f",.;:!?%\\)\\]{_RAQUO}{_RDQUO}{_ELLIP}"
_OPEN_SIGNS = f"{_INV_Q}{_INV_B}"
_SENT_END = f".!?{_ELLIP}"
_OPENERS = f"{_INV_Q}{_INV_B}\"'({_LAQUO}{_LDQUO}[-{_EMDASH}"

# --- espaciado --------------------------------------------------------------
_RE_WS = re.compile(f"[ \\t{_NBSP}{_NNBSP}]+")
_RE_NL = re.compile(r"[ \t]*\n[ \t]*")
# Espacio ANTES de puntuacion de cierre: siempre sobra.
_RE_SPACE_BEFORE_CLOSE = re.compile(f"[ \\t]+([{_CLOSERS}])")
# Espacio DESPUES de signo de apertura: siempre sobra.
_RE_SPACE_AFTER_OPEN = re.compile(f"([{_OPEN_SIGNS}\\({_LAQUO}{_LDQUO}\\[])[ \\t]+")
# Falta espacio antes de un signo de apertura espanol.
_RE_NEED_SPACE_BEFORE_OPEN = re.compile(f"(?<=[\\w,;:])([{_OPEN_SIGNS}])")
# Falta espacio tras coma. El [^\s\d] protege los decimales espanoles ("1,5").
_RE_NEED_SPACE_AFTER_COMMA = re.compile(r",(?=[^\s\d])")
# Falta espacio tras cierre de exclamacion/interrogacion/suspensivos.
_RE_NEED_SPACE_AFTER_BANG = re.compile(
    f"([!?{_ELLIP}])(?=[^\\s\\)\\]\"'{_RAQUO}{_RDQUO}!?.,;:])")
# Falta espacio tras punto. SOLO si le sigue MAYUSCULA: asi "Next.js", "acme.ai" y
# "3.5" quedan intactos, que es exactamente lo que hay que proteger.
_RE_NEED_SPACE_AFTER_DOT = re.compile(f"\\.(?=[A-Z{_UPPER_ACCENTED}{_OPEN_SIGNS}])")
# NO se toca ';' ni ':' porque "http://localhost:3000" los usa sin espacio y meter uno
# ahi rompe una URL dictada. Preferimos un ':' pegado antes que partir una URL.

# --- restos que deja el borrado de muletillas -------------------------------
_RE_DUP_COMMA = re.compile(r",(?:[ \t]*,)+")
_RE_COMMA_BEFORE_PUNCT = re.compile(f",[ \\t]*(?=[.;:!?{_ELLIP}])")
_RE_LEADING_JUNK = re.compile(r"^[\s,;:]+")
_RE_TRAILING_COMMA = re.compile(r"[ \t,;]+$")
_RE_OPEN_THEN_COMMA = re.compile(f"([{_OPEN_SIGNS}])[ \\t]*,[ \\t]*")

# --- muletillas -------------------------------------------------------------
# Grupo SEGURO: no son palabras de contenido en espanol ni en ingles. Se borran esten
# donde esten, arrastrando la coma/espacio que llevan detras.
_SAFE_FILLERS = (r"eh+|ehh+|em+|ehm+|hmm+|mmm+|mm+|um+|uhm+|uh+"
                 r"|este\s+este|o\s+sea|osea")
# "e," suelta: es como Whisper transcribe "eh" muy a menudo (medido en el corpus:
# "Eh, bueno, o sea..." -> "E, bueno, o sea...", y "...no responde, eh, cuando..." ->
# "...no responde e, cuando...").
#
# POR QUE ES SEGURO pese a que "e" es conjuncion legitima: la conjuncion SIEMPRE va
# seguida de un sustantivo ("padres e hijos"), nunca de una coma. Exigir la coma
# detras la descarta entera.
#
# EL CASO QUE SI HABRIA ROTO, y por eso el lookbehind: enumerar letras en voz alta
# ("opciones a, b, c, d, e, f") produce "e," legitimo. Se exige que delante haya dos
# caracteres de palabra, asi "responde e," entra y ", d, e," no.
_RE_E_INIT = re.compile(r"^[ \t]*e[ \t]*,[ \t]*", re.IGNORECASE)
_RE_E_MID = re.compile(r"(?<=\w\w)[ \t]+e[ \t]*,[ \t]*", re.IGNORECASE)
_RE_SAFE = re.compile(rf"(?<!\w)(?:{_SAFE_FILLERS})(?!\w)[ \t]*,?[ \t]*", re.IGNORECASE)

# Grupo CONSERVADOR: palabras legitimas. Solo mueren dentro de un molde de puntuacion.
_SOFT_FILLERS = r"este|pues|digamos|sabes|like|you\s+know|ya\s+sabes"
# a) aisladas entre comas -> ", este, " desaparece entera, comas incluidas
_RE_SOFT_MID = re.compile(rf",[ \t]*(?:{_SOFT_FILLERS})[ \t]*,[ \t]*", re.IGNORECASE)
# b) al principio de frase y seguidas de coma. "bueno" SOLO entra por aqui (spec).
#
# EXCEPCION DE COPULA, y ahora en las DOS ramas (con coma y sin ella). Antes vivia
# solo en la rama sin coma (b-bis), y eso dejaba pasar el falso positivo que cazo el
# corpus:
#   "Bueno es el adjetivo correcto" -> Whisper puntua la pausa -> "Bueno, es el..."
#   -> entraba por AQUI, la excepcion no le llegaba, y se comia el sujeto.
# Cuando detras viene una copula, "bueno" es atributo, no muletilla. Respetarlo cuesta
# dejar sin limpiar algun "bueno, es que..." real: exactamente el falso negativo que
# este modulo prefiere pagar.
_NO_COPULA = (r"(?![ \t]*,?[ \t]*(?:es|era|sera|será|fue|seria|sería"
              r"|son|eran)\b)")
_SOFT_INIT = rf"{_SOFT_FILLERS}|bueno{_NO_COPULA}"
_RE_SOFT_INIT_TEXT = re.compile(rf"^[ \t]*(?:{_SOFT_INIT})[ \t]*,[ \t]*", re.IGNORECASE)
_RE_SOFT_INIT_SENT = re.compile(
    f"([.!?{_ELLIP}][ \\t]+|\\n[ \\t]*)(?:{_SOFT_INIT})[ \\t]*,[ \\t]*", re.IGNORECASE)
# b-bis) "bueno" abriendo el dictado SIN coma detras. Whisper no siempre puntua la
# pausa, y "bueno necesito que revises..." se queda con la muletilla puesta si se
# exige la coma. En esa posicion "bueno" es muletilla salvo cuando es atributo de una
# copula ("bueno es saberlo"), que es el unico uso literal realista ahi: se exceptua.
# El resto de blandas NO entra aqui: "pues claro que si" y "este endpoint" son frases
# normales que empiezan igual, y comerselas seria el falso positivo que se evita.
_RE_BUENO_INIT = re.compile(
    r"^[ \t]*bueno[ \t]+(?!es\b|era\b|sera\b|ser\u00e1\b|fue\b|seria\b|ser\u00eda\b)",
    re.IGNORECASE)

# c) coletillas finales. Exigen coma o '?' invertido delante: sin ese requisito,
#    "funciona o no?" perderia el "no" y se convertiria en otra pregunta. Ese falso
#    positivo es peor que dejar la coletilla puesta.
_RE_TAG = re.compile(
    f"(?<=\\w)[ \\t]*(?:,[ \\t]*{_INV_Q}?[ \\t]*|{_INV_Q}[ \\t]*)"
    r"(?:no|sabes|verdad|cierto|you\s+know)[ \t]*\?",
    re.IGNORECASE)

_STRIP_FROM_TOKEN = f".,;:!?)\"'{_RAQUO}{_RDQUO}{_ELLIP}"


def fix_spacing(text: str, *, insert: bool = True) -> str:
    """Normaliza espacios y puntuacion.

    `insert=False` deja la pasada en modo limpieza: colapsa y quita sobrantes, pero no
    inserta espacios tras la puntuacion. Es el modo que se usa DESPUES del diccionario,
    cuando el texto ya contiene canonicos con punto dentro ("Next.js").
    """
    if not text:
        return text
    t = _RE_NL.sub("\n", text)
    t = _RE_WS.sub(" ", t)
    t = _RE_SPACE_BEFORE_CLOSE.sub(r"\1", t)
    t = _RE_SPACE_AFTER_OPEN.sub(r"\1", t)
    t = _RE_DUP_COMMA.sub(",", t)
    t = _RE_COMMA_BEFORE_PUNCT.sub("", t)
    t = _RE_OPEN_THEN_COMMA.sub(r"\1", t)
    if insert:
        t = _RE_NEED_SPACE_AFTER_COMMA.sub(", ", t)
        t = _RE_NEED_SPACE_AFTER_BANG.sub(r"\1 ", t)
        t = _RE_NEED_SPACE_AFTER_DOT.sub(". ", t)
        t = _RE_NEED_SPACE_BEFORE_OPEN.sub(r" \1", t)
        t = _RE_WS.sub(" ", t)
        t = _RE_SPACE_BEFORE_CLOSE.sub(r"\1", t)
    t = _RE_LEADING_JUNK.sub("", t)
    t = _RE_TRAILING_COMMA.sub("", t)
    return t.strip()


def strip_fillers(text: str) -> str:
    """Borra muletillas. Ver el docstring del modulo: falso negativo > falso positivo."""
    if not text:
        return text
    t = _RE_TAG.sub(".", text)
    t = _RE_SAFE.sub("", t)
    # El "E," inicial va ANTES del bucle y una sola vez: si sobrevive, desplaza a la
    # muletilla siguiente fuera de la posicion inicial y el molde de INICIO ya no la
    # alcanza. Es el efecto domino que dejaba "E, bueno, lo que quiero..." intacto.
    t = _RE_E_INIT.sub("", t)
    t = _RE_E_MID.sub(" ", t)
    # Dos vueltas, y el molde de INICIO antes que el de EN MEDIO. Las muletillas vienen
    # en racimo ("pues, digamos, es lo mismo") y las dos reglas compiten por la misma
    # coma: si el molde de en medio se lleva ", digamos," primero, el "pues," inicial
    # se queda sin la coma que lo delataba y sobrevive. Dando prioridad al inicial y
    # repitiendo, el racimo se deshace de fuera hacia dentro.
    for _ in range(2):
        t = _RE_SOFT_INIT_SENT.sub(r"\1", t)
        t = _RE_SOFT_INIT_TEXT.sub("", t)
        t = _RE_BUENO_INIT.sub("", t)
        # Se comen las DOS comas, no una: la muletilla era lo unico que separaba, asi
        # que dejar la coma ("es, el de arriba") seria dejar una cicatriz nuestra.
        t = _RE_SOFT_MID.sub(" ", t)
    return t


def capitalize(text: str, protected: frozenset[str] = frozenset()) -> str:
    """Mayuscula al principio de cada frase, saltando los canonicos protegidos.

    Recorre caracter a caracter en vez de partir por un regex de frases porque el
    limite de frase no es "un punto": es "un punto seguido de espacio o de fin". Sin
    esa condicion, "Next.js" se convierte en "Next.Js".
    """
    if not text:
        return text
    chars = list(text)
    n = len(chars)
    at_start = True
    i = 0
    while i < n:
        ch = chars[i]
        if at_start:
            if ch.isspace() or ch in _OPENERS:
                i += 1
                continue
            if ch.isalpha():
                if protected:
                    j = i
                    while j < n and not chars[j].isspace():
                        j += 1
                    token = "".join(chars[i:j]).rstrip(_STRIP_FROM_TOKEN)
                    if token in protected:
                        at_start = False
                        i = j
                        continue
                up = ch.upper()
                if len(up) == 1:  # 'ss' alemana: .upper() mide 2 y cambiaria la longitud
                    chars[i] = up
            at_start = False
            i += 1
            continue
        if ch == "\n":
            at_start = True
        elif ch in _SENT_END:
            # Solo es fin de frase si detras hay espacio o se acabo el texto. Esta
            # condicion es la que salva "Next.js", "acme.ai" y "3.5".
            if i + 1 >= n or chars[i + 1].isspace():
                at_start = True
        i += 1
    return "".join(chars)


def run(text: str, *,
        do_fillers: bool = True,
        do_spacing: bool = True,
        do_dictionary: bool = True,
        do_capitalize: bool = True,
        dictionary: Dictionary | None = None) -> str:
    """Pipeline completo de nivel 0. El orden esta justificado en el docstring."""
    if not text:
        return ""
    t = text
    if do_spacing:
        t = fix_spacing(t, insert=True)
    if do_fillers:
        t = strip_fillers(t)
        if do_spacing:
            t = fix_spacing(t, insert=False)
    protected: frozenset[str] = frozenset()
    if do_dictionary and dictionary is not None:
        t = dictionary.apply(t)
        protected = dictionary.protected
        if do_spacing:
            t = fix_spacing(t, insert=False)
    if do_capitalize:
        t = capitalize(t, protected)
    return t.strip()
