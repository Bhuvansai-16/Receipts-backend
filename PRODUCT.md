# Product

## Register

product

## Users

Two audiences, in this order:

1. **Hackathon judges** (Nebius x NVIDIA, Coding & Agentic Engineering track). They open the demo URL, pick a SWE-bench instance and a PR (real fix, do-nothing PR, or a pasted diff), watch the check run, and need to read the verdict and trust the evidence within about ten seconds of it landing.
2. **Open-source maintainers** triaging pull requests, many of them written by AI agents. They want to know which PRs actually do what they claim before spending review time, and they want the evidence (the generated test and every run's output) to check it themselves in about thirty seconds.

## Product Purpose

Receipts checks whether a pull request does what it claims. It writes the missing test blind (from the issue only), runs it in forked sandboxes on the base code and on the PR, and reports a verdict with the evidence: PROVEN, REFUTED, REGRESSION or UNPROVEN. The web UI has two jobs: start a check, and show its receipt (live while it runs, complete afterwards). Success: a judge understands what happened and why without reading documentation.

## Brand Personality

Precise, calm, trustworthy. Evidence over opinion. Nothing shouts: the interface is quiet so the verdict can carry the weight. PROVEN reads as a fast lane for honest contributors, not only REFUTED as a reason to close. Uncertain results are stated plainly as UNPROVEN, never dressed up.

## Anti-references

- Dark "hacker terminal" developer-tool aesthetics and neon-on-black.
- Blue as a brand or accent color (explicitly excluded by the owner).
- Walls of CI logs as the primary view; raw output belongs behind a click.
- SaaS dashboards made of identical card grids and hero metrics.
- Anything that makes a REFUTED verdict feel like an accusation (alarm-red banners, warning icons everywhere).

Positive reference: clay.com (white field, black type and actions, rounded compact controls, color used in small deliberate moments).

## Design Principles

1. **The receipt is the product.** One artifact, read top to bottom like a receipt, with the verdict as the total. Everything else is a detail you open.
2. **Show the evidence, not a score.** Every verdict sits next to the test and the run results that produced it.
3. **Say less when unsure.** UNPROVEN is a first-class, neutral outcome; the UI never implies more certainty than the engine has.
4. **Live is legible.** While a check runs, each step appears when it happens, in the order it happens, so watching it is the explanation.
5. **Quiet by default, color for meaning.** Color appears only where a verdict or run result lives.

## Accessibility & Inclusion

WCAG 2.2 AA: text contrast at least 4.5:1 (including muted and placeholder text), full keyboard operation with visible focus, verdicts and run results never conveyed by color alone (always a word and a glyph), live updates announced politely via aria-live, and the receipt "printing" animation replaced by instant rendering under prefers-reduced-motion.
