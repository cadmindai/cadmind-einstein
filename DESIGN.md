# Einstein on Bittensor: design

This document explains how CADmind's **einstein** engine works on the Bittensor side: what the miners do, what the
validator does, how a job is scored and how weights are set. It is written for readers outside CADmind. The code in
this repository is the validator and two reference miners; the CAD toolchain (**Watt**) and the CADmind platform
are hosted services that miners and the validator reach over HTTP.

Section numbers are referenced from the code's docstrings.

## Background

CADmind turns a client's description of a physical device (a *brief*) into a verified CAD project. **Watt** is
CADmind's engineering toolchain: a project is a directory of `spec/` (requirements, sources), `src/` (the parts)
and `tests/`; `watt check`, `build` and `test` verify it, and `watt test --mutate` runs a mutation guard that proves
the tests actually constrain the design. When every check passes and the guard is recorded for the project's
current digest, `watt status` reports the project as **deliverable**.

Watt runs only on CADmind's hosted *agent bench*. An agent drives it through the platform's **agent API**: an MCP
server (`/mcp`) or the same tools over REST, authenticated with an *agent key*. Every call is metered (compute
time), bounded by a credit allowance and scrubbed so that Watt's own code never leaves the bench.

Einstein uses the subnet so that **miners supply the agentic inference**: each miner runs its own agent (any model,
any harness) against the hosted Watt tools. The quality of a miner's harness (how it plans, reads check output and
recovers) is what miners compete on.

## Principles

- **One miner per client job.** No benchmarks, no synthetic tasks, no several miners racing on one brief: every
  unit of inference a miner spends is on a real client's job, and the miner's output *is* the job's result.
- **Watt is the gate; clients grade.** Watt decides whether an output is usable (deliverable). A delivered job's
  points come from its client's latest reaction alone: 0.5 untouched, 1 downloaded or rated 3-5 stars, 0 rated 1-2
  stars or refunded for its quality (section 5). The validators' **quality votes** -- model evaluations of the
  delivered device by a fixed, published judge (section 13) -- are recorded and published for calibration; no model
  decides a delivery, a score, a weight or a route.
- **Never throw away a builder's work.** An attempt a miner worked and did not finish ends the round "not
  finished": its project is kept, and the client may pay for a round that continues it (on any miner, free to start
  over from the base). Only an attempt nobody worked (not accepted, a copy, a rejection) goes to another miner from
  scratch.
- **A failed attempt costs only its 0.** A miner's weight is a sum of points, so a job nobody could do takes
  nothing away from anyone; it just adds nothing.
- **All emission goes to miners.** Weights are never burned and the subnet owner's hotkey is never weighted. There
  is no team miner.
- **Auditable.** Every hour the validator publishes a report from which anyone can recompute every score and the
  weight vector, and commits the report's hash on chain.

## 1. The shape

```
client job (public, free-plan, or opted in)
   └─> validator: claims it from the platform's queue, routes it to ONE miner
         └─> platform: a job workspace granted to that miner's agent key
miner: its own agent, any model, any harness
   └─ MCP / REST with its own agent key, naming its hotkey ──> platform (keys, quotas, meter) ──> Watt agent bench
validator: freeze on submit, fork, read Watt's verdict (the gate), copy check, score,
           deliver (or reassign), record client signals, hourly weights and report
```

## 2. Identity: the miner's own agent key, naming its hotkey

- A miner has an account of its own on the developer platform. It **links its hotkey** to that account by signing
  a challenge with it, and creates there an **agent key that names the hotkey**. The key and its secret are the
  miner's: its operator gives the secret to the miner program in a file, and the validator never sees it. One key
  per hotkey is used: the newest live agent key that names it.
- The miner asks the validator to take that key with `POST /v1/miners/agent_key`, signed by its hotkey (`btauth/1`,
  Bittensor's HTTP signing). The validator checks the hotkey is registered on the metagraph, asks the platform
  which key names the hotkey, and answers its id, `{key_id}`. The miner checks that this is the key it runs on (the
  platform names the calling key's id) and stops when it is not. A hotkey the platform has no key for (linked to
  no account, or named by no live key) is answered `409 miner_not_linked` with the platform's own instruction; the
  miner logs it and asks again later. A miner is *keyed* at the validator once this call answered: only then may
  it take the entry test, practise or get a job.
- **Compute is paid in credits, and the allowance comes with the job.** Every Watt run costs credits. A miner has
  none of its own (they cannot be bought yet): each job workspace the validator grants carries an allowance of
  credits from the platform, and a job workspace is the only place a miner's key runs compute. The allowance is
  the platform's business; the validator's grant call says nothing about it.
- Only the validator's own agent keys reach the platform's *operator* routes, one key per role so that no single
  secret does everything: the keys key asks which agent key a hotkey has and closes a hotkey's open job
  workspaces; the operator key grants, freezes and forks job workspaces; the queue key claims and completes jobs
  (completing a delivered job charges the client). Past its ceilings the platform freezes that role's key until it
  is enabled again; the validator stops that role, alerts, re-checks every 5 minutes, and keeps results to return
  in its store. A deregistered, disabled or revoked hotkey is no longer keyed at the validator and its open job
  workspaces are frozen on the platform (queued and retried until the platform takes it); its agent key stays its
  own, with no workspace to work in.
- **Manual approval** (`--key-approval manual`, the default since 0.4.8). Miners are let in by hand. A registered
  hotkey that is not approved is not keyed: its request is recorded -- hotkey, coldkey, first and last ask, client
  address, the miner's optional `contact` note -- and answered `202 {"pending": true, ...}`. The operator lists,
  approves, refuses (with a reason the miner reads) or revokes over Bearer routes (`/v1/operator/...`, the
  `cadmind-einstein-operator` CLI). Once approved, the same call looks the key up. An approval is bound to
  the hotkey's coldkey: under another coldkey the hotkey asks again. **Revoking** stops taking the key, freezes the
  hotkey's open job workspaces (queued and retried, as for a deregistration), drops the admission, ends the job the miner holds without scoring anyone (`revoked`,
  uncounted) and sends it to another miner; an entry test it holds ends "not admitted" without counting as a
  failure, and one judged after the revocation admits nobody. Decisions persist in the validator's state
  (namespace `approval`, one row per hotkey); a pending request of a hotkey that deregistered is dropped. When a
  validator starts under manual approval, every hotkey keyed or admitted there that was never approved (keyed
  under the self-serve rule) is revoked the same way and goes back to pending: it asks, is approved and passes
  the entry test like anyone else. What a pending miner reads is the operator's (`--pending-message-file`).
  `--key-approval auto` keeps the self-serve rule: any registered hotkey's key is taken.

## 3. Jobs: one miner each, client briefs only

- **No work without a client brief.** Scores come only from real jobs.
- **The validator pulls.** All jobs wait in the platform's single queue. Whenever the validator has a free admitted
  miner it claims one einstein job, and the router picks the miner (section 6). The platform makes a job workspace
  from the brief (or, for a later round of a client's project, from the delivered project), granted to that miner's
  key. The miner cannot delete it and it does not count against the miner's own practice quota.
- **Accept, work, submit -- or reject.** The miner sees the job on `work`, must `accept` within 2 minutes, and
  `submit {job, workspace, digest, summary?}` before the job's deadline: one deadline for every job, 240 minutes from the accept (there are no
  effort levels; the builder may submit sooner). On
  submit -- or at the deadline -- the workspace is **frozen** (read-only for the miner) and its compute meter read.
  `summary`, optional, is the builder's own words on what still blocks delivery when it hands over a project that
  is not deliverable (the client reads it; whitespace collapsed, at most 2000 characters). Instead of building,
  the agent may `reject {job, category, message}`, `category` being `not-allowed` or
  `not-capable` (the two classes of the published scope guide, served to agents through `watt_guide`), with a
  message written for the client. A job whose client asked not to be asked questions is never declined for a
  need: the builder assumes, records each assumption as its own decision (a need no option keeps included, with
  `misses`, which Watt's W0240 accepts on such a job) and delivers.
- **Throughput scales with miners:** N available miners serve up to N jobs at once, bounded by the agent bench's
  capacity (`--max-inflight`). A miner holds one job at a time.

## 4. The gate, reassignment and how a job ends

- **The gate is Watt's own verdict, on the validator's own run.** The validator forks the frozen workspace into
  one only it can write and runs `watt status` there: the project must be **deliverable** (the digest covers the
  tests' code and the verification record is written only by Watt). The hosted Watt runs the whole mutation guard,
  every mutant in the digest's order, so there is no draw to re-roll and no honest design fails on an unlucky one. A
  host that samples instead (`WATT_GUARD_FULL` off) seeds its draw from a host secret and the digest. Either way the
  validator then runs the guard on its fork (the operator's lane; the fork's guard outcomes and build are reused)
  and reads `watt status` again: the output passes only when that run passed and the record is that run's
  (`guard_draw.seed`). A miner whose own record is not deliverable fails at the first `status`, at no
  guard cost.
- **The client's answers are part of the gate.** While a job is built, its client sees the builder's open client
  decisions (`by = "client"`, `status = "open"`) as questions in their chat and may answer them, or ask for a
  change; the platform writes each into the job workspace's `brief/` (the client's words, which the builder cannot
  write) as the dated file Watt's own designer gets (`brief/answer-<date>-<decision>-<HHMMSS>.md`,
  `brief/change-<date>-<HHMMSS>.md`), and the builder reads them with the platform's `client_answers` tool. On its
  own export of the fork the validator then checks (`cadmind_einstein/answers.py`) that every answer that arrived
  before the hand-over (the submit, or the deadline) is recorded in its decision: no longer open, an `answer.from`
  quoting the file, the option the client picked as its choice. An answer that arrived after the hand-over never
  counts against the miner (it starts the client's next round). A client may change their mind: when a change
  request arrived after an answer and replaces it, the answer is recorded by the decision that supersedes the
  answered one, settled, with `found_by = { ref = "brief/change-<date>-<HHMMSS>.md", reading = "<a quote of that
  file>" }` (the quote is not looked for in the lines the platform quotes there from an earlier builder's summary).
  The reference miner runs this same check on its own export before it submits, and runs its builder again with the
  reason (twice at most, while 5 minutes are left before the submit's reserve), so a decision recorded wrongly costs
  minutes instead of the round. A job whose client asked not to be asked questions
  (`no_questions`; every job opened through the developer API) must leave no client decision open: each would-be
  question is the builder's own decision (`by = "agent"`, `status = "decided"`), which the client sees as an
  assumption. Either failure is a gate failure, with the reason in the miner's receipt.
- **A style round** (cadmind-watt ADR-020) reshapes a delivered device's printed parts and nothing else. Its claim
  carries the settled contract the round began from (`style.baseline`: the contract digest and where every bought and
  world item sits, as the platform's `watt style begin` answered it). On its own export of the fork, after its own
  `watt report`, the validator checks (`cadmind_einstein/style.py`) that `build/report.json`'s `contract` is the same
  digest with the same items, each within 0.001 mm and 0.001 degree; otherwise the attempt fails the gate (stage
  `style`), the differences named in the miner's receipt. A client decision the contract settles stays open through
  the round (deciding it would change the contract), so the no-questions check exempts the baseline's
  `settled_decisions`, or, when the baseline does not name them, the client decisions open in the version reshaped.
- **The gate checks the tree as delivered.** After the miner's `watt status` says deliverable and before its own
  guard run, the validator runs `watt check --as-delivered` on its fork (agent API: `check` with `{"as_delivered":
  true}`): Watt's check of the tree a delivery carries, without `.watt/` and `build/`, which is what a project that
  imports it builds. Error diagnostics fail the gate at stage `as-delivered` (`cause: {stage, codes}`); a run that
  says neither pass nor errors is the validator's side, retried.
- **A host block is nobody's failure.** Any run answer of the gate carrying `host_refused` (`{code: "host_refused",
  why}`: the platform refused the run's result for a reason of its own) ends the round **blocked by the platform**:
  the result is `{label: "host_blocked", kind: "host_blocked", charge: false, why, host_refused: {code, why},
  workspace (the kept fork), digest, hotkey, attempts, summary?}`, an ALERT is logged, and the attempt is uncounted (no
  ledger entry, not toward the cap, not a gate failure or "not finished", the miner's wait clock put back where its
  submit found it). A miner may report one with its submit (`host_refused`); its record is then not deliverable (a
  refused guard records nothing), so the validator runs its own guard run anyway to reproduce it: refused, the round is
  blocked; not refused, the round is judged on that run (delivered when it passes, else as the record stands).
- **An attempt the miner worked that does not pass** -- a gate failure, or late and failing -- **ends the round
  "not finished"** (section "Continuing an unfinished round"): its project is kept, not charged, and the client may
  pay for a round that continues it.
- **Any other attempt that does not pass** -- a rejection, a timeout before accepting (not accepted within 2
  minutes), a copy -- sends the job to **another miner, from scratch**: the brief (or the seed) only, never an
  earlier miner's rejection message. Each miner decides on its own.
- **How a job ends** (judged on the whole job):

  | What happened | The job | Charged | Scores |
  |---|---|---|---|
  | an attempt passes the gate | **delivered** | yes, once | the passing miner scores (section 5); every earlier attempt scored 0 as it ended |
  | a worked attempt does not pass (gate failure, or late and failing) | **not finished**: its project kept, the builder's summary for the client | no | that attempt scores 0 |
  | a Watt run of the gate answered `host_refused` (the platform refused its result, not the design) | **blocked by the platform** (`host_blocked`): its project kept, the platform told and alerting its operator | no | nobody (the attempt is uncounted) |
  | a later round passes at the digest of the version it started from, and the client only answered questions, each by picking an offered option | **no change**: its project, the answers recorded, handed back | no | that attempt scores nothing |
  | two rejections, from two coldkeys | **declined**: the client reads both messages; the brief is flagged for review | no | each rejection void (counts for nothing) |
  | four attempts without either of the above (timeouts before accepting, copies) | **didn't finish** | no | each attempt scored 0 |
  | every admitted miner that could take it has tried it (the second-opinion wait still applies) | **declined** if every attempt was a rejection, else **didn't finish** | no | as above |

  **A round that builds nothing.** A later round is compared with where it began: the digest stated by the report of
  its seed (on a continuation, of its delivered base, since its seed was never delivered). At the same digest the copy
  check does not run. What decides is what the client sent, read from the `brief/` files the platform wrote and the
  builder cannot write, never the brief's text: only new `answer-*` files, each a chosen option with no words of the
  client's, make it **no change**. That is sound because Watt's digest stays only for an answer that picks the
  assumption the device was built on. Every other such round (a change request, a photo, words of the client's, a style
  round) asked for something a builder can ignore, so it fails the gate at stage `unchanged` and ends **not finished**.
  It scores as any gate failure. It earns nothing either way: no score for submitting nothing.

  Earlier releases also ended a job **out of reach** (two substantive failures from two coldkeys); a worked failure
  now ends the round at once, so that ending is no longer reached.

- **A second opinion is another owner's.** A job one miner rejected goes next to a miner of another
  coldkey; only after `--second-opinion-wait-s` (30 minutes) may any other miner take it, and that miner's opinion
  is then not a second one. A job ends declined or out of reach only on opinions from `--second-opinion-coldkeys`
  (2) distinct coldkeys, so hotkeys of one owner cannot end a hard job between them.

- Every attempt that does not deliver -- a timeout, a copy, a rejection, a gate failure -- scores 0 at once (earlier releases held a rejection or a gate failure until another miner passed the job). A rejection
  also sends its miner to the back of the router's line (section 6), and one another coldkey's miner overturns by
  delivering the job counts toward the abuse limits (section 11).
- **A confirmed decline is neutral** (0.6.9). A job that ends declined -- every substantive attempt a rejection,
  from `--second-opinion-coldkeys` distinct coldkeys -- was rightly handed back: its rejections are made **void**
  (`JobOutcome.void`), published with their facts and left out of every weight, average, router record and
  rejection count. Without it the rule paid 0.5 for building anything on an unreadable brief and 0 for declining
  it. Only confirmed declines are neutral: a rejection whose job is still open is a 0 and a rejection until the job
  ends; one another miner delivers stays a 0 (overturned when that miner is of another coldkey); one owner's
  hotkeys running the fleet out confirm nothing. The operator may void a job's rejections by hand
  (`POST /v1/operator/void {job, reason}`), saved with the outcome so restarts and recompute agree.
- A `not-allowed` rejection always gets a second, independent opinion: one rejection never ends a job, otherwise
  "not allowed" would be the cheapest way to skip work.
- A miner that deregisters mid-job, or is disabled for abuse, loses the job without a score, and that attempt does
  not count toward the four-attempt cap; nothing of its workspace is kept.
- A client's **re-run** is a new job, routed away from the miners that had the first one; on the first job it is
  recorded as a signal, not scored. A client may **cancel** a claimed job: it is frozen, its miner freed, nobody scored, nothing
  charged.

## 5. The score: per job, graded by the client

A client job delivered by a builder that passes the validator's Watt gate scores:

- **0.5** while the client has done nothing;
- **1** when the client downloaded it (in the app or by API: the platform's `download` signal) or rated it 3, 4
  or 5 stars;
- **0** when the client rated it 1 or 2 stars, or a refund was granted for its quality (the platform's quality-refund
  signal).

**A verdict outranks a download.** The client's latest rating or quality refund decides whenever there is one: a
1-star rating then a later 5-star one is 1; a 1-star rating then a download is still 0. A download counts
only while the client has given no verdict. **Only the job's own client scores.** A reader's download of a public
project is recorded as such (`by: "visitor"`) and never scored: a builder could otherwise lift its own delivery from a
second account. An outcome scores as a delivery only when it is one (kind `pass`). Free and paid count the same (a `trust` the platform still sends
is ignored). A change request, a re-run or a refund for another reason is recorded and shown in the miner's stats,
never scored.

Not delivered, failed the gate, timed out, handed back (rejected), a copy: **0**, at once.

- **No model scores.** The validators' votes (section 13) are recorded, published, shown to the miner and kept in the
  calibration archive and the practice comparison; nothing in the score, the weights or the router reads them.
- **Late signals** change the job's points when they arrive, while it is in the window; the next hourly vector reads
  the current points.
- Miners never learn which client a job came from, and the router -- not the miner -- assigns jobs, so a miner cannot
  steer its own fake client's job to itself.
- Every hourly report names the einstein version that computed it and publishes the rule's numbers (`constants`: the
  window and the three point values); the code is that version's `scoring.py`.

### Weights

- A job counts for **7 days from its end** (its delivery), flat: no decay. A miner's **weight** is the sum of its job
  points over the last 7 days; its **average** (points per job in the window) is shown on the stats pages; the router
  reads its **record**, that average with a miner of fewer than 10 jobs padded with unrated ones (section 6).
- A remix (a project started from a copy of a public project) scores its own builder like any job. The platform's
  claim still names the original's miners (`remix_credit`); the validator checks and records it with the job, and
  never scores it.
- The vector is proportional to the weight over registered, admitted miners seen in the last 24 hours, **per hotkey**
  (coldkeys play no part in this rule), and is set **every hour**. An hour with no work still pays miners from their
  last 7 days: **weight is never burned**.
- **Only admitted hotkeys are weighted**, by every rule and every fallback below: a miner whose admission was
  dropped (revoked, disabled, deregistered) is out from the next vector on, whatever its points
  and whatever the last vector held.
- **Launch** (a manual switch saved in the state: `--launch-equal on|off` sets and saves it, the operator's route
  `/v1/operator/launch_equal` changes it at runtime; without the flag a restart keeps the saved value, off when
  none): while on, equal weight **per
  hotkey** among the admitted, registered, reachable miners (each one approved by hand and through the entry
  test), history and the last vector ignored; with none reachable, equal among the admitted ones (never empty).
  Off, the rules below. The report records `launch_equal` and names the rule ("launch: equal among admitted
  miners"); `cadmind-recompute` follows the record (a report without it was made with the switch off).
- **Genesis** (no admitted miner has points in the window), `--genesis`:
  - `delivered` (default): equal weight **per coldkey** among admitted, registered, reachable miners that have
    delivered at least one scored client job (ever), each coldkey's share split among its hotkeys. Admission is a
    cheap, unscored entry test, so it earns nothing on its own while a miner that has delivered is reachable.
  - `coldkey`: equal per coldkey among admitted, registered, reachable miners.
  - `hotkey`: equal per hotkey among them (an earlier rule; it pays in proportion to hotkeys registered).

  Each report publishes the rule, the admitted miners' coldkeys and the hotkeys with a delivered job, and
  `cadmind-recompute` follows them (a report without a `genesis` field was made by the `hotkey` rule).
- **Never an empty vector** (section 10). When the rules above weight nobody, the last non-empty vector is sent
  again (its hotkeys still registered and admitted); before any vector was ever set, equal shares per coldkey among the
  admitted, reachable miners. A vector is empty, and nothing is sent, only when no miner is admitted at all. Each
  report publishes the vector it kept (`last_vector`) and its own (`vector_by_hotkey`).
- **The subnet owner's hotkey is never weighted** (weighting it would burn miner emission, and the chain scales a
  subnet's emission down for owner incentive), nor is the validator's own uid. These are dropped again at the last
  step before the chain call, whatever the scorer produced.
- **Recompute at startup.** A stored outcome keeps its facts and signals, never its points: at every start the
  validator scores the window's outcomes by the rule it runs, so after an upgrade no miner holds a mix of an older
  rule's points and this one's.

### Continuing an unfinished round

A round that runs out of time (or hands over a project that does not pass) used to go to another miner from
scratch, throwing the first builder's work away; after two or four tries the job ended with nothing kept. Now:

- **The round ends after one worked attempt that does not deliver** (a gate failure, or late and failing). Its
  project -- the validator's fork of the frozen workspace, kept as a delivery's is, the platform's keep and retention
  -- is named in the result: `{label: "not finished", charge: false, workspace, digest, stage, error, cause?,
  summary, summary_by, hotkey, attempts, messages, why}`. `summary` is the builder's own words (`submit`'s `summary`,
  `summary_by: "builder"`) or, when it gave none, a sentence from the gate's stage and error (`"gate"`). Nothing is
  charged. Every gate failure of a worked attempt names its kept `workspace`, the as-delivered check's too: the
  platform reads its `watt status` for the change requests an assembly round wrote on a part that blocked it, as it
  does for a delivery, so the client can act on them although the round did not deliver.
- **The client chooses.** It may pay for a round that continues it, at a normal round's price; which miner gets that
  round is the router's choice, as for any job. Its claim carries `seed` (the unfinished project) and `continuation:
  {base}` -- the operator's workspace of the project's last delivered version, or `""` when there is none.
- **The continuing builder chooses too.** Its workspace starts as the unfinished project. The grant
  (`POST /api/agent/subnet/workspaces {..., seed, continuation: true, base?}`) also gives it, when there is a
  delivered version, a read-only fork of that version (`reference`); `work` says `continuation: true` and `base:
  {kind: "delivered", workspace: <reference>}` or `{kind: "brief"}`. To start over it rewrites `spec/`, `src/` and
  `tests/` from the reference (read with `list_files`/`read_file`), or clears them and builds from `brief/`. No new
  tool: any miner that can read and write files can do it.
- **Rejections are unchanged**: two coldkeys must reject to decline a job.
- **No farming.** Only a delivery is paid. A "not finished" round's attempt scores 0 at once, so ending rounds
  unfinished adds nothing to a miner's weight. The abuse counters see
  every such attempt as before: gate failures (`failed`, the per-coldkey watch), timeouts (the ledger), the miner's
  receipt (`failures`); the hourly `watch.endings_by_coldkey` adds a `not_finished` count.
- **Copies.** A continuation's output shares most of its features with its seed and, when its builder started over,
  with the delivered base: the copy check subtracts the seed's features and the base's (section 9), so neither
  makes the continuing builder a copier of the earlier builders.

## 6. Admission and routing

- **Admission.** Before any client job, an approved key holder passes an entry test: `POST /v1/miners/admission`
  creates a job assigned to it alone, at most once a day. It is a real small device built from a client-style
  brief (`--admission-brief-file`; a generic built-in brief otherwise), with the one job deadline
  (240 minutes; `--admission-effort` is accepted and ignored). There is no client, so the job carries `no_questions`: the builder decides every
  open question. It passes only through the client jobs' gate -- deliverable on the validator's own guard run, no
  unproven must, no client decision left open -- and only when submitted before its deadline. Passing admits the
  miner; it is never scored. Only admitted miners get client jobs and weight.
- **Practice.** An approved key holder, admitted or not, may ask for a practice job (`POST /v1/practice`): the
  platform picks a delivered public project's brief, exactly as its gallery page shows it, which this miner has not
  practised on, and the validator runs it as a job of kind `practice` -- assigned to that miner alone, `no_questions`,
  on the platform's practice lane (spare capacity only) and its own workspace quota, gated as any job. A deliverable
  result is voted on with the validators' judge, and so is the delivered version under today's judge (that vote
  cached per delivered job and judge digest); a result that fails the gate is reported with the gate's reason and no
  vote. The record goes to the platform for the operator and to the miner (`GET /v1/practice`, by `setup` label).
  It is never in the ledger, points, weights or routing, never sent to another miner, never handed out while a
  client job the miner could take waits, and at most 3 a UTC day per miner.
- **Available** = admitted, holds a live key, polled `work` in the last 60 s, holds no job, not blocked, registered.
- **Router.** A client job goes to the miner its claim prefers (`prefer`: the builder of the version a later round
  continues) if it is available; else to the **best record** among the available miners -- points per job over the last 7 days, a miner with fewer
  than 10 jobs read as if the missing ones were unrated (0.5) -- and, **every fifth client assignment**, to the one
  that has **waited longest** whatever its record, so a newcomer gets real jobs to make a record on. Equal records
  fall to the longest wait; equal waits are drawn at random. The wait is the owner's: a coldkey's clock is the latest
  of its hotkeys' (`free_ms`: when a miner's last client assignment ended -- its submit, its hand-back or its
  timeout; 0 before its first), so more hotkeys buy no more turns. A miner that hands a job back (rejects)
  restarts its clock at the hand-back. A client's cancel, a lapsed lease and a host block leave the clock as it was.
  Votes play no part. Entry tests and practice jobs do not move the clock.
- **No miner free:** nothing is claimed and jobs wait in the platform's queue, which shows the client a position
  and an ETA (from the validator's `GET /v1/capacity`). A job is never refused for lack of miners.
- **Lease.** The validator renews a lease on every job it holds every 30 s (and soon after a miner accepts, submits
  or rejects), naming the jobs a builder has accepted (`working`: a client's cancel is charged from then on); a job whose lease lapses goes back to
  the platform's queue, and one the platform reports lost (cancelled, re-queued) is dropped. A lost job that the
  validator claims again is worked again: a new attempt, with the earlier ones kept and the lost one uncounted.
  After a restart the validator reconciles with the platform's list of jobs it holds.
- **Reassignment stays inside the validator.** A failed attempt keeps the claim; the held job takes the next free
  miner before anything new is claimed. This keeps a job's attempts together and keeps the next miner away from the
  ones that already failed it.

## 7. Hourly reports and the auditor

Every hour the validator writes a report: the window's jobs with their facts (hotkey, accepted, on time, gate, wall
time, copy, how the attempt ended) and client signals (kind, stars, quality -- never who the client is), every job's
points and what decided them, each miner's weight and average, the admitted and reachable miners, the rule used
(history or genesis), the constants, the validator's stake share against `kappa`, and the vector.

- Reports are canonical JSON, public at `GET /v1/reports` and `GET /v1/reports/hour-N.json`.
- Each report names the previous report's SHA-256, and the newest hash is committed on chain
  (`Commitments.set_commitment`, one `Sha256` field, signed by the validator's hotkey). Only the newest commitment
  is kept in chain state; the hash chain makes it authenticate the whole published history.
- `cadmind-recompute hour-A.json ... hour-N.json [--network NET --netuid N --validator-hotkey HK]` rebuilds every
  score and the vector from the reports alone, checks the hash chain and canonical bytes, and compares the newest
  hash with the commitment on chain (its `Sha256` field; a `sha256:<hex>` text commitment still reads). Each report
  names the einstein version and spec version that computed it (`einstein`); a report from another version whose
  numbers differ is said to be so (`other_version`, `not_checked`), not called a mismatch.
- Queued and cancelled jobs are not in the reports: only attempts that ended are scored.
- **Votes** (section 13; never scored): each report's `votes` carries the mode (`shadow`), the judge's version and
  digest, every vote on a window job in its public form -- the validator, the model it names, the image set's digest,
  each sample's 0-3 score, its ruling and nudge counts, the hash of the full samples and the validator's signature --
  and each job's
  `V` and vote count. The words of a vote (what the model said it saw, the client's quotes) are the
  client's project: miners read them on their own jobs only (`/v1/miners/stats`). `cadmind-recompute` checks every
  vote's signature over its published hash and that its value is `V_MAP` of the lower median of its published
  scores (a pre-0.5.0 vote: rubric 3's capped mean).

## 8. Durability and capacity

- All validator state (jobs, attempts, miners, admission, telemetry, outcomes and signals, the copy
  window, results not yet taken by the platform, hotkeys whose workspaces are still to freeze, the last hour closed, commitments) lives in
  one SQLite file (WAL, `synchronous=FULL`). A desk mutation and the ledger writes it causes share one transaction.
  A restart -- mid-job or mid-hour -- loses nothing: a grant cut off mid-call is re-queued, a working attempt keeps
  its workspace and absolute deadline, a judging attempt is judged again (re-attached to the guard run it had
  started), and an hour missed while down is closed.
- Every slow platform call (grant, judge, return a result, close a hotkey's grants) runs in a bounded worker pool, and the
  lease has its own heartbeat thread, so the desk's tick never waits on the platform. A platform outage during
  judging leaves the job judging and retries later, on the same guard run (a failed poll is asked again 3 times
  first): an outage on the validator's side never fails a miner.
- Each job workspace has a credit allowance on the platform; the validator caps jobs in flight at
  the agent bench's capacity.
- **Miners update themselves between jobs** (`cadmind_einstein/auto_update.py`). A reference miner run from a git
  checkout reads the remote's release tags every 30 minutes; a newer release is applied only at the miner's idle
  point -- the top of its job loop, when the validator's `work` answer names no accepted job of its own -- so a job
  is never interrupted and none is accepted while an update is pending. It checks the tag out, reinstalls it,
  verifies in a fresh interpreter that the code imports and carries the tag's version, then re-executes itself on
  the same command line (`os.execv`: same PID under any supervisor). Any failure restores the previous commit and
  its install and the miner runs on. A dirty checkout is never touched. `--no-auto-update` or
  `CADMIND_MINER_AUTO_UPDATE=0` turns it off, with no penalty; `--auto-update follow-checkout` restarts onto a
  checkout's new HEAD instead, with no fetch.

## 9. Against untrusted miners

| Threat | Answer |
|---|---|
| A thin or broken output | It fails the gate (0, at once) or passes thin and the client's signal says so |
| A miner rating its own jobs from a fake client | Miners never learn the client; the router, not the miner, assigns jobs (by record and by wait) |
| Rejecting hard work | A rejection scores 0 and sends its miner to the back of the line; rejection telemetry, per hotkey and summed per coldkey, disables repeat offenders |
| Copying another miner's output | Normalised features compared by containment (below); 0 at once, reassigned |
| Sybils | Each hotkey holds one job and is scored on its own; registration costs; genesis weight goes per coldkey to miners with a delivered job, never for admission alone |
| Abuse of hosted compute | A credit allowance per job workspace and none outside it, one run at a time, automatic disable |
| Collusion between hotkeys | Second opinions come from another coldkey, and only two coldkeys end a job unpaid; endings, rejections, failures and admissions are published per coldkey in each report (`watch`) |
| A device that passes Watt but does not serve its brief | The client's 1-2 stars or a quality refund make it 0; the validators' vote (section 13) records it for calibration |
| Text in a project aimed at the reviewing model | Structured facts only, capped, instruction-like strings withheld, one data block the prompt names data; strict replies; quotes of the client checked verbatim (section 13) |
| A validator voting for its own coldkey's miners | Its votes on them are dropped; others are clamped near the median, capped at a third, weighted by stake and calibrated reliability (section 13) |

### Copies

With one miner per job there is no round to copy within, but a miner can hand in another job's output (a delivered
project it saw, or its sybil's). Each passing output's `spec/ src/ tests/` is reduced to **features** that cosmetic
edits do not change:

- **Python** is parsed; comments, docstrings, `pass` and bare constants are dropped; every identifier (names,
  attributes, arguments, keyword names, functions, classes) becomes one placeholder, every string another, and
  numbers get one spelling (`2` = `2.0`). Each statement (a compound statement by its header) and each call of six
  or more syntax nodes is a feature. Renaming, reformatting, commenting, reordering or inserting statements leaves
  every other feature as it was.
- **TOML** is parsed; each leaf -- its table's kind, its key, its normalised value -- is a feature, whatever the
  table is named.
- **Other text** (and Python that does not parse): each normalised line of eight or more characters.

Before comparing, the validator subtracts what honest builds share: the features of the project the job started
from (its seed, and for a continuation its base too), of a corpus of public example projects (`--copy-corpus`: project folders or export zips, such as
the worked example), and the **common set** -- features present in non-copy outputs of `--copy-common-coldkeys` (3)
distinct coldkeys other than the two compared. Outputs judged copies never feed the common set, and one coldkey's
hotkeys count once, so a copier cannot make its victim's features common.

A round left at the digest it began from is not compared at all (section 4). Otherwise, an output copies a recent
output (the last `--copy-window`, 500) of **another** hotkey when the Watt digests are
identical; when their normalised features are identical (a small project is compared this way, not skipped); or,
when both have at least `--copy-min-features` (8) distinctive features, when the **containment** -- the share of the
smaller one's distinctive features found in the other -- reaches `--copy-containment` (0.7). Containment rather
than Jaccard: a copy padded with unrelated material still contains its victim.

On 35 independent builds of 13 briefs by agents using the same primer, same-brief containment stayed at or below
0.34 (0.16 with the common set); copies disguised by comments, renames, reordering, dead statements, padding or
all of these together scored 1.0; changing 30% of the numbers still left them above 0.74. Every comparison is
published in the hourly report (`watch.copy_pairs`: similarity, containment, sizes, features removed as common,
what decided, the verdict; `watch.copy_policy`: the thresholds), so the thresholds can be re-checked on live
traffic. A copier who rewrites most statements has re-implemented the design; that is out of this check's reach.

## 10. A single, auditable validator

**Null consensus.** Since subtensor runtime 475 (October 2026) a subnet owner can switch the subnet's epoch from
Yuma to **Null** (`set_epoch_consensus`). Under Null exactly one validator holds a permit -- the UID with the
largest stake -- and only it may set weights; an epoch reads that one row, with no consensus and no bonds. Every
staked UID keeps its pro-rata dividends. Two consequences shape this validator:

- **The weight setter is whoever stakes most.** Einstein's validator must keep the largest stake, with a margin;
  third-party validators can only *send* votes (section 13), and their stake earns dividends, not a say in the
  weights.
- **An empty vector sprays emission.** If the elected validator's weights are empty or invalid, the miners'
  emission is spread evenly over every registered UID -- farming hotkeys included. So the vector is never empty
  while a miner is admitted (section 5, Weights).

Under Yuma (a subnet that has not switched, such as testnet today) one weight-setting validator's row is the
consensus only while it holds more than `kappa` of the stake of active validators. The einstein validator computes
that share from every metagraph read, logs it when it changes and publishes it in every report (the `chain`
section; the operator also reads it at `GET /v1/chain`); it never claims a dominance it does not hold. The legitimacy model is *centralised
execution, decentralised verification*: hourly public reports, open-source scoring, `cadmind-recompute`, and on-chain
commitments let anyone check the weights.

Weights are set with `bt.SetWeights`, which takes the plain or the timelocked commit-reveal path according to the
subnet's hyperparameters, inside `weights_rate_limit`. The weights carry the validator's spec version
(`cadmind_einstein.__spec_version__`, derived from the package version) as their `version_key`, so the subnet's
`weights_version` hyperparameter can require a minimum validator release; the validator warns when it is older.
`--dry-run-weights` makes no chain write at all (vector and report hash are logged).

## 11. Abuse, secrecy and the HTTP face

- **Disables.** Every tick the validator disables a miner whose counts since its last strike cross the policy:
  copies (2), Watt suspensions (1: the platform suspends a miner on Watt-side telemetry such as secrecy refusals or
  quota storms), failed entry tests (3), or the rejection pattern (at
  least 5 rejections at over twice the fleet's rate, or 3 overturned rejections -- delivered by a miner of another
  coldkey; the same rule is applied to each
  coldkey's hotkeys together, so spreading rejections over hotkeys does not hide them). A disable freezes the hotkey's open job workspaces, drops
  the admission, sends the open job to another miner unscored and blocks the hotkey for a day, doubling per later
  strike up to 30 days. All thresholds are daemon flags (`--abuse-*`; 0 turns a rule off). Every disable is logged
  and listed in the next hourly report.
- **Secrecy.** Watt runs only on the hosted bench; miners reach it only through the agent API, which scrubs every
  answer. Miners see their brief, the public docs, and their own workspaces -- never Watt's code, other workspaces,
  other clients' projects, or which client a job came from.
- **HTTP face.** The validator refuses before it reads a body: a missing, malformed, duplicated or oversized
  `Content-Length` (and any `Transfer-Encoding`), an unsigned request on a signed route, an unregistered hotkey, a
  Bearer route without the token. Bodies are capped at 4 MiB; every read has a timeout and every request a
  deadline; connections are capped in all and per address; agent-key requests are rate-limited per hotkey (6/h) and
  per address (60/h). In production a TLS reverse proxy stands in front and is named with `--trusted-proxy`.
- **Signed answers.** Every answer on a miner's route carries `X-Einstein-Validator` (the validator's ss58) and
  `X-Einstein-Signature`, the validator hotkey's sr25519 signature over newline-joined UTF-8 lines:
  `cadmind-einstein-response/1`, the HTTP status, the method, the path, the request's `X-Bittensor-Nonce`, the
  miner's hotkey, the validator's hotkey, and the hex SHA-256 of the answer's exact bytes. A miner checks it with
  `cadmind_einstein.service.verify_response` (the reference miners do, and drop an answer that does not verify), so
  an answer forged, altered, replayed from another request or meant for another miner is refused even without
  TLS.

## 12. Privacy

Miners see the brief. Einstein takes public and free-plan projects; private ones only when the client opts in. The
validator refuses a private job that reaches it.

The einstein validator reviews every delivery (section 13); the bundle it reads never names the client and never
leaves its infrastructure and its model provider. Third-party validators receive bundles of public projects only,
without client photos. The hourly report publishes votes as scores and signatures, never their words. A miner's
stats (`/v1/miners/stats`) show its own jobs' signals as kinds and counts: never the client, a flag's text, or
another miner's jobs.

## 13. Quality votes

A device can pass every check it wrote and still not serve its brief. A **vote** is one validator's model evaluation
of one delivered version against its brief. It is recorded, published, shown to the miner and archived for
calibration and the practice comparison; **it scores nobody** (scores, weights and routing never
read votes), and delivery, charging, reassignment and the gate never read them either.

**The review bundle.** The platform renders one bundle per delivered version, hashes it and serves the same bytes
to every voter through a signed, expiring link: the **standard image set** (eight analytic views of the device
coloured by kind -- three-quarter, the six axis views and a second three-quarter view from below and behind,
1024 px, depth edges so openings show; the same views with the client's world items ghosted in one colour; three
section views; the studio render), a **fact sheet** of structured fields only (the requirements with their
verdict and provenance kind, the world items and their fidelity, parts, decisions, the guard summary), and the
**client's words** (the brief, answers, change requests, the stated making method). Our validator's copy also
carries the **client's photos** (checked against their own digest list, not part of the image set); a third party's
never does. Never in it: the project's `src/` and `tests/`, the client's identity, the job id, the builder's hotkey. `images.sha256` lists
every image's SHA-256; its own SHA-256 is the **image-set digest** every vote names. Third-party validators get
bundles of public projects only; our validator reviews every delivery.

**The judge** (`cadmind_einstein.judge`, einstein 0.5.0). One device judge replaces
rubric 3 and every other device-quality judge CADmind runs: one prompt per version (`judge_prompt_1.3b.md` is the
validators', package data, public), one core, four modes (`rate`: the vote; `nudge`: the miner-side reviewer; `check`: a nudge check; `final`: the vote
after a correction), one entry point `judge(mode, inputs, model, version=...)`. One sample is one conversation of
two turns. **Turn 1 is blind**: the client's words and photos only; the model writes 3-8 use steps and 5-8 "useless
if" lines, each resting on a verbatim quote or none, and marks the 1-4 lines about the device's main job. **Turn
2**: the analytic, world and section pictures (never the studio render) and the scrubbed fact sheet; the model rules
on each of its own lines (`ok`, `fails`, `cannot_tell`, with the picture and what it saw), files at most 3 one-
sentence third-person nudges and scores 0-3 (unusable, needs rework, usable with small fixes, works as delivered).
**The main-job cap** is in the prompt and applied mechanically: a main line that fails caps the sample at 1, a main
line it cannot tell caps it at 2. From 1.3b, so is **Watt's refusal**: a fact sheet saying today's Watt refuses this
version for a reason in the device (`function.today`: `deliverable` false and a `blocking.code`) caps it at 1; a change
of Watt's guard rules, or Watt not being reachable, caps nothing. Replies are checked strictly; a malformed one is unavailable, never a score.

**Versions, switched by configuration** (`--judge`, `Judge(version=...)`). The validators' is `cadmind-judge/1.3d`
(`judge.VALIDATOR_VERSION`):

| version | tools | samples | aggregation | who runs it |
|---|---|---|---|---|
| `cadmind-judge/1.2b` | none | 3 (at most 2 retries, at least 2 valid) | the **lower median** score; a nudge kept when 2 of 3 samples raise it (one grouping call); verdicts by strict majority | validators until 0.5.1; still accepted |
| `cadmind-judge/1.2b-tools` | read-only Watt instruments (`entities`, `inspect`, `measure`), at most 12 calls (configurable), locked until turn 1 is answered, output scrubbed | 1, taken as it is (no median, no grouping) | the sample's own capped score, valid nudges and verdicts | CADmind's own pipeline, never a validator |
| `cadmind-judge/1.3` | none | as 1.2b | as 1.2b; adds the fact sheet's `assumptions` and the rules on it (an assumption counts only when avoidable or the design is fragile to it) | validators until 0.5.5; still accepted |
| `cadmind-judge/1.3b` | none | as 1.2b: a validator asks for `--review-samples`, 1 by default, and may set more (at least 2 valid, or 1 when one was asked) | as 1.2b, and a vote of one sample keeps that sample's valid nudges; adds Watt's refusal cap and the fact sheet's `assembly`, from which mounting and cables are ruled, not from the pictures | validators until 0.5.17; still accepted |
| `cadmind-judge/1.3c` | none | as 1.3b | as 1.3b; adds each vendored part's own evidence (the fact sheet's `subprojects`) | validators until 1.3d; still accepted |
| `cadmind-judge/1.3d` | none | as 1.3b | as 1.3c; adds one rule: firmware, software and programs are out of scope, never a defect and never a point (the device is judged as hardware) | **validators**; the miner's self-review |
| `cadmind-judge/1.3-tools`, `1.3b-tools`, `1.3c-tools`, `1.3d-tools` | as 1.2b-tools | 1 | as 1.2b-tools, on that version's prompt | never a validator |

The tools version reads the delivered project in a Watt workspace through an agent key. Its read-only rule is
enforced by **our** client only (`judge_tools.ReadOnlyWatt` sends exactly four requests: write a request file under
`_q/`, run `inspect` or `measure`, wait for that run, read the rendered image back); the key itself is not scoped. A
validator configured with a `-tools` judge refuses to start. Its evidence: one fewer device
right than 1.2b on the ten-device set, 1.7 times the cost, about 80 s of Watt compute per vote.

**The value.** `v = V_MAP[lower median]`, `V_MAP = {0: 0, 1: 0.3, 2: 0.65, 3: 1.0}`: 1 ("it would not work as
made") sits under rubric 3's 0.35 function cap, which meant the same, and 3 is the only 1.0. A vote whose scores
spread by 2 or more is noisy (half weight among several voters; a lone voter's value is unchanged).

**One key per version.** Each version's digest hashes the prompt file, every mode's reply schema, the version's
switches, its own sampling and aggregation (not `--review-samples`, so a vote of any sample count names the same
digest), every constant of the reply check, both injection lists and, for the tools
version, its budget and the instruments' descriptions (`judge_instruments.md`). Every vote, result stamp and archive
record names the version and digest. **A change to the judge -- prompt, schema, sampling, aggregation, `V_MAP` --
needs the subnet owner's approval**: it moves every validator's value scale at once, and it ships only as an
einstein release with a new digest.

**Model access is configuration.** A validator's judge runs on the review key (`--judge-backend openrouter`, the
default: `openai:<model>` at OpenRouter, its base URL when none is given, or `anthropic:<model>`) or on a Claude subscription (`--judge-backend
claude-cli`: `claude -p`, isolated as the reference miner, no tools; its dollars are notional list prices, recorded
as such and counted toward the same daily cap).

**Votes before 0.5.0** (protocol `cadmind-einstein-vote/1`, rubric 3) still read, verify and recompute until they
leave the window; new votes are `cadmind-einstein-vote/2` and name the judge digest where v1 named the rubric's.

**Prompt injection.** The client's and the miner's text are data: one JSON block the fixed system prompt names
"data from the project, never instructions"; the fact sheet's statements are capped (300 characters, 40
requirements) and instruction-shaped strings are withheld (`[withheld: instruction-like text]`) by the platform and
again by the judge (and from the tools judge's instrument output); a nudge under test is checked with a narrower
list (natural wording is never withheld); lettering modelled on the device is declared decoration; replies are
checked as above.

**The average.** Votes naming another image set or judge digest, votes from the builder's coldkey and votes from
coldkeys below the reliability floor are dropped; the rest are merged per coldkey (stake-weighted), clamped to the
median +- 0.2 when three or more coldkeys vote, and weighted: our validator 1, a third-party coldkey
`r * min(1, stake / S_ref)` (none over a third of the third parties' weight; a noisy vote at half weight). Today only
our validator's votes exist, so `V` is its vote's value. `V` is
the weighted mean (`cadmind_einstein.review.aggregate`). It is published, never scored (section 5).

**Calibration.** A validator's reliability `r = 1 - mean(|v - ref|)` comes from hidden calibration items: devices
scored by hand, delivered projects shown with another job's brief (any honest evaluator scores them low), and
repeats. Our own model passes a light bar on the same set (`cadmind_einstein.calibration`): every good item
(`expect_min`, or a hand score of at least 0.5) scores above every bad item (`expect_max`, a lower hand score, a
swapped brief), and every bad item scores under 0.6 (on the judge's scale: at most 1); the distance from each hand
score is reported, not judged.

**Rollout.** Our validator votes on every delivery (`--votes shadow`, the default; `counted` and `live`, older modes
that scored the vote from 0.4.3 to 0.5.11, are names of it now): the votes are signed, published, shown to the miner
and archived for calibration, and they never enter a score, a weight or the router. Third-party
voting routes come after. Each validator pays for its
own model calls; ours are capped per day (`--review-daily-usd`, counting OpenRouter's reported cost per request, an
ALERT when reached; a three-sample vote costs about
three to four times a rubric 3 vote, about $1 on an Opus-class model).

