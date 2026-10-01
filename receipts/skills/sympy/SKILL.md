---
name: sympy
description: Read for sympy issues (expressions, evaluate=False, assumptions, solve, simplify or printing).
---
# sympy

- Create symbols with the assumptions the issue uses, e.g. Symbol("x", positive=True); a plain Symbol("x") has
  no sign assumptions.
- Keep values exact: Rational(1, 3), sqrt(2), pi. Use .evalf() and a tolerance only when the issue itself gives
  decimals.
- For unevaluated expressions, build the expected object with evaluate=False exactly as the issue writes it and
  compare objects (see the expected-values skill); srepr never shows evaluate=False.
- solve() returns a list of solutions (a list of dicts with dict=True). When order doesn't matter, compare sets.
- Keep inputs small; simplify() on large expressions is slow.
