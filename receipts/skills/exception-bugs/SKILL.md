---
name: exception-bugs
description: Read before writing the test when the bug is an exception (the issue says a call raises or crashes).
---
# When the bug is an exception

The test must fail with an AssertionError on the current code. Call the code inside try/except, catch the
exception the issue names, and turn it into an assertion:

    def test_fit_accepts_lists():
        try:
            result = fit([1, 2, 3])
        except TypeError as e:
            assert False, f"raised {e!r}"
        assert result == [2, 4, 6]  # only if the issue states the correct result

- Catch only the exception type the issue names; anything else should still show up as an error.
- Don't use pytest.raises for the bug itself: it passes on the buggy code.
