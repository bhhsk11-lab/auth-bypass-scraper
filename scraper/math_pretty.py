r"""
Math formula humanizer: LaTeX → human-readable Unicode.
Handles \frac, ^, _, \times, \infty, Greek letters, etc.
Optional sympy fallback for complex expressions.
"""
import re

# LaTeX symbol → Unicode
SYMBOLS = {
    r"\times": "×", r"\cdot": "·", r"\div": "÷", r"\pm": "±",
    r"\infty": "∞", r"\leq": "≤", r"\geq": "≥", r"\neq": "≠",
    r"\approx": "≈", r"\rightarrow": "→", r"\to": "→", r"\Rightarrow": "⇒",
    r"\sum": "Σ", r"\prod": "Π", r"\int": "∫", r"\partial": "∂",
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\Delta": "Δ", r"\theta": "θ", r"\lambda": "λ", r"\mu": "μ",
    r"\pi": "π", r"\Pi": "Π", r"\rho": "ρ", r"\sigma": "σ",
    r"\Sigma": "Σ", r"\phi": "φ", r"\omega": "ω", r"\Omega": "Ω",
    r"\left": "", r"\right": "", r"\,": " ", r"\;": " ", r"\ ": " ",
    r"\quad": "  ", r"\qquad": "    ", r"\dots": "…", r"\ldots": "…",
    r"\cdots": "⋯", r"\sqrt": "√", r"\in": "∈", r"\forall": "∀",
    r"\exists": "∃", r"\subset": "⊂", r"\cup": "∪", r"\cap": "∩",
}

SUPERSCRIPTS = {"0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
                "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
                "+": "⁺", "-": "⁻", "n": "ⁿ", "k": "ᵏ", "i": "ⁱ",
                "(": "⁽", ")": "⁾", "x": "ˣ", "a": "ᵃ", "b": "ᵇ"}

SUBSCRIPTS = {"0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
              "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
              "+": "₊", "-": "₋", "n": "ₙ", "k": "ₖ", "i": "ᵢ",
              "(": "₍", ")": "₎", "a": "ₐ", "e": "ₑ"}


def _to_super(s: str) -> str:
    return "".join(SUPERSCRIPTS.get(c, c) for c in s)


def _to_sub(s: str) -> str:
    return "".join(SUBSCRIPTS.get(c, c) for c in s)


_FRAC_RE = re.compile(r"\\[dt]?frac\s*")


def _find_matching_brace(s: str, open_idx: int) -> int:
    """s[open_idx] must be '{'. Returns the index of its matching '}',
    correctly skipping over any nested {..} pairs in between, or -1 if
    the braces are unbalanced."""
    depth = 0
    for i in range(open_idx, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _convert_fracs(s: str) -> str:
    """Convert \\frac{a}{b} (and \\dfrac, \\tfrac) to (a)/(b).

    Unlike a single regex with [^{}]* arguments, this handles a or b
    containing further nested braces — e.g. \\frac{-b}{\\sqrt{b^2-4ac}}
    or a \\frac nested inside another \\frac. A plain [^{}]* regex simply
    fails to match in that case (very common: sqrt-over-something, or
    fraction-of-a-fraction), leaving \\frac unconverted; it then falls
    through to the generic residual-brace/command stripper later in
    latex_to_readable, which deletes the braces and the \\frac command
    without ever inserting the "/" — silently concatenating the numerator
    and denominator with no operator between them instead of dividing.
    """
    out = []
    i = 0
    while i < len(s):
        m = _FRAC_RE.match(s, i)
        if m and m.end() < len(s) and s[m.end()] == "{":
            num_start = m.end()
            num_end = _find_matching_brace(s, num_start)
            if num_end != -1:
                j = num_end + 1
                while j < len(s) and s[j].isspace():
                    j += 1
                if j < len(s) and s[j] == "{":
                    den_end = _find_matching_brace(s, j)
                    if den_end != -1:
                        numerator = _convert_fracs(s[num_start + 1:num_end])
                        denominator = _convert_fracs(s[j + 1:den_end])
                        out.append(f"({numerator})/({denominator})")
                        i = den_end + 1
                        continue
        out.append(s[i])
        i += 1
    return "".join(out)


def latex_to_readable(latex: str) -> str:
    """Convert a LaTeX string to human-readable Unicode text."""
    s = latex.strip()

    # Strip delimiters: \( ... \), $ ... $, \[ ... \]
    s = re.sub(r'^\\\(|\\\)$|^\$|\$$|^\\\[|\\\]$', "", s)

    # \frac{a}{b} → (a)/(b);  \dfrac, \tfrac same. Balanced-brace scan
    # (not a plain regex) so nested content — \sqrt{...}, another \frac,
    # etc. — inside a or b is handled correctly. See _convert_fracs().
    s = _convert_fracs(s)

    # \sqrt{a} → √(a), \sqrt[n]{a} → ⁿ√(a)
    s = re.sub(r"\\sqrt\s*\[([^\]]+)\]\s*\{([^{}]*)\}", lambda m: _to_super(m.group(1)) + "√(" + m.group(2) + ")", s)
    s = re.sub(r"\\sqrt\s*\{([^{}]*)\}", r"√(\1)", s)

    # Superscripts: ^{...} → unicode superscript, ^x → unicode
    s = re.sub(r"\^\{([^{}]+)\}", lambda m: _to_super(m.group(1)), s)
    s = re.sub(r"\^(\w)", lambda m: _to_super(m.group(1)), s)

    # Subscripts: _{...} → unicode subscript
    s = re.sub(r"_\{([^{}]+)\}", lambda m: _to_sub(m.group(1)), s)
    s = re.sub(r"_(\w)", lambda m: _to_sub(m.group(1)), s)

    # Symbol replacements (order matters — longest first)
    for k in sorted(SYMBOLS, key=len, reverse=True):
        s = s.replace(k + " ", SYMBOLS[k] + " ")
        s = s.replace(k, SYMBOLS[k])

    # \text{...} → plain
    s = re.sub(r"\\text\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\mathrm\s*\{([^{}]*)\}", r"\1", s)

    # Cleanup residual braces and backslashes
    s = s.replace("{", "").replace("}", "")
    s = re.sub(r"\\[a-zA-Z]+", "", s)  # any remaining commands
    s = re.sub(r"\s{2,}", " ", s)

    return s.strip()


def humanize_formulas_in_text(text: str) -> dict:
    """
    Scan a full article/PDF text for LaTeX formulas and replace each
    with human-readable form. Returns cleaned text + list of conversions.
    """
    conversions = []

    # Pattern: \( ... \)  and  \[ ... \]  and  $ ... $
    patterns = [
        re.compile(r"\\\((.+?)\\\)", re.DOTALL),
        re.compile(r"\\\[(.+?)\\\]", re.DOTALL),
        re.compile(r"\$\$(.+?)\$\$", re.DOTALL),
        re.compile(r"(?<!\\)\$(?!\s)(.+?)(?<!\\)\$", re.DOTALL),
    ]

    result = text
    seen = set()
    for pat in patterns:
        def repl(m):
            raw = m.group(1)
            pretty = latex_to_readable(raw)
            if raw not in seen:
                conversions.append({"latex": raw.strip(), "readable": pretty})
                seen.add(raw)
            return pretty
        result = pat.sub(repl, result)

    return {
        "text": result,
        "formulas_converted": conversions,
        "count": len(conversions),
    }


# sympy fallback for truly gnarly expressions
def latex_via_sympy(latex: str) -> str | None:
    """Try sympy's LaTeX parser for complex formulas. Optional."""
    try:
        from sympy.parsing.latex import parse_latex
        expr = parse_latex(latex)
        return str(expr)
    except Exception:
        return None
