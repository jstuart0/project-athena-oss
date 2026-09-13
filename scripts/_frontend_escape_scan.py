"""Shared lexical scanning helpers for the ATHENA-66 frontend-escaping guard scripts.

Not a gate script itself (no __main__, no exit-code contract) — imported by
check-handler-escaping.py, check-html-template-escaping.py, and
check-callee-sinks.py so the fragile parsing logic (quote nesting, `${}` depth
tracking, top-level comma/paren splitting) exists in exactly one place.

This is NOT a JS parser. It is a raw-text scanner tuned to the two handler
construction styles actually present in admin/frontend/*.js:

  1. Template-literal style:   onclick="foo('${escapeJsAttr(x)}')"
  2. Concatenation style:      'onclick="foo(\'' + escapeJsAttr(x) + '\')"'

Both are handled by scanning the RAW FILE TEXT (not per-JS-string-literal),
because in style 2 the meaningful attribute-value boundary (the real `"` that
closes the onclick attribute) is a literal character in the file regardless
of which JS string literal it happens to fall inside of.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

QUOTE_CHARS = ("'", '"', "`")

# The canonical escaping-primitives file. Its header docblock documents
# `on<event>=` attribute shapes as prose examples (e.g. `onclick="foo(${id})"`
# inside a comment) — these are not production handler attributes and must
# not be counted in any handler-span population. Excluded by path, not by
# shape, matching D6/rule 12's precedent for the uniqueness guard.
CANONICAL_ESCAPE_FILE = "escape-html.js"


def iter_frontend_js_files(frontend_dir: Path, exclude_canonical: bool = True) -> list[Path]:
    files = sorted(Path(frontend_dir).glob("*.js"))
    if exclude_canonical:
        files = [f for f in files if f.name != CANONICAL_ESCAPE_FILE]
    return files

# Matches `on<event>=` immediately followed by the outer attribute quote.
# Handler event names in this codebase are lowercase ascii (onclick, onchange,
# onmouseover, ...).
HANDLER_ATTR_RE = re.compile(r'\bon[a-z]+\s*=\s*(["\'])')


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def skip_js_string(text: str, i: int) -> int:
    """`text[i]` is a quote character opening a nested JS string/template.

    Returns the index immediately AFTER the matching closing quote. Handles
    backslash escapes. For a nested backtick, does not attempt to recurse
    into further `${}` inside it — that is a depth-of-2 case that does not
    occur in this codebase's handler spans and is treated as opaque (skipped
    to its closing backtick).
    """
    quote = text[i]
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == quote:
            return i + 1
        i += 1
    return n


def scan_attr_value(text: str, start: int, quote: str) -> tuple[str | None, int]:
    """`start` is the index immediately after the opening attribute quote.

    Scans forward through the RAW file text (crossing JS string-literal /
    `+` boundaries freely) until the matching unescaped `quote` character at
    brace-depth 0. `${` opens an opaque region tracked by PURE BRACE
    COUNTING — quote characters inside `${...}` are deliberately NOT given
    string-skip treatment, because a `/'/`-shaped regex literal (verified:
    app.js:3309's `.replace(/'/g, "\\'")`) defeats a naive "skip to the
    matching quote" scan: it walks past the escaped quote inside the
    following double-quoted string and consumes the attribute's own real
    closing quote as if it belonged to the regex. Brace counting is immune
    to that because it never treats a quote character specially at all — it
    only breaks if a string inside the expression contains an unbalanced
    (unpaired) `{` or `}`, which does not occur in this codebase's handler
    spans (verified against every `${...}` span with a nested quote:
    app.js:3309's regex-with-strings and every ternary-with-quotes case).

    Returns (raw_value, end_index) where end_index is the index of the
    closing quote, or (None, -1) if unterminated (e.g. a decoy `on*=` inside
    a comment or string that never resolves to a real attribute — treated as
    "not a real handler span" by the caller).
    """
    n = len(text)
    i = start
    depth = 0
    while i < n:
        c = text[i]
        if depth == 0:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == quote:
                return text[start:i], i
            if c == "$" and i + 1 < n and text[i + 1] == "{":
                depth = 1
                i += 2
                continue
            if c == "\n" and (i - start) > 4000:
                # Runaway scan (unterminated span) — bail rather than consume the file.
                return None, -1
            i += 1
        else:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == "{":
                depth += 1
                i += 1
                continue
            if c == "}":
                depth -= 1
                i += 1
                continue
            i += 1
    return None, -1


@dataclass
class HandlerSpan:
    event: str
    quote: str
    raw_value: str
    value_start: int
    value_end: int
    line: int


def find_handler_spans(text: str) -> list[HandlerSpan]:
    spans: list[HandlerSpan] = []
    for m in HANDLER_ATTR_RE.finditer(text):
        quote = m.group(1)
        start = m.end()
        raw_value, end = scan_attr_value(text, start, quote)
        if raw_value is None:
            continue
        event = m.group(0)
        event_name = re.match(r"\bon[a-z]+", event).group(0)
        spans.append(
            HandlerSpan(
                event=event_name,
                quote=quote,
                raw_value=raw_value,
                value_start=start,
                value_end=end,
                line=line_of(text, m.start()),
            )
        )
    return spans


def find_top_level_dollar_braces(value: str) -> list[tuple[int, int]]:
    """Return (start, end) index pairs (relative to `value`) for each top-level
    `${...}` in `value`, where start points at `$` and end points one past the
    matching `}`. Pure brace counting — see `scan_attr_value`'s docstring for
    why quote characters inside `${...}` are NOT given string-skip treatment
    (a `/'/`-shaped regex literal defeats it; verified at app.js:3309).
    """
    out: list[tuple[int, int]] = []
    i = 0
    n = len(value)
    while i < n:
        if value[i] == "$" and i + 1 < n and value[i + 1] == "{":
            depth = 1
            j = i + 2
            while j < n and depth > 0:
                c = value[j]
                if c == "\\" and j + 1 < n:
                    j += 2
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def split_top_level(text: str, seps: str = ",", skip_strings: bool = True) -> list[str]:
    """Split `text` on any of `seps` at paren/bracket/brace depth 0.

    `skip_strings=True` (the default, used for call-argument lists) treats a
    quote character as opening a real, self-contained nested string literal
    and skips to its match, so a `,` or `+` inside a quoted string is not a
    split point.

    `skip_strings=False` is for text that was extracted starting *inside* an
    already-open JS string literal (a handler span's raw_value in
    concatenation-construction style) — here a bare `'`/`"` encountered is
    the closing delimiter of a literal whose opening half is outside the
    extracted text, not a balanced pair, so it must NOT trigger a skip. Only
    backslash-escaped characters are consumed as escape pairs.
    """
    parts: list[str] = []
    depth = 0
    buf_start = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if skip_strings and c in QUOTE_CHARS:
            i = skip_js_string(text, i)
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and c in seps:
            parts.append(text[buf_start:i])
            buf_start = i + 1
        i += 1
    parts.append(text[buf_start:])
    return parts


def find_matching_paren(text: str, open_idx: int) -> int:
    """`text[open_idx]` must be `(`. Returns the index of the matching `)`,
    or -1 if unterminated.
    """
    assert text[open_idx] == "("
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c in QUOTE_CHARS:
            i = skip_js_string(text, i)
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


ESCAPE_CALL_RE = re.compile(r"^(escapeHtml|escapeJsAttr)\s*\((.*)\)\s*$", re.DOTALL)


@dataclass
class Interpolation:
    expr: str
    position: str  # "quoted" | "bare"
    escape: str | None  # "escapeHtml" | "escapeJsAttr" | None
    quote_char: str | None
    offset: int  # offset within the handler's raw_value


def _classify_expr(expr: str) -> tuple[str | None, str]:
    """Returns (escape_fn_or_None, inner_expr_or_original)."""
    stripped = expr.strip()
    m = ESCAPE_CALL_RE.match(stripped)
    if not m:
        return None, stripped
    fn, inner = m.group(1), m.group(2)
    # Confirm the matched "(...)" is actually balanced/complete (ESCAPE_CALL_RE's
    # greedy .* could over-match a nested call). Verify via paren matching.
    open_idx = stripped.index("(")
    close = find_matching_paren(stripped, open_idx)
    if close != len(stripped) - 1:
        return None, stripped
    return fn, inner


def classify_template_interpolations(raw_value: str) -> list[Interpolation]:
    out = []
    for start, end in find_top_level_dollar_braces(raw_value):
        expr = raw_value[start + 2 : end - 1]
        before = raw_value[start - 1] if start > 0 else ""
        after = raw_value[end] if end < len(raw_value) else ""
        if before in ("'", '"') and before == after:
            position = "quoted"
            quote_char = before
        else:
            position = "bare"
            quote_char = None
        escape_fn, _inner = _classify_expr(expr)
        out.append(
            Interpolation(
                expr=expr.strip(),
                position=position,
                escape=escape_fn,
                quote_char=quote_char,
                offset=start,
            )
        )
    return out


_IDENT_OR_CALL_RE = re.compile(
    r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*(?:\([^()]*(?:\([^()]*\)[^()]*)*\))?$"
)


def _split_concat_plus(raw_value: str) -> list[str]:
    """Split a concatenation-style handler span on top-level `+` operators.

    `raw_value` starts already INSIDE an open single-quoted JS string — the
    convention this codebase uses to build `on<event>=` attributes via
    `'...' + expr + '...'` (the `on<event>="` match itself is literal content
    of that already-open string, so scanning starts mid-literal). A bare
    (unescaped) quote character toggles in/out of "inside a string" state;
    paren/bracket/brace depth is only meaningful in real code (state
    `none`), because a `(` appearing as literal string content — e.g. the
    `(` in `'selectGuest('` — is not JS syntax and must not gate splitting.
    Backslash-escaped characters never toggle string state.
    """
    segments: list[str] = []
    buf_start = 0
    state = "single"  # 'single' | 'double' | 'none'
    depth = 0
    i = 0
    n = len(raw_value)
    while i < n:
        c = raw_value[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if state == "single":
            if c == "'":
                state = "none"
            i += 1
            continue
        if state == "double":
            if c == '"':
                state = "none"
            i += 1
            continue
        # state == "none": real code.
        if c == "'":
            state = "single"
            i += 1
            continue
        if c == '"':
            state = "double"
            i += 1
            continue
        if c in "([{":
            depth += 1
            i += 1
            continue
        if c in ")]}":
            depth -= 1
            i += 1
            continue
        if depth == 0 and c == "+":
            segments.append(raw_value[buf_start:i])
            buf_start = i + 1
            i += 1
            continue
        i += 1
    segments.append(raw_value[buf_start:])
    return segments


def classify_concat_interpolations(raw_value: str) -> list[Interpolation]:
    """For a handler span with NO `${...}` at all — the pure `+`-concatenation
    construction (guest-context.js). Splits raw_value on top-level `+` and
    classifies each identifier/call-shaped segment.

    "Quoted" vs "bare" is determined by whether the identifier/call is
    immediately flanked (on both sides, across the `+` operator) by an
    ESCAPED quote character — the only form that survives concatenation as a
    literal output quote. An unescaped flanking quote is a JS string
    literal's own delimiter, which vanishes at runtime and contributes
    nothing to the rendered attribute value.
    """
    if "${" in raw_value:
        return []  # not concatenation-only; handled by the template path.

    segments = _split_concat_plus(raw_value)
    if len(segments) < 2:
        # No top-level `+` was ever found (the state machine never reached
        # code state `none`, or reached it without a `+`) — this handler's
        # entire value is static text with zero dynamic content, e.g.
        # `onclick="filterIntents()"` inside a backtick template that has no
        # `${...}` anywhere in THIS particular span. Not an interpolation.
        return []

    out: list[Interpolation] = []
    for idx, seg in enumerate(segments):
        candidate = seg.strip()
        if not candidate or not _IDENT_OR_CALL_RE.match(candidate):
            continue
        prev_quote = _concat_prev_flank_quote(segments[idx - 1]) if idx > 0 else None
        next_quote = (
            _concat_next_flank_quote(segments[idx + 1]) if idx + 1 < len(segments) else None
        )
        position = "bare"
        quote_char = None
        if prev_quote and next_quote and prev_quote == next_quote:
            position = "quoted"
            quote_char = prev_quote
        escape_fn, _inner = _classify_expr(candidate)
        out.append(
            Interpolation(
                expr=candidate,
                position=position,
                escape=escape_fn,
                quote_char=quote_char,
                offset=raw_value.find(seg) if seg in raw_value else -1,
            )
        )
    return out


def _concat_prev_flank_quote(segment: str) -> str | None:
    """`segment` is the literal fragment immediately BEFORE a `+`-joined
    expression. Returns the quote character if the fragment's rendered
    content ends in an escaped quote immediately followed by that same
    quote's real closing delimiter (`\\''` / `\\""`) — i.e. the expression
    is preceded by a real output quote character. None otherwise (bare).
    """
    m = re.search(r"\\(['\"])\1$", segment.strip())
    return m.group(1) if m else None


def _concat_next_flank_quote(segment: str) -> str | None:
    """`segment` is the literal fragment immediately AFTER a `+`-joined
    expression. Returns the quote character if the fragment opens with a
    real delimiter immediately followed by an escaped quote of the same
    type (`'\\'` / `"\\"`) — i.e. the expression is followed by a real
    output quote character. Deliberately does not require the fragment to
    be a complete, self-closing literal: the FINAL segment in a handler
    span can be truncated at the attribute's real closing quote (which the
    span scanner treats as the terminator) before the JS string literal
    that contains it would naturally close.
    """
    m = re.match(r"^(['\"])\\\1", segment.strip())
    return m.group(1) if m else None


def classify_span_interpolations(raw_value: str) -> list[Interpolation]:
    if "${" in raw_value:
        return classify_template_interpolations(raw_value)
    return classify_concat_interpolations(raw_value)


def is_builder_marker(raw_value: str) -> bool:
    """True if the ENTIRE handler attribute value is opaque — a single
    interpolation (template or bare identifier) with no statically visible
    function-call syntax anywhere in the residue. This is the D9b marker for
    a handler-text *builder call site* (the handler body is constructed one
    function away, e.g. `onclick="${onClick}"`).
    """
    residue = raw_value
    for start, end in reversed(find_top_level_dollar_braces(raw_value)):
        residue = residue[:start] + residue[end:]
    return "(" not in residue


FUNCTION_DEF_RE = re.compile(r"function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*\{")


def find_enclosing_function(text: str, pos: int) -> tuple[str, list[str], int] | None:
    """Find the nearest `function NAME(params) {` whose brace-matched body
    contains `pos`. Returns (name, param_names, def_start) or None.

    Scans candidate `function` definitions backward from `pos` and confirms
    containment by brace-matching forward from each candidate in turn
    (nearest first), so a sibling function defined earlier in the file that
    does NOT enclose `pos` is correctly skipped.
    """
    candidates = [m for m in FUNCTION_DEF_RE.finditer(text, 0, pos)]
    for m in reversed(candidates):
        brace_open = text.index("{", m.end() - 1)
        depth = 0
        i = brace_open
        n = len(text)
        while i < n:
            c = text[i]
            if c in QUOTE_CHARS:
                i = skip_js_string(text, i)
                continue
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body_end = i
        if brace_open <= pos <= body_end:
            params = [p.strip() for p in m.group(2).split(",") if p.strip()]
            return m.group(1), params, m.start()
    return None


def strip_outer_delims(s: str) -> str:
    """Strip a single layer of matching outer quote/backtick delimiters, if
    present (used to unwrap a builder call argument like `` `foo('${x}')` ``
    to its inner template content before re-classifying it as if it were a
    handler span's raw_value).
    """
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("`", "'", '"'):
        return s[1:-1]
    return s


class Builder:
    """A D9b handler-text builder: a function whose entire handler span is
    one opaque `${identifier}` interpolation with no visible call — the
    escaping decision for its callers is made one function *upstream*.
    """

    def __init__(self, file, span_line: int, func_name: str, params: list[str], param_idx: int):
        self.file = file
        self.span_line = span_line
        self.func_name = func_name
        self.params = params
        self.param_idx = param_idx

    def key(self, repo_root) -> str:
        try:
            rel = self.file.relative_to(repo_root)
        except ValueError:
            rel = self.file
        return f"{rel}:{self.span_line} {self.func_name}"


def discover_builders(directory) -> list[Builder]:
    """D9b marker: an `on<event>=` span whose entire value is a single bare
    `${identifier}` with no statically visible call anywhere in the residue.
    Confirms the identifier is a parameter of its enclosing function.
    """
    builders: list[Builder] = []
    for path in iter_frontend_js_files(directory):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for span in find_handler_spans(text):
            if not is_builder_marker(span.raw_value):
                continue
            enclosing = find_enclosing_function(text, span.value_start)
            if enclosing is None:
                continue
            func_name, params, _def_start = enclosing
            braces = find_top_level_dollar_braces(span.raw_value)
            if len(braces) != 1:
                continue
            ident = span.raw_value[braces[0][0] + 2 : braces[0][1] - 1].strip()
            if ident not in params:
                continue
            builders.append(Builder(path, span.line, func_name, params, params.index(ident)))
    return builders


def find_calls(text: str, func_name: str) -> list[tuple[int, int, str]]:
    """Find call sites of `func_name(` in `text`, excluding its own
    definition. Returns (call_start, args_start, args_text) triples.
    """
    out = []
    call_re = re.compile(r"\b" + re.escape(func_name) + r"\s*\(")
    def_starts = {m.start(1) for m in re.finditer(
        r"function\s+(" + re.escape(func_name) + r")\s*\(", text
    )}

    def _name_start(match_start: int) -> int:
        # `match_start` points at the start of the `\bfunc_name\(` match;
        # the identifier itself begins at the same offset since `\b` is
        # zero-width.
        return match_start

    for m in call_re.finditer(text):
        if _name_start(m.start()) in def_starts:
            continue
        open_idx = text.index("(", m.start())
        close_idx = find_matching_paren(text, open_idx)
        if close_idx == -1:
            continue
        out.append((m.start(), open_idx + 1, text[open_idx + 1 : close_idx]))
    return out
