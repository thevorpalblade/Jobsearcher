# M10: several candidates, logins, and internet access (plan, 2026-10-07)

Today the app serves one candidate on the LAN with no login. The goal: a second job
seeker with their own CVs, roles, rankings, drafts and applications; one admin (the
server's owner); both reached over the internet via HTTPS.

Decisions (from the user, 2026-10-07):

- The second user is **another job seeker**.
- Roles are **admin + regular users**. The admin may see every profile.
- Access is the **public internet at the user's domain**, with HTTPS and a login. This
  replaces PLAN.md's "LAN or Tailscale only".
- **Each person brings their own API keys and chooses their own models.** This
  replaces the shared $20 budget for that person.
- **Each person sets their own region.** All jobs go into the one shared local
  database.
- **The chat is local only.**
- **Two-factor codes for the admin only**, for now.

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
- **Prefilter per person, over the whole shared DB (decided).** Each profile's
  keyword filters pick its candidates from every open job in the shared database, not
  only from what was fetched for it: its target roles and keywords, excluded
  keywords, its region and occupation filters. Only jobs that pass go to that
  person's ranking.
  - The prefilter is free (no LLM), so it runs daily for every profile and whenever
    the person changes their filters.
  - A job fetched for one person also reaches the other if it passes their filters.
- **Personal ranking:** loop over profiles, each with its own CVs, `ranking.yaml`,
  model and key, through the same `run_ranking`. A job ranked for two people costs
  two calls, one on each person's key. Rankings are cached per profile, so changing
  one person's CV or roles re-ranks only their list.
- **Onboarding and filter changes:**
  - A new profile's first run covers every open job in the DB that passes its
    prefilter, not just new ones.
  - Before ranking, the settings page shows how many jobs pass and the estimated cost
    with the chosen model, so the person can tighten the filters first.
  - Their budget limit applies as usual, and ranking stops when it's reached.
- **News signals:** classified per profile (the prompt holds that profile's CV and
  preferences), only for that profile's companies.
- **Models and budget are per profile.** See section 3a. Each profile's ranking,
  drafting and signals run with that profile's models and keys, and count against its
  own monthly budget.
- **Regions:** each profile sets its own `locations`. The search fetches the union,
  and every job is stored in the shared DB whichever profile it was fetched for. A
  profile only ranks and shows jobs inside its own region.

## 3a. API keys and models per profile

- **Models:** `profile.yaml` has its own `llm:` section (ranking, drafting, grounding,
  grounding_fallback, monthly_budget_usd). Anything left out falls back to
  `config.yaml`, but only for profiles the admin allows (`use_server_keys: true`).
  The admin's own profile is set up that way, so today's setup keeps working.
- **Keys:** stored in `profiles/<slug>/secrets.env` (mode 0600, gitignored). They are
  not kept in the DB, so a copied or backed-up DB carries no keys.
  - The user enters keys on their settings page. The page is write-only: it shows
    "set, ends in …a1b2" and offers Replace and Remove, never the key itself.
  - The daemon and the draft runner read the file when they build that profile's
    clients.
  - Encrypting the file would add little, because the decryption key would have to
    live on the same server.
- **Providers a regular user may pick:** moonshot, zai, nvidia and anthropic, with
  their own keys.
  - `claude_code` is the admin's personal Claude subscription. Using it for someone
    else's work would break the subscription's terms, so it's admin-only.
  - `ollama` shares the GPU, which overheats under long runs, so it's admin-only
    unless the admin allows it.
- **A "Test" button** next to each model sends one tiny request, like `llm-check`,
  and shows the result.
- **Rate limits** are per key, so the process-wide limiter becomes keyed by provider
  and key (`limiter_for(name, key_id, rpm)`). Two people on NVIDIA's free tier don't
  slow each other down.
- **Usage:** `llm_usage.profile` makes the budget and the dashboard's budget bar
  per profile. Shared work such as news fetching isn't billed, because
  classification runs per profile.

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
- **Two-factor (TOTP) for the admin only** for now. It's RFC 6238 in ~30 lines of
  stdlib (hmac, base64, struct), set up by scanning a QR code; a `totp_secret` column
  is kept per user so others can opt in later. Recovery: `jobsearcher users reset-2fa`
  from the CLI.
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
on the server. **Decided: the chat is local only.**

- **Who:** the admin only. Requests that come through Caddy (the public side) are
  refused, even for the admin.
- **How "local" is decided:** the app serves two listeners, and the chat is decided by
  which one a request arrived on (the server port), never by headers a client could
  forge.
  - **Public:** `127.0.0.1:8081`, which only Caddy can reach. Chat off.
  - **LAN:** `192.168.1.197:8080`, for the home network. Chat on for the admin. The
    router never forwards 8080.
  - Both need the login.
- **On the public site:** the chat panel and routes are hidden and return 404.

## 7. Exposing it

- **Caddy** on the host gets Let's Encrypt certificates automatically and proxies to
  the public listener, `127.0.0.1:8081` (section 6). The app stops binding `0.0.0.0`.
- Trust `X-Forwarded-For` only on the public listener (from Caddy), for the per-IP
  login throttling.
- **Domain:** the user's domain, which resolves to the home's public address, the router's public
  address. Set `web.allowed_hosts` to include it.
- **Needs from the user:** forward ports 80 and 443 on the router to this machine
  (192.168.1.197), and install Caddy (`pacman -S caddy`). Its Caddyfile is in essence
  `the user's domain { reverse_proxy 127.0.0.1:8081 }` plus the headers.
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
| 1 | Profiles in files, DB and pipeline; migration; one profile | Same app, data now per profile | **Built 2026-10-07** (branch `m10-profiles`); see "Phase 1 as built" |
| 2 | Users, sessions, login/logout, middleware, CLI user commands, roles | Login required, still LAN only | **Built 2026-10-07** (branch `m10-auth`); see "Phase 2 as built" |
| 3 | Per-profile web UI, per-profile models and keys, admin user page, profile switcher, `/account`, admin TOTP | Two candidates usable | Medium to large |
| 4 | Caddy, headers, Origin checks, throttling, chat lockdown, security review, outside tests | On the internet | Small to medium |
| 5 | Onboard the second user: profile, CV upload, roles form, first ranking run | Second candidate live | Small |

Phases 1–3 can be built and tested on the LAN; the port opens only after phase 4.

## Phase 1 as built (2026-10-07)

- **Config:** `Config.profiles_dir` (default `profiles/` next to config.yaml),
  `profile_slugs()`, `for_profile(slug)`. Without a profiles folder there is one
  profile, `default`, made of config.yaml's paths, so other setups and the tests are
  unchanged. `profile.yaml` holds the name and search settings
  (`profile.example.yaml`).
- **Store:** `Store(path, profile=...)`. The migration rebuilds the four per-profile
  tables with a `profile` column (old rows become `default`) and adds
  `llm_usage.profile`. It runs once, on the first open. `rename_profile()` is used by
  the migration command.
- **Pipeline:**
  - `search` fetches once with every profile's keywords and companies, and keeps a
    job if it's in any profile's region.
  - `rank` and `signals` run per profile. `rank` applies the profile's own region and
    excluded words (`run_ranking(wanted=...)`).
  - Each profile has its own news-fetch timestamp (`signals:<slug>`).
  - The daemon and `run` loop over every profile. `--profile` picks one for the
    per-profile commands.
- **Drafts** go to `data/drafts/<slug>/`.
- **Web:** shows the first profile, or `jobsearcher web --profile <slug>`. The
  Settings page edits that profile's `profile.yaml`.
- **Moving the live setup over:**
  1. Stop the services.
  2. Back up `data/jobsearcher.db`.
  3. Run `jobsearcher migrate-profiles jenny`. It moves `ranking.yaml`,
     `companies.yaml`, `cvs/` and the search section into `profiles/jenny/`, assigns
     the database rows to `jenny`, and moves the drafts.
  4. Restart the services.

  A rehearsal on a copy of the live data kept all 575 current rankings (nothing
  re-ranks), the 7 tracked applications and the drafts. Every page and a draft
  download worked.
- **Not yet:** a per-profile LLM budget (phase 3; usage is already recorded per
  profile, but the $20 limit is still shared), and per-profile backups (still
  `data/backups/`).

## Phase 2 as built (2026-10-07)

- **`jobsearcher/auth.py`:**
  - scrypt password hashes.
  - Users, one-time invite links (24 h), server-side sessions (14 days idle, 30 days
    at most). Tokens are stored as SHA-256.
  - Login throttling: 5 failures per username or 20 per address in 15 minutes.
  - An audit log of logins, failed logins, password changes and account changes.
  - New tables: `users`, `sessions`, `login_failures`, `audit_log`.
- **Web:**
  - A middleware sends anyone without a session to `/login`. HTMX requests get a 401
    with `HX-Redirect`. Only `/login`, `/invite/*`, `/static/*` and `/healthz` are open.
  - A second middleware refuses cross-site writes (`Origin`, `Sec-Fetch-Site`).
  - The session cookie is HttpOnly and SameSite=Lax, and Secure once the request comes
    over HTTPS (phase 4).
  - Pages: `/login`, `/logout`, `/invite/<token>`, `/account` (change password, which
    logs out other devices).
- **Profiles per user:** the app keeps one state per profile (`Shared.state_for`), and
  each request gets its user's profile, so every page, form, draft and file is that
  candidate's. The draft queue is shared, one draft at a time, but each profile has
  its own keys.
- **Admin only** (404 for others): the chat, the status page and `config.yaml`. The
  admin sees the profile set on their account (a switcher comes in phase 3).
- **CLI:** `jobsearcher users add <name> [--admin] [--profile <slug>] [--url ...]`,
  `invite <name>` (a new link, e.g. a forgotten password), `disable`, `enable`, `list`.

## Still open

- **Decided:** the first profile is `jenny`. The new profile's first run ranks
  everything in the DB that passes its prefilter (section 3).
- **Assumed, not confirmed:** the admin has no job-seeker profile of their own. They
  log in as admin and switch into `jenny` or the second profile. Jenny gets her own
  regular login.
