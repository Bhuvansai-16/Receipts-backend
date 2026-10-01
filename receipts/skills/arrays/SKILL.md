---
name: arrays
description: Read when the issue's result is a numpy array or a pandas or xarray object.
---
# numpy, pandas and xarray results

- These helpers raise AssertionError, so they count as assertion failures:
  numpy.testing.assert_array_equal and assert_allclose, pandas.testing.assert_frame_equal and
  assert_series_equal, xarray.testing.assert_identical and assert_equal.
- `assert a == b` on arrays is ambiguous (it compares element by element); use a helper, `.all()`, or compare
  `.tolist()`.
- xarray's assert_identical also compares names and attrs; assert_equal ignores them. Use the one that matches
  what the issue says.
- When the bug is that an object is shared instead of copied (attrs, data), test it the way the issue does:
  change the result and check the input didn't change, or check `result.attrs is not source.attrs`.
- Use small inline data; no files.
