# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Each release's spec version (`10000*MAJOR + 100*MINOR + PATCH`) is the
`version_key` the validator sets weights with.

## [Unreleased]

### Fixed

- **The OpenRouter judge calls OpenRouter.** With `--judge-backend openrouter`, an `openai:<model>` with no
  `--review-base-url` now goes to `https://openrouter.ai/api/v1`; before, the SDK's default (api.openai.com) was
  called. The same default applies to `cadmind-judge`, `cadmind-review` and `cadmind-calibrate`.
- **The judge's daily cap counts real money.** Each OpenRouter reply's `usage.cost` is what `--review-daily-usd`
  counts; the `--review-usd-per-mtok-*` prices are the fallback for a reply without a cost. A capped validator
  whose provider reports no cost and that has no prices set refuses to start, and a reply with neither stops the
  day's calls with an ALERT (before, every call counted $0 and the cap never tripped).
- **`cadmind-recompute` reads the validator's own commitment.** The validator commits one `Sha256` field, and
  recompute only parsed a `sha256:<hex>` text, so it reported "no sha256 commitment found". It now reads the field,
  and the text form still reads.

### Added

- **Each hourly report names the einstein version that computed it** (`einstein: {version, spec_version}`; part of
  the hashed bytes). `cadmind-recompute` recomputes a report only as the version that made it would: a report from
  another version (or from before this field) whose numbers differ is listed under `not_checked` with a plain note,
  not called a mismatch; its hash and chain links are still checked.

## [0.5.2] - 2026-10-26

### Changed

- **No effort levels.** Every job has one deadline: 120 minutes from the builder's accept
  (`--job-deadline-min`, default 120). The builder may still submit sooner, and the client can still run
  another round on the job. The job's `effort` is gone from `work`, job status and the hourly report; an
  `effort` the platform still sends with a claim is accepted and ignored, and older saved jobs that carry one
  still load.
- **Efficiency compares like with like by brief size alone** (the bucket was effort and size).
- **The entry test** has the same 120-minute deadline. `--admission-effort` is still accepted, so an
  existing service file keeps working, and it does nothing.

## [0.5.1] - 2026-10-25

The validators' judge is now `cadmind-judge/1.3`, approved by the subnet owner after a calibration re-judge.

### Fixed
- A result the platform refuses for one job (a 503 with that job's own code) no longer holds the validator's whole
  queue role: only that job waits, with a growing delay (10 s doubling to 5 min, kept across restarts), and it is
  sent after the others. A long-refused result is logged once as an ALERT. A 429 `too_many_in_flight`, or a 503
  with no code or `busy`, still holds the role.

### Added
- **`cadmind-judge/1.3` and `1.3-tools`** (prompt `judge_prompt_1.3.md`): 1.2b plus the fact sheet's
  `assumptions` section (review facts format /3: what the builder decided in the client's place, why it could not
  be known, what the design was built for, the bought parts' unpublished facts, the facts to verify on the part, and
  whether the client could be asked) and rules on it. An assumption counts against the device only when it was
  avoidable (the client's words or photos, or a maker's data, give the fact; or the builder could ask and did not)
  or the design is fragile to it (it works only at the guessed value where a reasonable design covers the plausible
  range). An unavoidable, stated assumption the device covers is not a failure and is never a nudge. The blind turn,
  the schemas, the caps and the sampling are 1.2b's.
- `judge.RELEASED`: the versions a validator may vote with (`1.3`, `1.2b`; the tools judges are refused). Any
  other version is for offline re-judging; `for_validator` refuses it. The default `--judge` is `1.3`.

### Unchanged
- `cadmind-judge/1.2b` and its digest. On a /3 fact sheet 1.2b withholds `assumptions` from the model, so it sees
  exactly what a /2 fact sheet gave it.

## [0.5.0] - 2026-10-24

Proposed as **0.5.0**: the vote protocol and the value scale change. A change to the judge needs the subnet
owner's approval (DESIGN.md section 13).

### Added
- **CADmind's device judge** (`cadmind_einstein/judge.py`, the prompt `judge_prompt.md` as package data; owner
  decision 2026-10-24). One core, four modes (`rate`, `nudge`, `check`, `final`), one entry point `judge(mode,
  inputs, model, version=...)`. One sample is one two-turn conversation: a **blind turn 1** (the client's words and
  photos only: use steps and 5-8 "useless if" lines, 1-4 of them the main job), then the pictures and the fact sheet
  (rulings, at most 3 nudges, a score 0-3). The **main-job cap** is in the prompt and applied mechanically. Two
  versions, switched by configuration (`--judge`, `Judge(version=...)`), each with its own `JUDGE_VERSION` and
  digest (prompt, schemas, switches, sampling, aggregation, constants, injection lists):
  - `cadmind-judge/1.2b`: no tools, 3 samples, the lower median, a nudge kept on 2 of 3. The validators' judge.
  - `cadmind-judge/1.2b-tools`: read-only Watt instruments (`entities`, `inspect`, `measure`) on the delivered
    project in a workspace, at most 12 calls (configurable), locked until turn 1 is answered, output scrubbed of
    instruction-like text; ONE sample, taken as it is. For CADmind's pipeline and the miner-side reviewer only. The
    read-only rule is enforced by our client (`judge_tools.ReadOnlyWatt`, four allowed requests), not by the key.
- **`--judge-backend openrouter|claude-cli`**: a validator's judge runs on its review key (`openrouter`, the
  default) or on a Claude subscription (`claude-cli`: `claude -p` isolated as the reference miner, no tools,
  `ANTHROPIC_*`/`OPENROUTER*` stripped from its environment). The subscription's dollars are notional list prices,
  archived as `usd_kind: notional-subscription` and counted toward the same `--review-daily-usd` cap.
- **`cadmind-judge`**: run the judge on a review bundle in any mode and either version (development and our
  pipeline; `claude-cli:<model>` or an API model). `judge_tools` (the instruments and their stdio MCP server) and
  `judge_workspace` (load a delivered project into a workspace for the tools judge).
- **The client's photos** in our validator's bundle are read and checked (`photos.sha256`) and go to the judge's
  blind turn 1; they are not part of the image set.
- **The calibration archive** (`<state-dir>/archive/jobs/<job>.jsonl`, one append-only file per job, 0600): what
  the store's 7-day window used to forget. Every vote as it is recorded, with every model call made for it -- the
  raw reply text, what it parsed to or why it was unavailable, the model that answered, tokens, dollars and latency
  -- the judge (rubric version and digest, model, samples, mode) and the bundle read (its sha256, image set and
  manifest); the calls that made no vote (too few valid samples, a provider error); and every job's row with all its
  attempts (timeline, rejections, gate errors, retries) as the platform takes its result and again as the store
  drops it. A vote recorded before the archive existed is archived as it ages out. Never served or published.
- **Versions in a job's result** (`versions`: this einstein, the job's deadline and the gate run's budget, and the
  judge its delivery is voted under), so the platform stamps each delivery with what it was judged under. The
  stored vote row carries its cost (`cost`).
- **The operator's calibration tag**: a claim's `calibration` object (a canary's set and id, a generator) is kept on
  the job and in its archive, and never reaches the miner's `work`.
- **`POST /v1/signals {kind: forget}`**: the platform says the client deleted the job; its archive is removed and
  nothing more of it is archived (also after a restart).

### Changed
- **The vote is the judge's** (`cadmind-einstein-vote/2`): `review.py` and `votes.py` call `judge("rate")` with
  1.2b. A vote's samples are the judge's, it names the judge digest and version (`judge`, `judge_version`, where v1
  named `rubric`), its value is `V_MAP[lower median]` with `V_MAP = {0: 0, 1: 0.3, 2: 0.65, 3: 1.0}`, and a spread
  of 2 or more is noisy. `u` is unchanged and votes stay counted. The public form carries each sample's score,
  cap flag, ruling and nudge counts; the miner's own view adds the derived lines, rulings and kept nudges.
  `cadmind-recompute` checks v2 values as `V_MAP` of the lower median, and v1 votes still in the window as before.
  A vote costs about three to four times rubric 3's: re-set `--review-daily-usd`.
- **The calibration archive** records the judge's version, digest and mode, the samples asked, every conversation
  (both turns' raw answers, requests, tokens, dollars and their kind, latency), the grouping calls, the instrument
  calls and their Watt compute.
- **A validator refuses to start with `--judge 1.2b-tools`**, with why.
- `cadmind-review` and `cadmind-calibrate` judge with 1.2b.
- **The reference miner reads Watt's guide topics**: the core (brief, primer, scope), then each topic the device
  needs (`watt_guide <topic>`; the primer's Topics table says when).

### Removed
- **Rubric 3** (R1-R5, `FUNCTION_CAP`, `SYSTEM_PROMPT`, `REPLY_SCHEMA`, `parse_sample`, `sample_cap`,
  `capped_value`, `ask_samples`, `rubric_digest`, `user_text`): replaced by the device judge.
- **The `needs-client` rejection** (owner 2026-10-24: "no questions: the miner assumes and records"). On a job
  whose client asked not to be asked questions, a need no option keeps is the builder's own decided decision with
  `misses` (Watt's W0240 accepts it on such a job) and the job is delivered: the gate no longer refuses it, the
  desk takes `not-allowed` and `not-capable` only, and the reference miner's prompt says assume, record and deliver.

## [0.4.8] - 2026-10-23

### Added
- **Manual miner onboarding** (`--key-approval manual`, the new default; `auto` keeps the self-serve key). A
  registered hotkey that the subnet team has not approved gets no agent key: `POST /v1/miners/agent_key` records
  the request (hotkey, coldkey, first and last ask, client address, and an optional `contact` note from the miner)
  and answers `202 {"pending": true, "message", "retry_after_s"}`. Once approved, the same call mints the sealed key
  as before. A refused or revoked hotkey is answered 403 with the team's reason. An approval is bound to the
  hotkey's coldkey. Decisions persist in the validator's state (namespace `approval`); a pending request of a
  hotkey that deregistered is dropped. Starting under manual approval revokes every key that was never approved
  (as a revocation: deleted on the platform, job reassigned unscored) and puts its hotkey back to pending.
  `--pending-message-file` sets what a pending miner reads.
- **Operator routes and CLI.** `GET /v1/operator/key_requests?state=...`, `POST /v1/operator/approve {hotkey}`,
  `POST /v1/operator/refuse {hotkey, reason}`, `POST /v1/operator/revoke {hotkey, reason}`,
  `GET`/`POST /v1/operator/launch_equal {on}` (Bearer), and
  `cadmind-einstein-operator pending|approve|refuse|revoke|launch-equal` over them (the token read from a 0600 file, never
  printed). Revoking deletes the key, drops the admission, ends the miner's job for it without scoring anyone
  (`revoked`, uncounted) and sends it to another miner; a revoked hotkey approved again takes the entry test again.
- **Launch-equal weights**, a manual switch saved in the validator's state: `--launch-equal on|off` sets and saves
  it; without the flag the saved value is kept across restarts (off when none was ever saved), so a regenerated
  unit never switches it silently; the operator's route changes it at runtime, saved the same way. The startup
  log names the value and its source (flag, saved or default). While on, equal weight per admitted,
  registered, reachable hotkey, history ignored, never the owner's, never empty while a miner is admitted; off,
  the usual rules. The report records `launch_equal` and names the rule ("launch: equal among admitted miners");
  `cadmind-recompute` follows the record.
- **A real entry test.** `--admission-effort` (`high` by default) sets the entry test's effort and so its deadline.
  The entry test carries `no_questions` (no client to ask: the builder decides every open question), passes only
  through the client jobs' gate (the validator's own guard run, no unproven must, and the client-answers check: no
  client decision left open), and only when submitted before its deadline. The built-in brief is now a generic
  small device instead of "bring in Watt's worked example"; deployments pass their own with
  `--admission-brief-file`. Still once a day.
- The reference miners wait while their key request is pending and ask again (`--contact` sends a note).

### Changed
- **Only approved key holders take the entry test**, and only admitted miners are weighted under every rule and
  fallback.

### Fixed
- **The "kept" fallback re-sent hotkeys no longer admitted.** When the rules weighted nobody, the last non-empty
  vector was sent again for every hotkey still registered, so a revoked, disabled or deregistered-and-returned
  miner kept its share. It now keeps only the hotkeys still admitted.

## [0.4.7] - 2026-10-23

### Fixed
- **A gate refusal names every must the validator's guard refused.** Besides the survivors' `unguarded` musts, the
  words now name the musts whose per-requirement guard verdict is `unproven` (W0825), `read` or `excused`, which
  `watt-guard 9` refuses with no survivor to name (they used to say only "did not pass"). A guard without
  per-requirement verdicts still names its survivors; the words never decide the refusal.

## [0.4.6] - 2026-10-22

### Added
- **Whether a job may be split into sub-projects.** The platform sends `subprojects` (true or false) with each job;
  the validator keeps it and passes it to the builder on `work` (absent when the platform did not say). With
  `false`, the reference miner's prompt says to deliver one project. The workspace's `brief/job.toml` carries the
  same choice, and Watt refuses any `[subproject.<id>]` when it is false (W0233), so the gate's `watt status`
  refuses such a delivery with no separate check here.

## [0.4.5] - 2026-10-22

### Added
- **The lease says which jobs a builder has accepted.** The validator's lease call now sends
  `{"jobs": [...], "working": [...]}`. `working` lists the jobs whose current attempt a builder has accepted and
  that are being worked or judged. The platform uses it to charge a client's cancel only once a builder has
  accepted. Accepts, submits and rejections now send the lease on the next heartbeat (within about 5 s) instead of
  waiting up to 30 s. A platform that predates the field ignores it (it reads only `jobs`).
- **A delivered round keeps the builder's summary.** The delivered result now carries `summary` (and
  `summary_by: "builder"`) when the miner sent one with its submit. Before, a summary was kept only on undelivered
  rounds.
- **One structured log line per job event.** The format is `job-event {"event": ..., "job": ..., ...}` for these
  events: `assigned`, `accepted`, `rejected` (with its category), `submitted`, `deadline`, `gate-start` (with its
  run id), `gate-attach`, `gate-lost`, `gate-run`, `judge-retry` (with its reason), `verdict`, `attempt-failed`,
  `requeued`, `cancelled`, `ended` and `vote-recorded`. The lines hold ids, kinds and counts only: no key, no brief
  and no message to the client. An attempt keeps its last retry reason (`last_retry`) after it is judged.

### Changed
- **One rule for a part that cannot be delivered.** When the client can be asked, the builder builds everything
  that can be delivered without giving up a need and leaves the missing part as an open client decision with
  `misses`. On a `no_questions` job the same decision ends the job with `REJECT needs-client`, never a delivery;
  every other would-be question is still decided and built as usual. The reference prompt and docs/MINER.md say it
  once, the same way.
- **Sub-projects.** The prompt and docs/MINER.md say when to split a device (too big for one round), what the parent
  delivers (its own parts and each sub-device's interface, not the sub-devices), and what a sub-device's workspace
  holds (`brief/contract.toml`, `brief/parent.md`).
- **The client always reads the builder's summary.** A submission now carries the agent's final text without its
  `DONE` line, so the builder's notes on a delivered project reach the client too. A model provider's usage-limit
  notice is never sent as the builder's words.
- **A `REJECT` message is kept whole**: everything from the line to the end of the output, not its first line only.
- **Resume.** An agent that stops early is resumed up to 3 times (was once). A session that ended with no output or
  answers `No response requested.` is replaced by a new session told that the workspace holds its earlier work.
  Every run's exit code and error output are logged and kept in the job's log.
- **Answers.** The prompt says to keep a decision's `assumption` line when recording the client's answer: an answer
  that picks the assumption keeps the recorded guard. A `watt` answer now says when the client wrote and the builder
  has not read it.

### Fixed
- **A network blip no longer throws away the validator's guard run.** A failed poll of a run is asked again 3
  times (2, 4, then 8 s apart) before it counts. A judgement retried on the validator's side, or judged again after
  a validator restart, now re-attaches to the guard run it already started (`gate_run`, with what is left of its
  budget) instead of starting a second `watt report --mutate` run on the fork. A run the platform no longer knows
  (404) is started again. Before this fix, a blip cost a 6 to 15 minute rerun of the guard.
- **A miner is free for its next job as soon as it submits.** A miner used to stay busy until the validator
  finished judging its job, which could take up to 30 minutes. `max_inflight` still counts the jobs being judged,
  so the validator's load stays bounded. A job's end frees its miner of that job only, so a miner that has taken
  another job keeps it. A cancel of a submitted job no longer tells the miner to stop or cools it off. After a
  restart, a submitted job being judged holds no miner. A job judged at its deadline (never submitted) still
  holds its miner until the verdict.
- **A job every miner has tried no longer stays claimed forever.** Once every admitted miner that could take a
  job has tried it (for example one rejection and one missed accept), the job ends uncharged, instead of being
  re-queued and leased indefinitely. It ends declined, with the miners' messages, if every attempt was a
  rejection, and "didn't finish" otherwise. The second-opinion wait still applies, and a job never ends while a
  miner (busy, waiting or held off) could still take it.
- **A job re-claimed after a lapsed lease is worked again.** When the lease lapses, the platform re-queues the job
  and the validator stops its own copy. A later claim of that job now reopens the copy as a fresh round: its open
  attempt is kept as `lost` (uncounted, and its miner may take the job again), and its held failures are kept.
  Before this fix, the claim found the cancelled copy and only answered its status, so the job went round the
  platform's queue for up to 7 days.
- **The overturned-rejection rule.** A `needs-client` rejection is never overturned: on a no-questions job,
  missing a need is the client's decision alone, so that rejection is honest even when another builder later
  delivered the job. A delivery by a hotkey of the same coldkey as the rejecting one overturns nothing either. The
  rest of the abuse policy is unchanged (needs-client rejections still count in the rejection rate, and a
  rejection another miner overturns still scores 0).
- **A lost submit or reject is sent again** with a backoff on no answer, 429 or 5xx; 200, 404, 410 and other 4xx
  answers are final.
- **A revoked key or a dropped admission recovers.** While idle, the miner checks every hour that the platform still
  takes its agent key and asks for admission again; a revoked key is minted again. Before, both happened only at
  startup.
- **The local fetch reaches dual-stack hosts when IPv6 has no route.** `fetch_to_workspace` tries each checked
  address in turn, so a host whose IPv6 address comes first is reached over IPv4 instead of failing with
  `Network is unreachable`.
- **The local fetch percent-encodes the url's path and query**, so a non-ASCII or spaced path no longer fails with
  `UnicodeEncodeError` or `InvalidURL`. A redirected fetch says so plainly, so the builder checks that the stored page
  is the product it meant.


## [0.4.4] - 2026-10-22

### Added
- **A third rejection category, `needs-client`.** On a job whose client asked not to be asked questions
  (`no_questions`), a decision whose option leaves a need of the brief unmet (Watt's W0240, a decision with
  `misses`) is the client's alone. The reference miner does not decide it and does not deliver: it ends with
  `REJECT needs-client: <the decision's question>; options: ...`. The desk accepts the category (the job goes to a
  miner of another coldkey, as for the other two; when every attempt rejects, it ends declined, not charged, with the
  question shown), and the validator's answers check refuses a no-questions delivery that carries such a decision.

### Changed
- **Deliver what you can.** The reference miner's prompt and docs/MINER.md: when part of a request cannot be
  delivered, build the rest and make that part an open client decision whose option leaves it out
  (`misses = ["<need>"]`); reject only when nothing in the request can be delivered. A client's "you may quit if it
  is impossible" does not change that.
- **Fit-around data.** When a maker publishes no drawing of a product the device fits, keep looking (to-scale
  community drawings, case and accessory makers' templates, published CAD measurements) and cite one with its
  tolerance; if none exists, ask the client for measurements or a photo beside a ruler. Never refuse for missing
  data alone, and never guess silently.
- **The parts a client selected in the 3D view.** A change file may end with a block from CADmind listing them by
  view key (`build/view/device.json`), name and place. The reference miner's prompt and docs/MINER.md tell the builder
  to read the change file itself (`client_answers` gives only the client's words) and to see the parts with
  `watt inspect` (`show`).

## [0.4.3] - 2026-10-21

### Changed
- **A light judge: rubric 3** (`cadmind-einstein-rubric/3`, docs/VALIDATOR.md). The vote is a base rating, allowed
  to be imprecise, that scores miners when clients do not rate (or rate late); it does not re-do Watt. The review
  bundle's function evidence (`facts.function`) is context for the model, never read mechanically: rubric 2's caps
  and bounds from `summary.fails` and `summary.unverified` are removed. The one cap left is the reviewer's own R2 of 0
  ("it would not work as made"), which caps the sample at 0.35. The weights are unchanged; the rubric digest is now
  `sha256:48c0f1c63e43d5a883386c1a27b8e164c061b8f5c8bba6faf1cd7662f3bb086f`.
- **The vote counts at most `u = 0.4`** (was 0.6 with our vote alone, up to 0.9 with three third-party coldkeys):
  0.4 with our vote, alone or with third parties, so a client's stars clearly dominate it.
- **Votes are counted** (`--votes counted`, now the default; `live` is accepted as its old name). `--votes shadow`
  (recorded and published, `u = 0`) is still an option. At start the validator re-applies every recorded vote under
  its mode, so a switch between shadow and counted takes effect at once.
- **The calibration bar** (`cadmind_einstein.calibration.go_no_go`): every good item (`expect_min`, or a hand score of
  at least 0.5) must score above every bad item (`expect_max`, a lower hand score, or a swapped brief), and every bad
  item under 0.6. It replaces "within 0.15 of the hand score"; each item's distance from its hand score is still
  reported, and the result carries the `separation` (lowest good, highest bad, margin).

## [0.4.2] - 2026-10-21

### Changed
- **Rubric 2** (`cadmind-einstein-rubric/2`, docs/VALIDATOR.md): a device that does not work scores at most 0.35,
  whatever it looks like. The cap is applied mechanically after the reply is parsed, from the review bundle's
  **function evidence** (`facts.function`, fact sheet format 2: Watt's own verdicts -- each must and how it is
  checked, the controls and the client's products with their findings, the version re-checked under today's Watt):
  a reason in `summary.fails` (a must failing, a control a must relies on undeclared, unwired, worked by no operator or without
  travel, the client's product unreachable or not fitting) sets R2 to 0 and caps the value; a reason in
  `summary.unverified` bounds R2 at 2; the reviewer's own R2 of 0 caps the value too. Each sample records
  `raw_value`, `cap` and `value`; the public vote publishes each sample's cap (`caps`) and `cadmind-recompute`
  checks values against them. The cap rules are part of the rubric digest, which changes.
- R2's question says Watt judges the client's products as modelled: no finding is not proof of a fit with the real
  product.

## [0.4.1] - 2026-10-21

### Fixed
- **The reviewer's reply schema** showed each criterion's score as a quoted placeholder (`"<0-4>"`); a judge copied
  the type and answered `"score": "3"`, which the strict reply check refuses, so every sample was unavailable. The
  schema now shows a JSON number and the prompt says so (3, never "3"). The strict checks are unchanged; the rubric
  digest changes with the prompt.
- **`cadmind-calibrate`** stopped the whole run when the platform refused one item's bundle (404 for a version whose
  project its client deleted). That item now gets no vote (a no-go, as before) and the run goes on. An item may name
  a kept bundle file (`"bundle"`, relative to the set's file) that is reviewed instead of asking the platform. The
  reasons of unavailable samples are logged.

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
  go/no-go a model must pass before votes count; `cadmind-calibrate` runs a model over a calibration set from the
  platform's bundles, under a spend cap, and prints the go/no-go.
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
