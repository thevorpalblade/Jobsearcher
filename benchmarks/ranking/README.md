# Ranking benchmark

A fixed set of 40 job ads (`jobs.json`) and each model's scores for them (`runs/`), so a new
ranking model can be compared with the earlier ones. The set is 30 jobs spread evenly over
GLM's scores plus GLM's 10 best, picked on 2026-10-06.

```sh
python scripts/rank_benchmark.py compare --jobs          # reference: opus-5.5
python scripts/rank_benchmark.py run <label> --provider moonshot --model kimi-k3 \
    --extra '{"thinking": {"type": "disabled"}}' --rpm 3
python scripts/rank_benchmark.py import glm-5.3-flash --model glm-5.3-flash   # free
```

- **Only scores are committed** (fit, success, matched role, ad language, Swedish
  requirement: what `final_score` uses). The rationale and requirement lists describe the
  CV, which is personal, so full assessments stay in the gitignored `data/benchmark/ranking/`.
- **Runs depend on the CV, ranking.yaml and prompt**, which aren't committed. Each run
  records their hashes (`cv_sha`, `ranking_sha`, `prompt_version`); `compare` warns when a
  run differs from the reference, and `run` refuses to add to a run made with other inputs.
  After changing the CV, start new labels.
- `glm-5.3-flash` has 11 of 40 jobs: only those had a pipeline ranking made with the
  current CV. Fill in the rest with `run glm-5.3-flash --provider nvidia --model
  z-ai/glm-5.3-flash --extra '{"thinking": {"type": "disabled"}}'` when NVIDIA answers.
- **The committed ads have no contact details:** structured contacts and application
  emails are dropped, and names, emails and phone numbers in the text become `[name]`,
  `[email]`, `[phone]`. Runs use the full ads in the gitignored
  `data/benchmark/ranking/jobs.json` when this machine has them, so their inputs match the
  pipeline's; elsewhere they use the stripped copy (slightly different prompts).
