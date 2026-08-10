"""Diccionario de jerga: canonicalizacion por regex + semilla del decoder.

POR QUE vive aparte de level0: es el UNICO estado del post-proceso que el usuario
edita en caliente. Aislarlo permite recargar `dictionary.yaml` sin tocar las reglas
deterministas, y alimentar con los mismos canonicos los dos frentes del criterio
C7.5 del SPEC: el `initial_prompt` de Whisper (prevenir el error dentro del decoder)
y el regex de sustitucion (curarlo cuando ya ocurrio).

POR QUE el plegado de tildes es CARACTER A CARACTER y no unicodedata.normalize:
    Necesitamos casar sin tildes ("migracion" tiene que encontrar "migracion") pero
    reemplazar sobre el texto ORIGINAL, usando los offsets que devuelve el regex.
    Eso solo funciona si el texto plegado mide EXACTAMENTE lo mismo que el original.

    NFD no cumple: descompone cada letra acentuada en dos code points (letra + acento
    combinante), asi que cada tilde desplaza en +1 todos los offsets posteriores y el
    reemplazo acaba cortando por donde no es. Es el bug clasico de esta funcion y no
    se manifiesta hasta que aparece la primera tilde antes del termino a corregir.

    str.maketrans con un mapeo 1:1 (a-con-tilde -> a) preserva la longitud, y con ella
    la alineacion de indices. Verificado en el banco: len(fold(t)) == len(t) siempre.

POR QUE las variantes se ordenan por longitud descendente: el regex alterna y Python
elige la PRIMERA alternativa que casa, no la mas larga. Sin ese orden, "next" ganaria
a "next punto js" y el resultado seria "Next.js punto js".
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .. import logging_setup

ROOT = Path(__file__).resolve().parent.parent.parent
DICT_PATH = ROOT / "dictionary.yaml"

# Ventana de `initial_prompt` de Whisper: 224 tokens. Pasarse no da error, silenciosamente
# recorta por el principio. Aproximacion de tokens para espanol medida a ojo sobre el
# tokenizador de Whisper: ~3.5 caracteres por token.
PROMPT_TOKEN_LIMIT = 224
CHARS_PER_TOKEN_ES = 3.5
PROMPT_CHAR_BUDGET = int(PROMPT_TOKEN_LIMIT * CHARS_PER_TOKEN_ES)  # 784

# Mapa de plegado. Se escribe con escapes \u a proposito: este fichero se importa desde
# una consola Windows que puede estar en cp1252, y un literal acentuado ahi es una
# fuente de UnicodeDecodeError que no aporta nada.
_FOLD_PAIRS: tuple[tuple[str, str], ...] = (
    ("\u00e1\u00e0\u00e4\u00e2\u00e3\u00e5", "a"),
    ("\u00e9\u00e8\u00eb\u00ea", "e"),
    ("\u00ed\u00ec\u00ef\u00ee", "i"),
    ("\u00f3\u00f2\u00f6\u00f4\u00f5", "o"),
    ("\u00fa\u00f9\u00fc\u00fb", "u"),
    ("\u00f1", "n"),
    ("\u00e7", "c"),
    ("\u00fd\u00ff", "y"),
)


def _build_fold_table() -> dict[int, str]:
    table: dict[int, str] = {}
    for sources, target in _FOLD_PAIRS:
        for ch in sources:
            # Cada entrada es exactamente 1 caracter -> 1 caracter. Si alguna vez se
            # anade un mapeo de longitud != 1 (p.ej. "ae"), la alineacion de offsets se
            # rompe y con ella todo el reemplazo.
            assert len(ch) == 1 and len(target) == 1
            table[ord(ch)] = target
            up_src, up_dst = ch.upper(), target.upper()
            if len(up_src) == 1 and len(up_dst) == 1:
                table[ord(up_src)] = up_dst
    return table


_FOLD_TABLE = _build_fold_table()


def fold(text: str) -> str:
    """Quita tildes preservando la longitud (y por tanto los indices)."""
    return text.translate(_FOLD_TABLE)


@dataclass(frozen=True)
class _Compiled:
    """Estado inmutable del diccionario.

    Se sustituye entero de golpe al recargar: asignar un atributo es atomico en
    CPython, asi que un dictado en vuelo o ve el diccionario viejo o ve el nuevo,
    nunca uno a medio construir. Evita meter un lock en el camino caliente.
    """
    regex: re.Pattern[str] | None
    by_key: dict[str, str]
    canonicals: tuple[str, ...]
    protected: frozenset[str]
    n_variants: int
    # Prosa escrita a mano en dictionary.yaml (clave `prompt_prose`). Si esta,
    # gana a la generada: un humano puede poner cada termino en el contexto
    # sintactico en que de verdad se dice, y eso sesga mejor al decoder.
    prose: str = ""


_EMPTY = _Compiled(regex=None, by_key={}, canonicals=(), protected=frozenset(),
                   n_variants=0, prose="")


def _norm_key(s: str) -> str:
    """Clave de busqueda: plegada, en minusculas y con los espacios colapsados."""
    return " ".join(fold(s).lower().split())


def _variant_pattern(variant: str) -> str:
    """Patron de una variante. Los espacios se vuelven \\s+ para tolerar el espaciado
    que salga del ASR ("next   punto js")."""
    words = _norm_key(variant).split(" ")
    return r"\s+".join(re.escape(w) for w in words)


class Dictionary:
    """Carga dictionary.yaml, compila el matcher y sirve la semilla del decoder."""

    def __init__(self, path: Path | str | None = None, log: Any = None) -> None:
        self.path = Path(path) if path else DICT_PATH
        self._log = log or logging_setup.get("postprocess")
        self._mtime: float = 0.0
        self._load_lock = threading.Lock()  # serializa recargas, NO el camino caliente
        self._c: _Compiled = _EMPTY
        self.load()

    # -- lectura ------------------------------------------------------------
    @property
    def canonicals(self) -> tuple[str, ...]:
        return self._c.canonicals

    @property
    def protected(self) -> frozenset[str]:
        """Canonicos cuya capitalizacion NO se puede tocar ("n8n", "npm", "shadcn")."""
        return self._c.protected

    @property
    def n_terms(self) -> int:
        return len(self._c.canonicals)

    @property
    def n_variants(self) -> int:
        return self._c.n_variants

    # -- carga --------------------------------------------------------------
    def load(self) -> bool:
        """Lee y compila. Si el YAML esta roto, conserva lo anterior y avisa."""
        with self._load_lock:
            if not self.path.exists():
                self._log.warning("dictionary.yaml no existe en %s; sin jerga", self.path)
                self._c = _EMPTY
                return False
            try:
                raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            except Exception as e:
                # Un YAML a medio guardar no puede tumbar el dictado en curso.
                self._log.error("dictionary.yaml ilegible (%s); se mantiene el anterior", e)
                return False
            try:
                self._c = self._compile(raw)
            except Exception as e:
                self._log.error("dictionary.yaml invalido (%s); se mantiene el anterior", e)
                return False
            try:
                self._mtime = self.path.stat().st_mtime
            except OSError:
                pass
            self._log.info("diccionario cargado: %d terminos, %d variantes",
                           self.n_terms, self.n_variants)
            return True

    def maybe_reload(self) -> bool:
        """Poll de mtime. True si recargo. Se llama desde el hilo de estado."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        return self.load()

    def _compile(self, raw: dict[str, Any]) -> _Compiled:
        terms = raw.get("terms") or []
        if not isinstance(terms, list):
            raise ValueError("la clave 'terms' no es una lista")
        prose = raw.get("prompt_prose") or ""
        if not isinstance(prose, str):
            self._log.warning("prompt_prose no es texto; ignorada")
            prose = ""
        prose = " ".join(prose.split())

        by_key: dict[str, str] = {}
        canonicals: list[str] = []
        protected: set[str] = set()
        seen_canon: set[str] = set()

        for entry in terms:
            if not isinstance(entry, dict):
                continue
            canon = entry.get("canonical")
            if not canon or not isinstance(canon, str):
                continue
            canon = canon.strip()
            if canon not in seen_canon:
                seen_canon.add(canon)
                canonicals.append(canon)

            # Se protege de la capitalizacion todo lo que tenga forma "rara": digitos
            # ("n8n" -> "N8n" seria un destrozo), mayusculas internas, o marca explicita
            # keep_case en el YAML ("npm", "uv", "shadcn", "ffmpeg", "localhost").
            if (entry.get("keep_case")
                    or any(c.isdigit() for c in canon)
                    or any(c.isupper() for c in canon)):
                protected.add(canon)

            variants = entry.get("variants") or []
            if isinstance(variants, str):
                variants = [variants]
            # El propio canonico entra como variante: asi se corrige la CAJA aunque el
            # ASR haya acertado las letras ("supabase" -> "Supabase").
            for v in [canon, *variants]:
                if not isinstance(v, str):
                    continue
                key = _norm_key(v)
                if not key:
                    continue
                prev = by_key.get(key)
                if prev is not None and prev != canon:
                    self._log.warning("variante '%s' repetida: '%s' gana a '%s'",
                                      key, canon, prev)
                by_key[key] = canon

        if not by_key:
            return _Compiled(regex=None, by_key={}, canonicals=(),
                             protected=frozenset(), n_variants=0, prose=prose)

        # Longitud descendente: ver el docstring del modulo.
        keys = sorted(by_key, key=lambda k: (-len(k), k))
        alt = "|".join(_variant_pattern(k) for k in keys)
        # (?<!\w) / (?!\w) en vez de \b: \b depende de si el borde de la variante es
        # caracter de palabra, y aqui hay variantes que empiezan o acaban en '.'
        # ("next.js", "acme.ai"). Estos lookarounds valen para todas por igual.
        regex = re.compile(rf"(?<!\w)(?:{alt})(?!\w)", re.IGNORECASE)

        return _Compiled(
            regex=regex,
            by_key=by_key,
            canonicals=tuple(canonicals),
            protected=frozenset(protected),
            n_variants=len(by_key),
            prose=prose,
        )

    # -- aplicacion ---------------------------------------------------------
    def apply(self, text: str) -> str:
        """Sustituye variantes por canonicos sobre el texto ORIGINAL (con sus tildes)."""
        c = self._c  # una sola lectura: si recargan a mitad, se usa una version coherente
        if c.regex is None or not text:
            return text
        folded = fold(text)
        out: list[str] = []
        last = 0
        hit = False
        for m in c.regex.finditer(folded):
            canon = c.by_key.get(_norm_key(m.group(0)))
            if canon is None:
                continue
            hit = True
            # m.start()/m.end() son indices del texto PLEGADO, que mide igual que el
            # original: por eso se pueden usar tal cual sobre `text`.
            out.append(text[last:m.start()])
            out.append(canon)
            last = m.end()
        if not hit:
            return text
        out.append(text[last:])
        return "".join(out)

    # -- semilla del decoder ------------------------------------------------
    #
    # EL FORMATO DEL PROMPT IMPORTA MAS QUE SU CONTENIDO.
    #
    # Whisper trata `initial_prompt` como TEXTO PREVIO y continua su ESTILO, no
    # solo su vocabulario. Un prompt que es una lista separada por comas induce
    # una salida en forma de lista, y la transcripcion sale truncada: el decoder
    # escupe cuatro palabras clave y cierra.
    #
    # Medido sobre el corpus (small int8, beam=1, 10 clips, 2026-08-09):
    #
    #     formato del prompt        retencion   jerga    p50
    #     sin prompt                    99%     17/31   1494 ms
    #     lista "A, B, C"               77%     27/31   1566 ms   <- destrozaba
    #     prosa espanola               100%     30/31   1534 ms   <- ELEGIDO
    #     prosa + beam=5               100%     31/31   1893 ms   <- +359 ms por 1 termino
    #
    # O sea: la prosa gana en los DOS ejes a la vez y cuesta 40 ms sobre no tener
    # prompt. Fue el hallazgo que salvo la feature; la version en lista habria
    # pasado desapercibida como "small transcribe regular".
    _PORTADORAS: tuple[str, ...] = (
        "Trabajo con {}.",
        "Uso {} todos los dias.",
        "El proyecto corre sobre {}.",
        "Tambien aparecen {}.",
        "A veces menciono {}.",
    )
    _TERMS_POR_FRASE = 5

    @staticmethod
    def _enumerar(terms: list[str]) -> str:
        """'A, B y C', con la regla espanola de y -> e ante i-/hi-.

        No es cosmetica: el prompt tiene que parecer espanol corriente. Un 'y
        Inversiones' cantaria como texto generado y es justo lo que no queremos
        que el decoder imite."""
        if len(terms) == 1:
            return terms[0]
        ultimo = terms[-1]
        low = fold(ultimo).lower()
        conj = "e" if (low.startswith("i") and not low.startswith("hie")) or \
                      low.startswith("hi") and not low.startswith("hie") else "y"
        return f"{', '.join(terms[:-1])} {conj} {ultimo}"

    def _prose(self, budget: int) -> str:
        """Teje los canonicos en frases espanolas hasta agotar el presupuesto."""
        frases: list[str] = []
        used = 0
        i = 0
        canon = list(self._c.canonicals)
        while i < len(canon):
            grupo = canon[i:i + self._TERMS_POR_FRASE]
            plantilla = self._PORTADORAS[len(frases) % len(self._PORTADORAS)]
            frase = plantilla.format(self._enumerar(grupo))
            add = len(frase) + (1 if frases else 0)
            if used + add > budget:
                self._log.warning(
                    "jerga truncada para el prompt de Whisper: %d de %d terminos "
                    "(tope %d tokens)", i, len(canon), PROMPT_TOKEN_LIMIT)
                break
            frases.append(frase)
            used += add
            i += self._TERMS_POR_FRASE
        return " ".join(frases)

    def initial_prompt(self, style: str = "") -> str | None:
        """Contexto previo para el decoder. PREVIENE el error en vez de curarlo.

        Si `dictionary.yaml` trae `prompt_prose`, se usa tal cual: una prosa
        escrita a mano siempre sesga mejor que una generada, porque puede poner
        los terminos en el contexto sintactico en que de verdad se dicen.
        """
        style = (style or "").strip()
        escrita = (self._c.prose or "").strip()
        if escrita:
            texto = f"{style} {escrita}" if style else escrita
            if len(texto) > PROMPT_CHAR_BUDGET:
                self._log.warning("prompt_prose excede el presupuesto (%d > %d chars); "
                                  "se recorta por frase", len(texto), PROMPT_CHAR_BUDGET)
                trozos, acc = texto.split(". "), []
                for t in trozos:
                    if sum(len(x) + 2 for x in acc) + len(t) > PROMPT_CHAR_BUDGET:
                        break
                    acc.append(t)
                texto = ". ".join(acc).rstrip(".") + "."
            return texto or None

        if not self._c.canonicals:
            return style or None
        budget = PROMPT_CHAR_BUDGET - (len(style) + 1 if style else 0)
        body = self._prose(max(0, budget))
        if not body:
            return style or None
        return f"{style} {body}" if style else body

    def hotwords(self) -> str | None:
        """Sesgo lexico para backends con hotwords REALES (Parakeet, sherpa-onnx).

        OJO: faster-whisper no tiene hotwords de verdad; nuestro backend mapea
        este parametro a `initial_prompt`, y ahi una lista destroza la salida
        (ver la tabla de arriba). Por eso `app.py` pide `initial_prompt()`, no
        esto. Se conserva para el dia que entre un backend que si lo soporte.
        """
        if not self._c.canonicals:
            return None
        parts, used = [], 0
        for c in self._c.canonicals:
            add = len(c) + (2 if parts else 0)
            if used + add > PROMPT_CHAR_BUDGET:
                break
            parts.append(c)
            used += add
        return ", ".join(parts) or None
