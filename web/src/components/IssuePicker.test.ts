import { describe, expect, it } from "vitest";
import { searchInstances } from "./IssuePicker";

const items = [
  { id: "psf__requests-1142", repo: "psf/requests", difficulty: "", title: "requests.get is ALWAYS sending content length" },
  { id: "pydata__xarray-4629", repo: "pydata/xarray", difficulty: "", title: "merge(combine_attrs='override') does not copy attrs" },
  { id: "psf__requests-2317", repo: "psf/requests", difficulty: "", title: "method = builtin_str(method) problem" },
];

describe("searchInstances", () => {
  it("returns everything for an empty query", () => {
    expect(searchInstances(items, "  ")).toHaveLength(3);
  });

  it("matches every word across id, repo and title, case-insensitively", () => {
    expect(searchInstances(items, "requests CONTENT").map((i) => i.id)).toEqual(["psf__requests-1142"]);
    expect(searchInstances(items, "4629").map((i) => i.id)).toEqual(["pydata__xarray-4629"]);
    expect(searchInstances(items, "psf/requests").map((i) => i.id)).toEqual(["psf__requests-1142", "psf__requests-2317"]);
  });

  it("returns nothing when a word matches nowhere", () => {
    expect(searchInstances(items, "requests django")).toEqual([]);
  });
});
