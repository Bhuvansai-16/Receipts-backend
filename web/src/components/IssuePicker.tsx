import { useEffect, useId, useMemo, useState, type KeyboardEvent } from "react";
import type { InstanceSummary } from "../api";

const MAX_RESULTS = 60;

/** Every whitespace-separated word must appear in the id, repo or title (case-insensitive). */
export function searchInstances(items: InstanceSummary[], query: string): InstanceSummary[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return items;
  return items.filter((item) => {
    const haystack = `${item.id} ${item.repo} ${item.title}`.toLowerCase();
    return words.every((word) => haystack.includes(word));
  });
}

interface Props {
  inputId: string;
  hintId: string;
  instances: InstanceSummary[] | null;
  value: string | null;
  onChange: (id: string) => void;
}

/** Accessible combobox (WAI-ARIA 1.2 pattern) over the SWE-bench instance list. */
export function IssuePicker({ inputId, hintId, instances, value, onChange }: Props) {
  const listId = useId();
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);

  const results = useMemo(() => searchInstances(instances ?? [], query).slice(0, MAX_RESULTS), [instances, query]);
  const optionId = (i: number) => `${listId}-opt-${i}`;

  useEffect(() => {
    if (open) document.getElementById(optionId(active))?.scrollIntoView({ block: "nearest" });
  });

  function choose(item: InstanceSummary) {
    onChange(item.id);
    setQuery("");
    setOpen(false);
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) return setOpen(true);
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((i) => Math.min(Math.max(i + step, 0), Math.max(results.length - 1, 0)));
    } else if (e.key === "Enter" && open && results[active]) {
      e.preventDefault();
      choose(results[active]);
    } else if (e.key === "Escape") {
      if (open) e.preventDefault();
      setOpen(false);
      setQuery("");
    }
  }

  const loading = instances === null;
  return (
    <div className="combobox">
      <input
        id={inputId}
        className="input"
        type="text"
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls={listId}
        aria-activedescendant={open && results[active] ? optionId(active) : undefined}
        aria-describedby={hintId}
        autoComplete="off"
        spellCheck={false}
        disabled={loading}
        placeholder={loading ? "Loading issues…" : "Search by repo, number or title"}
        value={open ? query : (value ?? query)}
        onFocus={() => setOpen(true)}
        onBlur={() => {
          setOpen(false);
          setQuery("");
        }}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
          setOpen(true);
        }}
        onKeyDown={onKeyDown}
      />
      {open && !loading && (
        <ul id={listId} role="listbox" aria-label="SWE-bench issues" className="listbox">
          {results.length === 0 && (
            <li className="listbox__empty" role="presentation">
              No issue matches “{query}”.
            </li>
          )}
          {results.map((item, i) => (
            <li
              key={item.id}
              id={optionId(i)}
              role="option"
              aria-selected={i === active}
              className="option"
              onMouseDown={(e) => e.preventDefault() /* keep focus in the input */}
              onMouseMove={() => setActive(i)}
              onClick={() => choose(item)}
            >
              <span className="option__id">{item.id}</span>
              <span className="option__repo">{item.repo}</span>
              <span className="option__title" title={item.title}>
                {item.title}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
