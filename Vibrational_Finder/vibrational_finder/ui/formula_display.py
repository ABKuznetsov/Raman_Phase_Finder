from __future__ import annotations

import re

from vibrational_finder.ui.element_filter import actinides, lanthanides, periodic_table_positions


_SUBSCRIPT = str.maketrans("0123456789", "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089")
_SUPERSCRIPT = str.maketrans("0123456789+-", "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b")
_QUANTITY = r"[0-9]+(?:[.,][0-9]+)?(?:[-\u2013][0-9]+(?:[.,][0-9]+)?)?"
_CHARGE = r"[0-9]*[+-]"
_RRUFF_SCRIPTS = re.compile(
    rf"\^(?P<charge>{_CHARGE})\^(?P<charged_count>{_QUANTITY})?"
    rf"|(?P<base>[A-Z][a-z]?|[)\]])(?P<count>{_QUANTITY})"
)
_OPENSPECY_ENTITIES = {"&#183;": "\u00b7", "&#8804;": "\u2264", "&#8776;": "\u2248", "&#931;": "\u03a3"}
_OPENSPECY_MARKUP = re.compile(
    rf"\^(?P<charge>{_CHARGE})\^|(?<=[A-Za-z)\]^])_(?P<count>{_QUANTITY})_"
    rf"|(?P<entity>{'|'.join(map(re.escape, _OPENSPECY_ENTITIES))})"
)
_LOWERCASE_TOKEN = re.compile(r"(?<![A-Za-z&])[a-z]{1,2}(?![A-Za-z;])")
_ELEMENT_SYMBOLS = {symbol for symbol, _, _ in periodic_table_positions()} | set(lanthanides()) | set(actinides())
_SINGLE_SYMBOLS = {symbol for symbol in _ELEMENT_SYMBOLS if len(symbol) == 1} | {"D", "T"}
# Do not split case-folded runs, or reinterpret CO/Co and similar pairs. Keep n and y as possible variables.
_SAFE_CASE = {
    symbol.lower(): symbol for symbol in _ELEMENT_SYMBOLS
    if symbol not in {"N", "Y"} and not (len(symbol) == 2 and set(symbol.upper()) <= _SINGLE_SYMBOLS)
}


def _subscript(count: str) -> str:
    # Unicode has no subscript decimal point. Keep decimal and range text intact.
    return count.translate(_SUBSCRIPT) if count.isdecimal() else count


def _rruff_script(match: re.Match[str]) -> str:
    if match["charge"] is not None:
        return match["charge"].translate(_SUPERSCRIPT) + _subscript(match["charged_count"] or "")
    return match["base"] + _subscript(match["count"])


def _openspecy_markup(match: re.Match[str]) -> str:
    if match["charge"] is not None:
        return match["charge"].translate(_SUPERSCRIPT)
    if match["count"] is not None:
        return _subscript(match["count"])
    return _OPENSPECY_ENTITIES[match["entity"]]


def display_formula(formula: str, source: str) -> str:
    """Typeset known library notation for display, without inferring missing chemistry."""
    if source == "RRUFF":
        return _RRUFF_SCRIPTS.sub(_rruff_script, formula)
    if source == "OpenSpecy":
        formatted, substitutions = _OPENSPECY_MARKUP.subn(_openspecy_markup, formula)
        if substitutions:
            return _LOWERCASE_TOKEN.sub(lambda token: _SAFE_CASE.get(token[0], token[0]), formatted)
    return formula
