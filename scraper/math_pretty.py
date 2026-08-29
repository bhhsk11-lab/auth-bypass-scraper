"""
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


def latex_to_readable(latex: str) -> str:
    """Convert a LaTeX string to human-readable Unicode text."""
    s = latex.strip()

    # Strip delimiters: \( ... \), $ ... $, \[ ... \]
    s = re.sub(r'^\\\(|\\\)$|^\$|\$$|^\\\[|\\\]$', "", s)

    # \frac{a}{b} → (a)/(b);  \dfrac, \tfrac same
    s = re.sub(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"(\1)/(\2)", s)
    # handle nested: run twice
    s = re.sub(r"\\[dt]?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"(\1)/(\2)", s)

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
