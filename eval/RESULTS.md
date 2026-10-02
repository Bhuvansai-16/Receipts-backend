# Receipts against SWE-bench Verified's answer key

40 issues, 199 patches (79 fix the issue, 120 don't, by SWE-bench's hidden tests, which Receipts never sees).

| | Receipts (runs the blind test) | Nemotron Ultra reading the diff |
|---|---|---|
| Catch rate: wrong patches flagged | 61% | 53% |
| Wrong patches passed as fixes | 10% | 46% |
| Real fixes confirmed | 66% | 96% |
| Real fixes rejected | 9% | 4% |
| No answer (Unproven / unsure) | 28% | 1% |

Cost: $6.773 in all, $0.0046 median per check at Token Factory list prices; median 27.5 s per check; 70% of checks reused a blind test.

## Per repository

| Repository | Patches | Catch rate | Wrong passed | Fixes confirmed | Fixes rejected |
|---|---|---|---|---|---|
| astropy/astropy | 30 | 61% | 6% | 50% | 0% |
| matplotlib/matplotlib | 30 | 50% | 0% | 58% | 17% |
| mwaskom/seaborn | 5 | 67% | 0% | 0% | 50% |
| psf/requests | 24 | 67% | 7% | 89% | 0% |
| pydata/xarray | 30 | 89% | 6% | 100% | 0% |
| pylint-dev/pylint | 25 | 60% | 13% | 20% | 30% |
| pytest-dev/pytest | 30 | 61% | 17% | 75% | 0% |
| scikit-learn/scikit-learn | 25 | 33% | 27% | 80% | 10% |

## Why some checks were Unproven

- mixed runs: 19
- second opinion doubted the test: 17
- patch didn't apply: 8
- no valid test: 5
- pipeline error (sandbox or API): 3
- existing tests couldn't run: 2
- PR runs didn't run: 1

## Every miss

- REFUTED on matplotlib__matplotlib-23314 (gold , fixes the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-matplotlib__matplotlib-23314-gold)
- REFUTED on pylint-dev__pylint-4970 (gold , fixes the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-pylint-dev__pylint-4970-gold)
- REFUTED on pylint-dev__pylint-6386 (gold , fixes the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-pylint-dev__pylint-6386-gold)
- REFUTED on scikit-learn__scikit-learn-12682 (gold , fixes the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-scikit-learn__scikit-learn-12682-gold)
- PROVEN on pydata__xarray-2905 (wrong 20241028_agentless-1.5_gpt4o, does not fix the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-pydata__xarray-2905-wrong-20241028_agentless-1.5_gpt4o)
- PROVEN on pylint-dev__pylint-7277 (wrong 20240620_sweagent_claude3.5sonnet, does not fix the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-pylint-dev__pylint-7277-wrong-20240620_sweagent_claude3.5sonnet)
- PROVEN on pytest-dev__pytest-7236 (wrong 20241029_OpenHands-CodeAct-2.1-sonnet-20241022, does not fix the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-pytest-dev__pytest-7236-wrong-20241029_OpenHands-CodeAct-2.1-sonnet-20)
- PROVEN on scikit-learn__scikit-learn-12682 (wrong 20240620_sweagent_claude3.5sonnet, does not fix the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-scikit-learn__scikit-learn-12682-wrong-20240620_sweagent_claude3.5sonnet)
- PROVEN on scikit-learn__scikit-learn-12973 (wrong 20241028_agentless-1.5_gpt4o, does not fix the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-scikit-learn__scikit-learn-12973-wrong-20241028_agentless-1.5_gpt4o)
- PROVEN on astropy__astropy-13453 (wrong 20241029_OpenHands-CodeAct-2.1-sonnet-20241022, does not fix the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-astropy__astropy-13453-wrong-20241029_OpenHands-CodeAct-2.1-sonnet-20)
- PROVEN on psf__requests-1142 (wrong 20241113_nebius-search-open-weight-models-11-24, does not fix the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-psf__requests-1142-wrong-20241113_nebius-search-open-weight-model)
- PROVEN on pylint-dev__pylint-7277 (wrong 20250225_sweagent_claude-3-7-sonnet, does not fix the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-pylint-dev__pylint-7277-wrong-20250225_sweagent_claude-3-7-sonnet)
- PROVEN on pytest-dev__pytest-10081 (wrong 20241028_agentless-1.5_gpt4o, does not fix the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-pytest-dev__pytest-10081-wrong-20241028_agentless-1.5_gpt4o)
- PROVEN on pytest-dev__pytest-5631 (wrong 20241029_OpenHands-CodeAct-2.1-sonnet-20241022, does not fix the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-pytest-dev__pytest-5631-wrong-20241029_OpenHands-CodeAct-2.1-sonnet-20)
- PROVEN on scikit-learn__scikit-learn-10908 (wrong 20241029_OpenHands-CodeAct-2.1-sonnet-20241022, does not fix the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-scikit-learn__scikit-learn-10908-wrong-20241029_OpenHands-CodeAct-2.1-sonnet-20)
- PROVEN on scikit-learn__scikit-learn-12973 (wrong 20241029_OpenHands-CodeAct-2.1-sonnet-20241022, does not fix the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-scikit-learn__scikit-learn-12973-wrong-20241029_OpenHands-CodeAct-2.1-sonnet-20)
- REFUTED on matplotlib__matplotlib-23314 (right 20241029_OpenHands-CodeAct-2.1-sonnet-20241022, fixes the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-matplotlib__matplotlib-23314-right-20241029_OpenHands-CodeAct-2.1-sonnet-20)
- REFUTED on mwaskom__seaborn-3069 (right 20241108_autocoderover-v2.0-claude-3-5-sonnet-20241022, fixes the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-mwaskom__seaborn-3069-right-20241108_autocoderover-v2.0-claude-3-5-s)
- REFUTED on pylint-dev__pylint-4970 (right 20241108_autocoderover-v2.0-claude-3-5-sonnet-20241022, fixes the issue): [receipt](https://receipts-frontend-six.vercel.app/runs/eval-pylint-dev__pylint-4970-right-20241108_autocoderover-v2.0-claude-3-5-s)

Every row, score and trace: [the public LangSmith dataset](https://smith.langchain.com/public/aa3e7194-fe49-4c5a-8438-60dbb615a081/d).

## What the eval changed

The tables above are the baseline. Each fix below came from its misses and was measured where it applies:

| Change | Measured on | Before | After |
|---|---|---|---|
| Apply a PR's text changes when it also adds binary files without data (images) (c04adda) | the 7 patches with image files, re-run (experiment receipts-545c8189) | 7 Unproven: patch didn't apply | 2 real fixes Proven, 2 wrong patches caught, 2 Unproven, 1 wrong patch passed |
| Second opinion before Refuted: asks whether the failure is the bug (not an environment error) and whether the test checks observable behaviour; asked 3 times, any doubt keeps it Unproven (aa610a8) | replayed on every baseline check that reached it (scripts/eval_judge_replay.py) | 7 real fixes refuted, 58 wrong patches refuted | 2 real fixes refuted, 51 wrong patches refuted (5 of the 7 lost were tests crashing on an environment error that refuted the real fixes too) |

One baseline check (psf__requests-1724 with an agent patch) hung for over 20 minutes and was stopped; the tables cover the other 199.
