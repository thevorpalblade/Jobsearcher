# M9: landing dashboard and a chat with Claude Code

Requested 2026-10-05. The user's sister (Jenny, the candidate) uses the web UI and
wants to ask for features, drafts and explanations herself.

## Decisions (user)

- **No login:** the UI stays open on the network; trusted network only.
- **Full access to the repo:** the chat's Claude Code runs in this checkout with
  `--permission-mode bypassPermissions`, can edit `main` and run commands. This is
  remote code execution for anyone who can reach the page; the user accepted that.
- Safeguard added anyway (it doesn't change the decision): the chat is **off by
  default** (`chat.enabled`), and its routes refuse requests whose `Host` header is
  a public-looking DNS name (DNS-rebinding protection), unless listed in
  `web.allowed_hosts`.

## Dashboard (`/`)

- "Welcome, <web.user_name>" (config.yaml; "Welcome" if unset).
- The current **top five** jobs: ranked, open, not already applied/rejected/ignored,
  best score first, linking to the job page; a link to the full list.
- Counts (open, ranked, waiting, new in the last 2 days) and last search time.
- The chat panel, with suggested questions.
- The job list moves to `/jobs` (links, filter form and sort links updated).

## Chat

- Runs the official `claude` CLI headless (`-p --output-format stream-json
  --verbose`), prompt on stdin, `cwd` = the repo, resuming one Claude session per
  chat (`--session-id` first, then `--resume`), `--append-system-prompt` telling
  Claude who it's talking to and the house rules (CLAUDE.md is loaded by Claude
  Code itself): explain plainly, draft into `data/drafts/` grounded in the CVs and
  never invent experience, confirm before changing config/data/services, the live
  daemon and web UI only pick up code changes on restart, don't print secrets.
- Uses the user's own Claude login / subscription (same as the `claude_code` LLM
  provider; never an API key handed to another client).
- Settings (`config.yaml`): `chat.enabled`, `model` (opus), `effort` (medium),
  `permission_mode`, `timeout_s`, `workdir`.
- **One run at a time** across all chats (shared subscription limits, and two
  Claudes editing one checkout would collide); a second message gets "busy".
- Conversations persist in SQLite (`chat_sessions`, `chat_messages`); the browser
  streams a running reply over Server-Sent Events (text, tool activity, done), with
  a Stop button. Messages carry ids so a page reload mid-run never duplicates.
- Only works when the web UI runs from the repo (needs `claude` and the checkout);
  in Docker the panel says it's unavailable.
- Write requests need the `HX-Request` header (the existing CSRF guard).

## Tests (offline)

Stream-JSON parsing from a recorded transcript fixture, the manager with a fake
process (events, persistence, busy, cancel, errors, resume vs new session), the
routes (send/stream/cancel, Host check, disabled), the dashboard (top five filter,
greeting) and the `/jobs` move.
