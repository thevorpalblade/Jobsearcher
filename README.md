# Jobsearcher

Finds open positions (Platsbanken and JobTech Links for now), ranks them
against your CV with an LLM (Kimi or Claude, configurable per stage), drafts
tailored applications, and shows it all in a local web UI. See
[PLAN.md](PLAN.md) for the design and roadmap.

## LLM providers

Set per stage in `config.yaml` (`llm.ranking` / `llm.drafting`):

| `provider` | What it uses | Credential in `.env` |
|---|---|---|
| `moonshot` | Kimi API, pay per token | `MOONSHOT_API_KEY` |
| `anthropic` | Claude API, pay per token | `ANTHROPIC_API_KEY` |
| `claude_code` | Claude Code CLI on your Claude Pro/Max subscription | `CLAUDE_CODE_OAUTH_TOKEN` |

For `claude_code`, run `claude setup-token` once on any machine with a browser
(install Claude Code first) and paste the printed token into `.env`. The Docker
image already contains the CLI.

## Run with Docker (home server)

```sh
cp config.example.yaml config.yaml   # locations, sources, LLM providers
cp ranking.example.yaml ranking.yaml # target roles, preferences, score weights
cp companies.example.yaml companies.yaml  # target companies: careers sites + news signals
cp .env.example .env                 # add API keys / CLAUDE_CODE_OAUTH_TOKEN
mkdir -p cvs data                    # put your master CV in cvs/master.md
docker compose up -d --build         # runs the search now, then daily at 06:00
docker compose run --rm jobsearcher list        # ranked jobs, best first
docker compose run --rm jobsearcher llm-check   # verify LLM keys (costs a fraction of a cent)
docker compose logs -f
```

`config.yaml`, `.env`, `cvs/` and `data/` hold personal data and are gitignored.

## Web UI

`docker compose up -d` also starts `jobsearcher-web` on port 8080
(`http://<server>:8080`): the ranked job list with filters, a page per job
(score breakdown, rationale, contacts with their source, apply links), the
prefilter's view of each target role's occupations, LLM spend and pipeline
status. On a job's page you can mark it shortlisted, applied, interview,
rejected or ignored and keep notes; `Tracked` lists those jobs, expired ones
included, and ignored jobs drop out of the main list. Edits to `ranking.yaml` weights and adjustments show up on the next page
load, with no re-ranking.

There is no login: keep it on your LAN and use Tailscale or WireGuard from
outside. **Docker's published ports bypass ufw and firewalld**, so on a server
with a public IP, set `WEB_BIND` in `.env` to a LAN or Tailscale address
(e.g. `WEB_BIND=192.168.1.10`) instead of the default `0.0.0.0`.

Outside Docker: `jobsearcher web` serves it on `127.0.0.1:8080` (`--host`,
`--port`, or `web:` in `config.yaml`).

## Arch Linux server setup

```sh
sudo pacman -S --needed docker docker-compose git
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"      # log out and back in afterwards
git clone <this repo> jobsearcher && cd jobsearcher
```

The container runs as uid 1000 (the first user on most Arch installs). If your
user has a different uid, run `sudo chown -R 1000 data` so the container can
write its database.

## Run locally

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
cp config.example.yaml config.yaml && cp ranking.example.yaml ranking.yaml && cp companies.example.yaml companies.yaml
jobsearcher search          # fetch jobs into data/jobsearcher.db
jobsearcher rank            # score new jobs against cvs/master.md
jobsearcher run             # search + rank (what the daemon does daily)
jobsearcher list            # score, ✉ = has contact info
jobsearcher show <job-id>   # full record + ranking rationale as JSON
jobsearcher llm-check       # test the configured Kimi/Claude models
jobsearcher budget          # LLM spend this month
jobsearcher web             # web UI on http://127.0.0.1:8080
jobsearcher companies --detect  # target companies (companies.yaml): ATS found, open jobs
jobsearcher signals         # company news → companies worth a spontaneous application
pytest && ruff check .
```
