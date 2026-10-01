---
name: plotting
description: Read for matplotlib or seaborn issues (figures, axes, artists, colors or layout).
---
# matplotlib and seaborn

- Pick the non-interactive backend before pyplot is imported: import matplotlib; matplotlib.use("Agg").
- Never call plt.show(); check the objects instead: ax.get_xlim(), line.get_color(), fig.get_size_inches(),
  ax.get_legend().
- Compare colors with matplotlib.colors.to_rgba(...) rather than by name.
- Draw image output to an in-memory io.BytesIO, not a file, and finish with plt.close("all").
