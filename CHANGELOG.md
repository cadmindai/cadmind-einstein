# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Each release's spec version (`10000*MAJOR + 100*MINOR + PATCH`) is the
`version_key` the validator sets weights with.

## [Unreleased]

## [0.4.0] - 2026-10-21

### Added
- **Miner stats** (`cadmind_einstein/stats.py`): `POST /v1/miners/stats` (signed by the miner, answer signed by the
  validator) answers everything that affects a hotkey's weight: standing and its share of the current vector,
  counters over 24 hours, 7 days and lifetime (and over its coldkey's hotkeys), its last 100 jobs with every score
  part, the client's signals (downloaded yes/no, stars, changes, re-runs, refunds), the validators' votes with their
  evidence, disable counts against their thresholds, 30 days of deliveries, and the weight formula with its
  constants. Lifetime counts outlive the 7-day window through a daily rollup written at each hour's close.
  `GET /v1/stats/miners?hotkeys=` (Bearer, up to 20 hotkeys) serves the platform's miner dashboard;
  `GET /v1/stats/network` is the public leaderboard. `cadmind-miner-stats` prints your stats and checks the
  validator's signature. Imports are not counted yet (they arrive with project reuse); the answer says so.
- **Refunds as signals**: `POST /v1/signals {kind: "refund", quality, trust}`. A refund granted for the delivery's
  quality counts as a one-star rating in its place among the ratings; any other refund is recorded, not scored.
- **Quality votes, in shadow** (`cadmind_einstein/review.py`, `votes.py`, `models.py`, `calibration.py`;
  `DESIGN.md` section 13, new `docs/VALIDATOR.md`): the reference reviewer -- the rubric `cadmind-einstein-rubric/1`
  (R1 0.35, R2 0.30, R3 0.15, R4 0.10, R5 0.10), its prompt and digest, strict replies with verbatim R1 quotes,
  prompt-injection measures, review bundle checks, signed votes and the average across validators. The validator
  reviews every delivery the platform took (`--votes shadow`, the default; `--review-model`, `--review-key-file`,
  `--review-samples`, `--review-effort`, `--review-daily-usd` with an ALERT at the cap, `--review-usd-per-mtok-*`),
  publishes the votes in the hourly report (scores and signatures, never the words) and shows them to miners; in
  shadow they change no score. `cadmind-review` runs the reviewer on a bundle with your own model and key.
  `cadmind-recompute` checks every published vote's signature and value. The calibration set's structure and the
  go/no-go a model must pass before votes count.
- Optional dependencies `review` (the Anthropic and OpenAI SDKs), only for a validator that votes.

### Changed
- The score has a baseline `B = u*V + (1 - u)*I` between the immediate part and the client's signal:
  `s = 0.4*I + 0.6*(w*C + (1 - w)*B)`. In shadow `u = 0`, so `B = I` and every score is what it was. Job parts gain
  `T`, `V`, `u` and `B`.
- **The weight vector is never empty while a miner is admitted** (under Null consensus an empty vector spreads the
  miners' emission over every registered UID): an hour whose rules weight nobody sends the last non-empty vector
  again, or, before any, equal shares per coldkey among the admitted, reachable miners. With the default genesis
  this replaces "before the first delivery nothing is weighted". Reports publish `last_vector` and
  `vector_by_hotkey`; an hour with nothing to weight logs an ALERT.
- `DESIGN.md` section 10 describes Null consensus (one weight setter: the largest stake); the principle "there is no
  LLM grader" is restated: no model decides delivery, model votes score miners.

## [0.3.12] - 2026-10-21

### Changed
- The reference miner allows Claude Code's web search: `WebSearch` is no longer in `--disallowedTools` and is
  pre-approved in `--allowedTools`. A part that fits around a product needs that product's drawings (the positions
  and sizes of its buttons, ports, camera and other protrusions), and a spec page's outline was all the agent could
  find without it. Web search runs on the model provider's side and touches nothing on the miner's machine.
  `WebFetch` stays denied: every page the agent uses is downloaded with `fetch_to_workspace` (or the hosted
  `fetch`), so it is stored in the project and cited. Every other built-in tool stays denied.
- The reference miner's prompt no longer says to use ONLY the Watt MCP tools: it names the Watt tools, the local
  `fetch_to_workspace` tool and web search, says to search the web for drawings and dimensions of every product the
  device must fit, to store each page used with `fetch_to_workspace` (or with the hosted `fetch` when the local
  tool is not there, `--no-local-fetch`) and cite it, and that a spec sheet's overall size is not enough for a part
  that fits around a product.
- `docs/MINER.md` and `SECURITY.md` say why web search is allowed and web fetch is not.

## [0.3.11] - 2026-10-20

### Changed
- The platform's MCP tools now answer a finished `watt` run, a fetch, an upload and a part lookup as short text:
  every current error first, one line each (code, location, what was found, the fix), grouped by code and fix;
  a command run again in the workspace marks the new errors and counts what else is unchanged since its last run.
  `detail: true` answers the full JSON. The REST routes answer the full JSON as before. `docs/MINER.md` says so.
- The scripted miner (`cadmind-einstein-fake-miner`) asks `detail: true` on `watt` and `watt_wait`, since it reads
  the JSON.
- The local `fetch_to_workspace` tool answers the platform's text for the upload (with the url it was asked for when
  it was redirected). A platform that gives no text answer is a tool error that names what was stored.
## [0.3.10] - 2026-10-20

### Changed
- The reference miner's prompt says there is no hand-over: an unfinished project earns nothing. When the remaining
  work looks too large, the agent starts its most valuable piece (one control, one hold), builds and tests it, then
  takes the next; the only endings are `DONE`, `REJECT`, or `time_left` under 5 minutes. It no longer says "never
  stop or hand over early because you think time is up", which left stopping because the work looked too large open.
- The reference miner's prompt no longer asks for an `UNFINISHED: <what blocks delivery>` line when time runs out
  (with "your work is kept, and the client may pay for a round that continues it"): it read as an exit.
- Without `DONE`, the reference miner's `summary` is the agent's final text (whitespace collapsed, the last 2000
  characters), or an `UNFINISHED:` line when an agent still writes one.

### Added
- The reference miner resumes an agent that ends without `DONE` or `REJECT` while more than 10 minutes remain: once,
  in the same session, told how many minutes are left and to continue with the next most valuable piece, until
  2 minutes before the deadline. Only Claude Code's CLI is resumed (`--session-id` on the first run, `--resume` on
  the second: `ClaudeSession`); an `--agent-command` agent is submitted as it ended, and the miner logs why.

## [0.3.9] - 2026-10-20

### Changed
- A round ends after one attempt the miner worked that does not deliver (a gate failure, or late and failing):
  the result is `not finished`, not charged, naming the validator's fork of the project (kept, no longer deleted),
  the gate's `stage` and `error`, and a `summary` of what blocks delivery -- the builder's own words when it gave
  them (`summary_by: "builder"`), else a sentence from the gate (`"gate"`). It no longer goes to another miner from
  scratch, so the `out of reach` ending is no longer reached. Not-accepted timeouts, copies and rejections keep their
  rules (another miner, from scratch; two coldkeys to decline). Scoring is unchanged: a timeout scores 0 at once and
  a held gate failure counts 0 unless the job's opinions came from two coldkeys.

### Added
- Continuations: a claim may carry `continuation: {base}`, its `seed` the unfinished project and `base` the
  operator's workspace of the last delivered version (`""`: none). The validator grants it with
  `continuation: true` (and `base`), the platform answering a read-only fork of the base (`reference`); `work` says
  `continuation: true` and `base: {kind: "delivered", workspace}` or `{kind: "brief"}`. The copy check subtracts the
  base's features as well as the seed's.
- `submit` takes an optional `summary`. The reference miner ends an unfinished job with `UNFINISHED: <what blocks
  delivery>` and sends it as the summary; its prompt says what a continuation is and how to start over.
- The hourly report's `watch.endings_by_coldkey` counts `not_finished` endings.

## [0.3.8] - 2026-10-20

### Changed
- The reference miner's prompt says a change round is delivered only when the whole project passes `watt status`:
  failures inherited from a version delivered under older checks are part of the job, and time left is only what
  `time_left` says.

## [0.3.7] - 2026-10-20

### Changed
- The reference miner's style-round prompt asks its agent to list every visible difference between the picked
  image and the delivered render and, for each, build it or record a decided agent decision saying why not; sizes are
  free in a style round, so a feature too thin to round can be thickened.

## [0.3.6] - 2026-10-20

### Added
- Style rounds (cadmind-watt ADR-020): a claim may carry `style.baseline`, the settled contract the platform's
  `watt style begin` answered for the delivered version the round reshapes. The validator's gate refuses a delivery
  whose own `watt report` shows another contract digest, another set of bought and world items, or any item more than
  0.001 mm or 0.001 degree from where it was delivered (stage `style`; `cadmind_einstein/style.py`). `work` says
  `style: true`, and the reference miner's prompt says what a style round may change. The no-questions check leaves
  a style round's settled client decisions open: exactly the baseline's `settled_decisions`, and none when the
  baseline does not name them.

### Changed
- The reference miner's prompt tells its agent where the client's photos are (`brief/photo-*`, named in the brief,
  and a photo sent with a change, listed by `client_answers` as kind `photo`) and to look at each with `view_image`.
  The platform now writes a client's photos into the job workspace's `brief/` (with the brief, with a change while the
  job is built, and with a change on the result), as Watt's own designer gets them; before, they never reached the miner.
  MINER.md says the same.

## [0.3.5] - unreleased

### Added
- A miner is told when the client cancels its job: `work` answers `cancelled: <job>` until the miner accepts another
  job, and `accept`, `submit` and `reject` of that job answer 410. The reference miner stops its agent (the whole
  process group) when the job is cancelled or no longer its own, and submits nothing for it.
- After a cancel the miner gets no new job for `--cancel-cooloff-s` (45 s); its first assignment after the cancel, if
  not accepted in time, scores nothing and does not count toward the job's attempts (kind `stale`).
- A cancel while the job is being judged stops the validator's guard run and deletes the validator's fork. Every fork
  that delivers nothing (a failed gate, a copy, a late attempt, a job that could not be judged) is deleted; a
  delivered fork is kept.
- `--gate-run-s` (1800 s): the budget of one validator guard run. Past it the validator stops the run, so it no longer
  holds the validator's run slot, and counts a gate timeout, as for a run Watt stopped at its wall-clock limit; the
  second gate timeout fails the attempt.
- `--judge-max-retries` (20) and `--judge-give-up-s` (4 h): a judgement that keeps failing on the validator's side
  ends the job "didn't finish", not charged, nobody scored, with an ALERT in the log.
- Agent keys: the validator mints at most `--mint-per-hour` (40) keys in all and `--mint-per-coldkey-hour` (12) per
  owner, below the platform's own ceiling; past them it answers 429 without asking the platform. Revocations are
  never counted.

### Changed
- Judging errors on the validator's side -- rate limits (429), Watt busy (409), the validator's own quotas, timeouts,
  5xx, an unreachable platform -- are retried with an exponential back-off (15 s, or 5 s for the validator's busy
  key, doubling to 10 min, with jitter), never counted against the miner and never sent to another miner. A run
  Watt stopped at its wall-clock limit is a gate timeout, no longer an immediate gate failure.
- An export the gate cannot read fails the attempt's gate; any other unexpected error while judging is retried with
  the same back-off, never in a tight loop.
- A delivered result the platform answers `charged: false` (the client cancelled while it was sent, or the delivery
  could not be kept) has its scores withdrawn and is not sent again.
- `agent_key` checks the X25519 public key before anything else (400, and no place taken in the rate limit), and a
  hotkey whose live key was minted less than 10 minutes ago gets 429 instead of a new key.
- The reference miner denies Claude Code's built-in tools (`--disallowedTools`): `--allowedTools` only pre-approves
  the MCP tools, it does not remove the shell, file and web tools. It builds nothing for an assignment whose accept
  was refused.

### Compatibility
- The validator reads the platform's `{accepted, charged, label}` answer to a result; a platform that answers without
  `charged` keeps the earlier behaviour.
- A miner should stop working on a job when `work` names it in `cancelled`; a miner that does not is not penalised for
  the first assignment it misses.

## [0.3.0] - unreleased (proposed)

### Added
- The client's answers: while a job is built its client sees the builder's open client decisions as questions and
  may answer them (or ask for a change); the platform writes each into the job workspace's `brief/` as the dated
  file Watt's own designer gets (`brief/answer-<date>-<decision>-<HHMMSS>.md`, `brief/change-<date>-<HHMMSS>.md`).
  The validator's gate now checks, on its own export of its fork (`cadmind_einstein/answers.py`), that every answer
  that arrived before the hand-over is recorded in its decision (not open, an `answer.from` quoting the file, the
  option picked as its choice); an answer after the hand-over never counts against the miner.
- No-questions jobs: a claim's `no_questions: true` (the client asked not to be asked; every developer-API job) is
  on the miner's `work` answer, and the gate refuses such a delivery while a client decision is open.
- The miner's receipt lists its recent attempts that did not pass, with why (`failures`).
- The reference miner's prompt checks `client_answers` after each `watt` command, records each answer and re-runs
  what it reaches, checks once more before it submits, and in a no-questions job decides every would-be question
  itself (`{questions}` and `{no_questions}` in a `--prompt-file` template).
- `fetch_to_workspace`, a local fetch tool for the reference miner's agent (`cadmind_einstein/fetch_mcp.py`, a stdio
  MCP server named `cadmind-fetch`, also `cadmind-einstein-fetch-mcp`): it downloads a source from the miner's own
  connection, which many sites answer when they refuse the platform's address, and uploads it into the job's
  workspace through the platform's upload route (`upload_source`). Standard library only; http and https to public
  addresses only, every redirect checked, 5 s to connect, 20 s and 8 MB per download. The reference miner writes it
  into its MCP config and allows it beside the Watt MCP tools; `--no-local-fetch` leaves it out.

### Changed
- The reference miner's prompt prefers `fetch_to_workspace` for makers' pages and datasheets (the hosted `fetch`
  remains), and asks for `fetched_by = "builder"` on the source records of what it stored.
- The reference miner's prompt runs the full mutation guard only after a passing scoped guard that covers every
  must requirement, never as a first check.

### Compatibility
- Miners must check the client's answers (the platform's `client_answers` tool) and record them, and decide every
  question in a no-questions job: a delivery that ignores an answer no longer passes the gate. Deploy the
  platform's answer relay before this validator.

## [0.2.0] - 2026-10-14

### Added
- Bittensor-style command-line flags on the validator and the reference miner: `--wallet.name`, `--wallet.hotkey`,
  `--wallet.path`, `--subtensor.network`, `--subtensor.chain_endpoint`, `--netuid`, `--logging.debug`,
  `--logging.trace`, and `--version`. `cadmind-recompute` accepts `--subtensor.network` and
  `--subtensor.chain_endpoint`.
- `__spec_version__`, derived from `__version__`, used as the weights `version_key`; a warning when the subnet's
  `weights_version` hyperparameter requires a newer validator.
- A release check: at startup (and daily on the validator) the latest GitHub release is read and a newer one is logged
  as a warning. It never downloads or runs anything; `--no-version-check` turns it off.
- The reference miner, given `--netuid`, checks once at startup that its hotkey is registered (a warning only).
- Docker: a `Dockerfile` and `deploy/docker/compose.yml` (validator and miner profiles).
- pm2: `deploy/pm2/ecosystem.config.js`.
- An opt-in updater, `scripts/auto_update.sh`, with a systemd user service and timer.
- `min_compute.yml`, GitHub Actions CI (ruff and pytest), `SECURITY.md`, `CONTRIBUTING.md` and this changelog.

### Changed
- Log output goes through the standard `logging` module (bittensor 11 has no `bittensor.logging`), with the same line
  prefixes as before.
- The weights' `version_key` is the validator's spec version rather than the subnet's current `weights_version`.

### Fixed
- A validator whose port is already taken now reports the bind error instead of an `AttributeError`.

### Compatibility
- Every earlier flag still works: `--wallet-name`, `--wallet-hotkey` and `--network` are aliases of the new names.

## [0.1.0]

The first public release.

- **Validator** (`cadmind-einstein`): claims client jobs from the CADmind platform's queue and routes each to one
  admitted miner; grants, freezes and forks job workspaces; gates the miner's result on the hosted Watt check,
  including the validator's own run of the mutation guard; reassigns failed, timed-out or copied jobs to another
  miner; delivers passing results; records the client's reaction; scores jobs from the gate, efficiency, timeliness
  and the client's signals; sets weights hourly within the rate limit (plain or commit-reveal); publishes hourly
  reports and commits each report's SHA-256 on chain.
- **Miners**: a reference agent miner (`cadmind-einstein-miner`) that runs an agent CLI restricted to the hosted Watt
  MCP server, and a scripted miner (`cadmind-einstein-fake-miner`) for local end-to-end runs.
- **Identity**: hotkey-bound agent keys, sealed to the miner (X25519, HKDF-SHA256, AES-256-GCM); every answer on a
  miner route signed by the validator's hotkey.
- **Anti-abuse**: copy detection on normalised features, second opinions across coldkeys, rejection telemetry,
  admission tests, escalating disables, and a `watch` section in every report.
- **Auditability**: `cadmind-recompute` rebuilds every score and the weight vector from the published reports and
  checks them against the on-chain commitment.
- Durable state in one SQLite file; operator credentials split by role; a hardened HTTP face meant to sit behind a
  TLS reverse proxy.
