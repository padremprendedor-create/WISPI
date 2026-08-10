"""Filtro de alucinaciones de Whisper sobre audio mudo o casi mudo.

POR QUE hace falta aunque ya haya VAD y umbral de RMS: el encoder de Whisper rellena
siempre a una ventana de 30 s y el decoder tiene que emitir ALGO. Sobre silencio o
ruido de fondo emite lo que mas vio en su corpus de entrenamiento: creditos de
subtitulos de YouTube. En espanol, casi siempre "Subtitulos realizados por la
comunidad de Amara.org". El umbral de energia (C6.2) atrapa el silencio limpio; esto
atrapa lo que se cuela igualmente: un carraspeo, el ventilador, media palabra.

La lista base viene de un proyecto anterior y solo cubria ingles. Aqui se amplia
con el repertorio en espanol y se cambia la comparacion: alli era `lower()` a
secas, que falla con "Subtitulos" vs "Subtitulos" acentuado; aqui se pliegan
tildes y se quita la puntuacion antes de comparar.

POR QUE tambien detecta repeticion: sin `condition_on_previous_text` el decoder
todavia entra en bucle de vez en cuando y devuelve la misma palabra N veces
("gracias gracias gracias gracias"). Eso no esta en ninguna lista fija; se detecta
contando tokens unicos.
"""
from __future__ import annotations

import re

from .dictionary import fold

# Frases exactas. Se comparan ya normalizadas: minusculas, sin tildes, sin puntuacion
# final y con los espacios colapsados. Por eso van aqui en ASCII y sin punto final.
HALLUCINATIONS: frozenset[str] = frozenset({
    # --- ingles ---
    "thank you",
    "thanks",
    "thanks for watching",
    "thank you for watching",
    "subscribe to my channel",
    "like and subscribe",
    "please subscribe",
    "bye",
    "you",
    "the end",
    "okay",
    "ok",
    # --- espanol (lo que este micro produce de verdad) ---
    "subtitulos realizados por la comunidad de amara.org",
    "subtitulado por la comunidad de amara.org",
    "subtitulos realizados por la comunidad de amara org",
    "subtitulos por la comunidad de amara.org",
    "subtitulos realizados por la comunidad",
    "mas informacion en amara.org",
    "amara.org",
    "gracias",
    "muchas gracias",
    "gracias por ver el video",
    "gracias por ver este video",
    "gracias por su atencion",
    "suscribete al canal",
    "suscribanse al canal",
    "no olvides suscribirte",
    "dale like y suscribete",
    "mas videos en",
    "hasta la proxima",
    "nos vemos en el proximo video",
    "adios",
    # DELIBERADAMENTE FUERA: "si" y "no". Whisper tambien los alucina, pero un "no"
    # dictado de verdad es una respuesta legitima en un chat, y borrarla seria el
    # peor fallo posible. Se prefiere dejar pasar la alucinacion.
    # --- otros idiomas que Whisper cuela sobre silencio ---
    "sous-titres",
    "sous-titres realises par la communaute d'amara.org",
    "sottotitoli creati dalla comunita amara.org",
    "untertitel von stephanie geiges",
    "www.mooji.org",
})

# Cadenas que, si aparecen dentro del texto, ya lo delatan. Un dictado real no las
# contiene jamas, ni siquiera de pasada.
_SUBSTRINGS: tuple[str, ...] = (
    "amara.org",
    "amara org",
    "subtitulos realizados por",
    "subtitulado por la comunidad",
)

# "www." NO puede ir en _SUBSTRINGS: "mira la web www.ejemplo.com cuando puedas" es un
# dictado perfectamente real y borrarlo seria imperdonable. La alucinacion es la URL
# SUELTA, sin frase alrededor; por eso se exige que el texto entero quepa en dos
# palabras.
URL_ALONE_MAX_TOKENS = 2

# Patron de texto que es solo puntuacion, muletillas sueltas o basura equivalente.
# Solo se evalua sobre textos cortos (ver ONLY_JUNK_MAX_CHARS): con la alternancia
# y el '+' anclados, un texto largo que no casa provoca backtracking caro, y aqui hay
# un presupuesto de 5 ms que respetar.
_RE_ONLY_JUNK = re.compile(
    r"^(?:thank you|thanks|gracias|bye|adios|you|ok|okay|eh|em|mmm|uh|um"
    r"|[\W\d_])+$",
    re.IGNORECASE)
ONLY_JUNK_MAX_CHARS = 64

_RE_PUNCT_EDGES = re.compile(r"^[^\w]+|[^\w]+$")
_RE_WORD_SPLIT = re.compile(r"[^\w]+")

# Un texto de >= REPEAT_MIN_TOKENS palabras con <= N raices distintas es el bucle
# clasico del decoder. Los umbrales son deliberadamente altos: "no no no" enfadado es
# lenguaje real y no se descarta.
REPEAT_MIN_TOKENS = 4
REPEAT_LONG_TOKENS = 8


def normalize(text: str) -> str:
    """Forma canonica para comparar: minusculas, sin tildes, sin puntuacion en los
    bordes y con los espacios colapsados."""
    t = fold(text).lower().strip()
    t = _RE_PUNCT_EDGES.sub("", t)
    return " ".join(t.split())


def _is_repetition(tokens: list[str]) -> bool:
    n = len(tokens)
    if n < REPEAT_MIN_TOKENS:
        return False
    unique = set(tokens)
    if len(unique) == 1:
        return True
    return n >= REPEAT_LONG_TOKENS and len(unique) <= 2


def is_whisper_hallucination(transcript: str) -> bool:
    """True si el transcrito es basura conocida y debe descartarse sin insertar nada.

    Contrato: solo devuelve True cuando esta razonablemente seguro. Un falso positivo
    aqui borra un dictado real del usuario, que es el peor fallo posible de WISPI; un
    falso negativo solo mete una frase rara que el usuario borra con Ctrl+Z.
    """
    if not transcript or not transcript.strip():
        return True

    cleaned = normalize(transcript)
    if not cleaned:
        return True

    if cleaned in HALLUCINATIONS:
        return True

    # Sin el punto final tambien: "Gracias." y "Gracias" son lo mismo aqui.
    if cleaned.rstrip(".!?") in HALLUCINATIONS:
        return True

    for needle in _SUBSTRINGS:
        if needle in cleaned:
            return True

    pieces = cleaned.split()
    if len(pieces) <= URL_ALONE_MAX_TOKENS and any(p.startswith("www.") for p in pieces):
        return True

    if len(cleaned) <= ONLY_JUNK_MAX_CHARS and _RE_ONLY_JUNK.match(cleaned):
        return True

    tokens = [t for t in _RE_WORD_SPLIT.split(cleaned) if t]
    return _is_repetition(tokens)
