# Jobsearcher: notes for Claude

A personal job-search pipeline: search Swedish job boards, rank ads against
the user's master CV with an LLM, draft tailored applications, and show them
in a local web UI. It runs in Docker on the user's Arch Linux home server.

- **Design and decisions:** [PLAN.md](PLAN.md). Don't re-open settled decisions
  (local hosting, free contact sources only, $20/month budget, Kimi for ranking
  and Claude Code for drafting) without the user.
- **What's left to do:** [REMAINING_WORK.md](REMAINING_WORK.md). Read it before
  starting work, and update it when you finish something.

## Commands

```sh
pip install -e '.[dev]'
pytest                 # all tests run offline; keep it that way
ruff check . && ruff format --check .
jobsearcher --help     # search | rank | run | list | show | companies | signals | llm-check | budget | daemon
```

## Layout

- `jobsearcher/sources/`: one adapter per job board, returning normalised `models.Job`
- `jobsearcher/store.py`: SQLite store (jobs, job_sources, rankings, drafts, llm_usage)
- `jobsearcher/llm/`: provider-neutral `complete(system, context, prompt, schema)`.
  Every call goes through `BudgetedLLM`, which records cost and enforces the budget.
- `jobsearcher/ranking/`: `ranking.yaml` config, prefilter, scoring, `final_score`
- `jobsearcher/pipeline.py`: the search stage (job boards, then company feeds). `cli.py` wires the stages together.
- `jobsearcher/companies/`: target companies (`companies.yaml`), ATS detection, polite crawling;
  `jobsearcher/sources/ats/`: one adapter per ATS feed (Teamtailor, Varbi, Lever, Greenhouse, SmartRecruiters)
- `jobsearcher/signals/`: company news from GDELT, classified by the LLM into spontaneous-application signals

## Rules

- **Personal data never goes into git:** `config.yaml`, `ranking.yaml`, `companies.yaml`,
  `.env`, `cvs/`, `data/` are gitignored. Only `*.example.*` files are committed.
- **The LLM must never invent experience or contacts.** Drafts are grounded in
  `cvs/master.md`, and contacts always carry a `provenance`.
- **Avoid paying twice:** cache LLM results by a hash of their inputs, as
  `ranking/ranker.py:input_hash` does, and bump `PROMPT_VERSION` when a prompt
  or schema changes.
- **`claude_code` provider:** only ever run the official `claude` CLI with the
  subscription token. Never hand that token to an SDK. Don't use `--bare`.
- The JobTech APIs may be unreachable from cloud sandboxes, so test against
  fixtures in `tests/fixtures/`.
- **Crawl politely:** company sites and feeds go through `companies/http.py:PoliteClient`
  (robots.txt, per-host pacing). Don't work around a robots.txt disallow.
- Match the surrounding style: type hints, pydantic models, short comments
  that explain why.
