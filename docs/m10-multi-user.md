# M10: several candidates, logins, and internet access (plan, 2026-10-07)

Today the app serves one candidate on the LAN with no login. The goal: a second job
seeker with their own CVs, roles, rankings, drafts and applications; one admin (the
server's owner); both reached over the internet via HTTPS.

Decisions so far (from the user): the second user is **another job seeker**; roles are
**admin + regular users**; access is **public internet, HTTPS + login**. This replaces
PLAN.md's "LAN or Tailscale only".

## Shape

- **Profile** = one candidate's search: CVs, `ranking.yaml` (roles, preferences,
  prefilter, weights), `companies.yaml`, search keywords and locations, and everything
  derived (rankings, drafts, applications, signals).
- **User** = a login. A regular user owns exactly one profile and sees only it. The
  admin manages users and global settings and can switch into any profile (to help);
  regular users are told that the admin can see their data.
- **Shared** between profiles: the job pool (ads are public and fetched once), sources,
  company ATS detection, news items, the LLM setup, the budget and the schedule.

## 1. Per-profile files

```
config.yaml                    # global: llm, sources, crawl, schedule, web, auth, chat
profiles/<slug>/profile.yaml   # display name, search keywords/locations/excludes, budget share
profiles/<slug>/ranking.yaml
profiles/<slug>/companies.yaml
profiles/<slug>/cvs/*.md       # master.md first, as today
data/drafts/<slug>/...         # drafts and backups also move under the profile
data/backups/<slug>/...
```

- `profiles/` is gitignored like the files it replaces.
- **Migration:** a one-off `jobsearcher migrate-profiles` moves today's `ranking.yaml`,
  `companies.yaml`, `cvs/` and `search:` section into `profiles/<slug>/`, and moves
  `data/drafts` into `data/drafts/<slug>/`.
- **Compatibility:** without a `profiles/` folder the app runs as one implicit
  profile, so tests and other setups keep working.
- **Code:** `Config` gains `profiles() -> list[Profile]`. Code that reads `cv_path`,
  `ranking_config` or `companies_config` takes a `Profile` instead (cli rank, signals,
  drafting, web settings, the benchmark script).

## 2. Database

- **Shared tables (unchanged):** `jobs`, `job_sources`, `runs`, `company_ats`,
  `news_items`.
- **Per-profile tables** gain a `profile` column, which goes into their keys:
  - `rankings (job_id, profile, input_hash)`. `input_hash` already includes the CV
    text, so rankings for different candidates can't collide even now. The column
    makes queries and deletion clean.
  - `drafts (job_id, profile, input_hash)`, `applications (job_id, profile)` and
    `signals (item_id, profile)`. Company drafts are keyed `company:<slug>` per profile.
  - `llm_usage.profile` (nullable: shared work such as news classification batches).
- **New tables:**
  - `users (id, username, password_hash, role admin|user, profile, created_at,
    disabled, totp_secret)`
  - `sessions (token_hash, user_id, created_at, last_seen, expires_at, ip, user_agent)`
  - `login_attempts (key, ts)`, for throttling by username and by IP
- **Migrations:** SQLite can't change a primary key in place, so the per-profile
  tables are rebuilt (create new, copy with `profile = '<slug>'`, swap) in one
  transaction. A `PRAGMA user_version` schema version makes it run once. Back up the
  DB file first.

## 3. Pipeline (daemon)

- **Search once** for all profiles: the union of every profile's keywords and target
  roles, and of their locations. Each profile's own location and keyword filters move
  into its prefilter, so the second user doesn't see the first user's cities.
- **Companies:** crawl the union of all `companies.yaml` once. Each profile sees only
  the jobs and signals from its own companies.
- **Ranking:** loop over profiles, each with its own CVs and `ranking.yaml`, through
  the same `run_ranking`. The cost roughly doubles with two profiles, minus jobs that
  only one profile's prefilter passes.
- **News signals:** classified per profile (the prompt holds that profile's CV and
  preferences), only for that profile's companies.
- **Budget:**
  - The global `monthly_budget_usd` still caps everything.
  - `profile.yaml` can set a share, so one profile can't use up the other's ranking
    money.
  - Drafts run on the owner's Claude subscription, so set a monthly draft limit per
    profile (default e.g. 20).

## 4. Authentication

Stdlib where it's enough, so there are no new crypto dependencies to keep updated:

- **Passwords:** `hashlib.scrypt` (n=2^15, r=8, p=1, 16-byte salt), stored as
  `scrypt$n$r$p$salt$hash`. Compare with `hmac.compare_digest`. Minimum 12
  characters, with no other composition rules.
- **Sessions:** server-side.
  - A random 32-byte token goes in a `__Host-session` cookie (Secure, HttpOnly,
    SameSite=Lax, Path=/). Only its SHA-256 is stored, so a leaked DB doesn't leak
    live sessions.
  - Sessions expire after 14 days idle and 30 days absolute.
  - Logout deletes the row. A password change ends the user's other sessions.
  - The web process still needs no secret key.
- **Login throttling:** after 5 failures per username or 20 per IP in 15 minutes,
  further attempts are refused for 15 minutes. Each failure takes a constant ~0.5 s
  (scrypt cost plus a fixed pad) and gives the same message whether or not the user
  exists.
- **Two-factor (TOTP)** for the admin at least: RFC 6238 in ~30 lines of stdlib
  (hmac, base64, struct), set up by scanning a QR code. It's optional for regular
  users. Recovery: the admin resets it from the CLI.
- **CSRF:**
  - Keep the `HX-Request` header requirement on every POST.
  - Also check `Origin`/`Sec-Fetch-Site` against the configured public host, because
    a header check alone is weak once cookies authenticate requests.
  - The login form is a plain POST, so it gets the Origin check plus a per-form token.
- **Accounts from the CLI**, so nothing about this is exposed before login:
  - `jobsearcher users add <name> --profile <slug> [--admin]` prints a one-time
    set-password link, valid for 24 h.
  - `jobsearcher users reset <name>`, `users disable <name>`, `users list`.
  - Users change their own password and TOTP on a `/account` page.
  - There is no email reset, since the app sends no email.

## 5. Authorization in the web app

- **Deny by default:** a middleware sends every request without a valid session to
  `/login`, except `/login`, `/static/*` and `/healthz`. Routes then get
  `current_user` and `profile` from dependencies, never from the URL.
- **Every per-profile query takes the profile.** `Store` methods for rankings, drafts,
  applications and signals get a required `profile` argument, so a forgotten filter
  is a type error, not a leak. Job IDs are shared, so `/jobs/{id}` works for any job,
  but its ranking, draft and application are always the viewer's own.
- **Files:** draft downloads resolve under `data/drafts/<profile>/` with the existing
  path checks. CV pages resolve under `profiles/<slug>/cvs/`.
- **Admin only:**
  - `config.yaml` editing, user management (`/admin/users`) and the status page
  - The profile switcher, which shows an "acting as <profile>" banner
  - The chat (see below)
- **Regular users:** their own dashboard, jobs, drafts, CVs, `ranking.yaml` form,
  companies form and account page.
- **Audit log:** a small table recording logins, failed logins, password and TOTP
  changes, user management and admin profile switches.

## 6. The chat (Claude Code with full permissions)

Once the app is on the internet, a stolen admin password plus the chat means a shell
on the server. Options, from safest:

1. **Chat only from the LAN/Tailscale** (recommended): allowed when the client address
   Caddy forwards is private, refused otherwise, even for the admin.
2. Chat over the internet for the admin, but only with TOTP and a fresh re-login
   within the last 15 minutes.
3. Turn the chat off.

## 7. Exposing it

- **Caddy** on the host gets Let's Encrypt certificates automatically and proxies to
  the app on `127.0.0.1:8080`. The app stops binding `0.0.0.0`.
- Trust `X-Forwarded-For` only from 127.0.0.1, for the IP throttling and the chat
  check.
- **Needs from the user:** a domain or dynamic DNS name, and ports 80 and 443 forwarded
  on the router. Set `web.allowed_hosts` to the domain.
- **Headers:**
  - HSTS (from Caddy)
  - `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'`. The
    templates have no inline scripts and HTMX is vendored, so a strict policy works.
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: same-origin`
- **Ad text:** stays autoescaped; `safe_url` stays the only way a URL becomes a link.
- **Optional:** fail2ban on the audit log's failed logins; Caddy access logs rotated.
- **Before opening the port:** run `/security-review` on the branch. Then test from
  outside: logged-out requests to every route get a redirect or 401, one user can't
  read the other's job, draft, CV or settings by ID, and the login is throttled.

## 8. Privacy

The second person's CV and search history live on your server, and the admin can see
them. Tell them, back them up like the first profile's, and offer full deletion:
`jobsearcher profiles delete <slug>` removes files, DB rows and drafts. CV text goes to
the LLM providers in the config (NVIDIA, Moonshot, Z.ai, Anthropic). Tell the second
user the same as the first.

## Phases

| # | Phase | Result | Size |
|---|---|---|---|
| 1 | Profiles in files, DB and pipeline; migration; one profile | Same app, data now per profile | Large: touches store, cli, ranking, drafting, signals, web settings |
| 2 | Users, sessions, login/logout, middleware, CLI user commands, roles | Login required, still LAN only | Medium |
| 3 | Per-profile web UI, admin user page, profile switcher, `/account`, TOTP | Two candidates usable | Medium |
| 4 | Caddy, headers, Origin checks, throttling, chat lockdown, security review, outside tests | On the internet | Small to medium |
| 5 | Onboard the second user: profile, CV upload, roles form, first ranking run | Second candidate live | Small |

Phases 1–3 can be built and tested on the LAN; the port opens only after phase 4.

## Open questions

- Should the admin be able to see the other users' data? The plan says yes, with a
  visible banner, for support.
- Which domain or dynamic DNS name should it use?
- Budget: does the second profile share the $20 a month, and with what split? A
  monthly draft limit on the subscription?
- Does the second user search the same region? If not, search volume and LinkedIn
  rate limits grow.
- Chat: LAN/Tailscale only (recommended), or over the internet with TOTP?
- TOTP: required for the admin only, or for everyone?
