---
name: expected-values
description: Read before writing the test when the issue writes the expected result as code (a call, a literal).
---
# Expected results written as code

Build the expected value in the test from the same code the issue writes, and compare objects with ==:

    expected = Mul(-1, Add(x, 2, evaluate=False), evaluate=False)  # copied from the issue
    assert parse_expr("-(x + 2)", evaluate=False) == expected

Never retype what you think str(), repr() or a printer shows for it. Printers can leave out what the code says
(sympy's srepr never prints evaluate=False), and a retyped string can fail even after a correct fix. Compare
printed output only when the issue itself only shows printed output, and then use the printer the issue used.
