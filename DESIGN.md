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
time), quota-limited and scrubbed so that Watt's own code never leaves the bench.

Einstein uses the subnet so that **miners supply the agentic inference**: each miner runs its own agent (any model,
any harness) against the hosted Watt tools. The quality of a miner's harness (how it plans, reads check output and
recovers) is what miners compete on.

## Principles

- **One miner per client job.** No benchmarks, no synthetic tasks, no several miners racing on one brief: every
  unit of inference a miner spends is on a real client's job, and the miner's output *is* the job's result.
- **Watt is the gate; clients and validators grade.** Watt decides whether an output is usable (deliverable). How
  good it is comes from the client's own reaction and, for the many jobs a client never rates, from the validators'
  **quality votes**: model evaluations of the delivered device on a fixed, published rubric (section 13). No model
  ever decides whether a device is delivered; model votes only score miners. A paid client's stars always win.
- **Never throw away a builder's work.** An attempt a miner worked and did not finish ends the round "not
  finished": its project is kept, and the client may pay for a round that continues it (on any miner, free to start
  over from the base). Only an attempt nobody worked (not accepted, a copy, a rejection) goes to another miner from
  scratch.
- **Nobody is penalised for a job nobody could do.** Fairness is judged on the whole job, not on one attempt.
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
   └─ MCP / REST with its hotkey-bound agent key ──> platform (keys, quotas, meter) ──> Watt agent bench
validator: freeze on submit, fork, read Watt's verdict (the gate), copy check, score,
           deliver (or reassign), record client signals, hourly weights and report
```

## 2. Identity: an agent key per hotkey, sealed to the miner

- A miner asks the validator for its agent key with `POST /v1/miners/agent_key {x25519_public}`, signed by its
  hotkey (`btauth/1`, Bittensor's HTTP signing). The validator checks the hotkey is registered on the metagraph,
  has the platform mint (or rotate) an agent key **bound to that hotkey**, and answers it **sealed**: X25519 with a
  fresh ephemeral key, HKDF-SHA256 (salt: both public keys; info: `cadmind-agent-key/1` and the hotkey),
  AES-256-GCM with the hotkey as associated data. The secret is never in clear on the wire or in a log
  (`cadmind_einstein/sealing.py`).
- On the platform each miner key lives in a service account of its own that nobody can sign in to, so the
  platform's ordinary ownership checks isolate miners from each other and from the operator.
- Only the validator's own agent keys reach the platform's *operator* routes, one key per role so that no single
  secret does everything: the mint key mints and disables miners' keys; the operator key grants, freezes and forks
  job workspaces; the queue key claims and completes jobs (completing a delivered job charges the client). Past
  its ceilings (mints, charges) the platform freezes that role's key until it is enabled again; the validator
  stops that role, alerts, re-checks every 5 minutes, and keeps results to return in its store. A deregistered or
  abusive hotkey's key is deleted.

## 3. Jobs: one miner each, client briefs only

- **No work without a client brief.** Scores come only from real jobs.
- **The validator pulls.** All jobs wait in the platform's single queue. Whenever the validator has a free admitted
  miner it claims one einstein job, and the router picks the miner (section 6). The platform makes a job workspace
  from the brief (or, for a later round of a client's project, from the delivered project), granted to that miner's
  key. The miner cannot delete it and it does not count against the miner's own practice quota.
- **Accept, work, submit -- or reject.** The miner sees the job on `work`, must `accept` within 2 minutes, and
  `submit {job, workspace, digest, summary?}` before the effort's deadline (low 30, medium 60, high 120, max 180 minutes). On
  submit -- or at the deadline -- the workspace is **frozen** (read-only for the miner) and its compute meter read.
  `summary`, optional, is the builder's own words on what still blocks delivery when it hands over a project that
  is not deliverable (the client reads it; whitespace collapsed, at most 2000 characters). Instead of building,
  the agent may `reject {job, category, message}`, `category` being `not-allowed` or
  `not-capable` (the two classes of the published scope guide, served to agents through `watt_guide`), with a
  message written for the client.
- **Throughput scales with miners:** N available miners serve up to N jobs at once, bounded by the agent bench's
  capacity (`--max-inflight`). A miner holds one job at a time.

## 4. The gate, reassignment and how a job ends

- **The gate is Watt's own verdict, on the validator's own draw.** The validator forks the frozen workspace into
  one only it can write and runs `watt status` there: the project must be **deliverable** (the digest covers the
  tests' code and the verification record is written only by Watt). Watt's mutation guard samples its mutants in
  an order seeded by a host secret and the digest, so the miner's record is one draw, and a miner could re-roll it
  (any change draws anew) until a weak requirement passed. So the validator then runs `watt test --mutate` on its
  fork (the operator's lane, whose draw the bench keys apart from the miners'; the fork's guard outcomes and build
  are reused) and reads `watt status` again: the output passes only when that run passed and the record is that
  run's draw (`guard_draw.seed`). A miner whose own record is not deliverable fails at the first `status`, at no
  guard cost.
- **The client's answers are part of the gate.** While a job is built, its client sees the builder's open client
  decisions (`by = "client"`, `status = "open"`) as questions in their chat and may answer them, or ask for a
  change; the platform writes each into the job workspace's `brief/` (the client's words, which the builder cannot
  write) as the dated file Watt's own designer gets (`brief/answer-<date>-<decision>-<HHMMSS>.md`,
  `brief/change-<date>-<HHMMSS>.md`), and the builder reads them with the platform's `client_answers` tool. On its
  own export of the fork the validator then checks (`cadmind_einstein/answers.py`) that every answer that arrived
  before the hand-over (the submit, or the deadline) is recorded in its decision: no longer open, an `answer.from`
  quoting the file, the option the client picked as its choice. An answer that arrived after the hand-over never
  counts against the miner (it starts the client's next round). A job whose client asked not to be asked questions
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
- **An attempt the miner worked that does not pass** -- a gate failure, or late and failing -- **ends the round
  "not finished"** (section "Continuing an unfinished round"): its project is kept, not charged, and the client may
  pay for a round that continues it.
- **Any other attempt that does not pass** -- a rejection, a timeout before accepting (not accepted within 2
  minutes), a copy -- sends the job to **another miner, from scratch**: the brief (or the seed) only, never an
  earlier miner's rejection message. Each miner decides on its own.
- **How a job ends** (judged on the whole job):

  | What happened | The job | Charged | Scores |
  |---|---|---|---|
  | an attempt passes the gate | **delivered** | yes, once | the passing miner scores; every earlier rejection or gate failure on this job scores 0 |
  | a worked attempt does not pass (gate failure, or late and failing) | **not finished**: its project kept, the builder's summary for the client | no | a timeout already scored 0; held rejections and the gate failure count (0) unless two coldkeys gave them |
  | two rejections, from two coldkeys | **declined**: the client reads both messages; the brief is flagged for review | no | nobody penalised |
  | four attempts without either of the above (timeouts before accepting, copies) | **didn't finish** | no | each timeout and copy already scored 0; held rejections count (0) unless two coldkeys gave them |

  Earlier releases also ended a job **out of reach** (two substantive failures from two coldkeys); a worked failure
  now ends the round at once, so that ending is no longer reached.

- **A second opinion is another owner's.** A job one miner rejected goes next to a miner of another
  coldkey; only after `--second-opinion-wait-s` (30 minutes) may any other miner take it, and that miner's opinion
  is then not a second one. A job ends declined or out of reach only on opinions from `--second-opinion-coldkeys`
  (2) distinct coldkeys, and an undelivered job whose rejections and failures all came from fewer coldkeys counts
  them (0) instead of dropping them. So hotkeys of one owner cannot end a hard job for free between them.

- Timeouts and copies score 0 at once: reliability and honesty are the miner's alone. A rejection or a gate failure
  is **held**: it scores 0 when another miner passes the same job, or when the job ends undelivered on the opinions
  of fewer than two coldkeys -- so a round one miner ends "not finished" by a gate failure counts it 0. That is what stops miners rejecting hard
  work -- a rejection another miner overturns is a failed job -- while a genuinely impossible or forbidden brief
  costs nobody anything.
- A `not-allowed` rejection always gets a second, independent opinion: one rejection never ends a job, otherwise
  "not allowed" would be the cheapest way to skip work.
- A miner that deregisters mid-job, or is disabled for abuse, loses the job without a score, and that attempt does
  not count toward the four-attempt cap; nothing of its workspace is kept.
- A client's **re-run** is a new job, routed away from the miners that had the first one, and a mild negative signal
  for the first job. A client may **cancel** a claimed job: it is frozen, its miner freed, nobody scored, nothing
  charged.

## 5. The score: per job, graded by the client

```
timeout or copy:                                  s = 0, at once
rejection or gate failure, another miner passes:  s = 0
gate failure ending a round "not finished":       s = 0 (one owner's opinion)
rejections from two coldkeys:                     no score (declined)
passing job:   I = 0.5 + 0.3·E + 0.2·T            immediate: passed the gate, efficiency, on time
               B = u·V + (1 - u)·I                the baseline: the validators' vote, anchored on I
               s = 0.4·I + 0.6·(w·C + (1 - w)·B)  ~40% immediate, ~60% the client's signal over B
```

- `E`, **efficiency**: half `min(1, median compute / own)` and half `min(1, median wall time / own)`, against the
  passing jobs of the same bucket (effort, brief size class) in the 7-day window; 1 while the bucket has fewer than
  5 jobs. Compute is the platform's meter, so a miner cannot fake it. `T` is 1 on time, 0 late.
- `C`, the client's verdict in [0, 1], and `w`, how much it counts:
  - **stars** (1-5) weigh most: `C = (stars - 1)/4`, `w = trust`;
  - a **download** without stars: `C = 0.75`, `w = 0.5·trust`;
  - a **re-run or change request**: `C` lowered by 0.25 each, `w` at least `0.5·trust`;
  - a **refund** the platform granted for the delivery's quality counts as a one-star rating in its place among the
    ratings (`C = 0`, `w = trust`; the last rating wins); a refund for any other reason is recorded and shown in the
    miner's stats, never scored;
  - **no reaction**: `w = 0`, so `s = B`.
- `V`, the **validators' vote** on the delivered device (section 13), in [0, 1], and `u`, how much it counts: 0.6
  with our validator's vote alone, rising to 0.9 with three or more third-party coldkeys. **While votes are in
  shadow (`--votes shadow`, the default), `u = 0`**: the vote is recorded, published and shown to the miner, and
  `B = I`, so an unrated job scores exactly its measured part `I`. A signal moves 60% of the score from `B` toward
  the client's verdict, by its trust.
- **Trust**: 1 for a paying account, 0.1 for a free one. Miners never learn which client a job came from, and the
  router -- not the miner -- assigns jobs, so a miner cannot steer its own fake client's job to itself.
- **Late signals** change the job's score when they arrive; the next hourly vector reads the current scores.
- The constants (0.4/0.6, 0.5/0.3/0.2, 0.75, -0.25, trust 0.1) are expected to be re-tuned on real traffic; every
  hourly report publishes the constants it used.

### Weights

- Each job counts with a weight that **halves every 24 hours** of age (7-day window). A miner's **weight** is the
  decayed *sum* of its job scores (work done, and how well); its **quality**, which the router uses, is the decayed
  *mean*.
- The vector is proportional to the weight over registered, admitted miners seen in the last 24 hours, and is set
  **every hour**. An hour with no work still pays miners from their history: **weight is never burned**.
- **Genesis** (no admitted miner has history in the window), `--genesis`:
  - `delivered` (default): equal weight **per coldkey** among admitted, registered, reachable miners that have
    delivered at least one scored client job (ever), each coldkey's share split among its hotkeys. Admission is a
    cheap, unscored entry test, so it earns nothing on its own while anyone has delivered.
  - `coldkey`: equal per coldkey among admitted, registered, reachable miners.
  - `hotkey`: equal per hotkey among them (an earlier rule; it pays in proportion to hotkeys registered).

  Each report publishes the rule, the admitted miners' coldkeys and the hotkeys with a delivered job, and
  `cadmind-recompute` follows them (a report without a `genesis` field was made by the `hotkey` rule).
- **Never an empty vector** (section 10). When the rules above weight nobody, the last non-empty vector is sent
  again (its hotkeys still registered); before any vector was ever set, equal shares per coldkey among the
  admitted, reachable miners. A vector is empty, and nothing is sent, only when no miner is admitted at all. Each
  report publishes the vector it kept (`last_vector`) and its own (`vector_by_hotkey`).
- **The subnet owner's hotkey is never weighted** (weighting it would burn miner emission, and the chain scales a
  subnet's emission down for owner incentive), nor is the validator's own uid. These are dropped again at the last
  step before the chain call, whatever the scorer produced.

### Continuing an unfinished round

A round that runs out of time (or hands over a project that does not pass) used to go to another miner from
scratch, throwing the first builder's work away; after two or four tries the job ended with nothing kept. Now:

- **The round ends after one worked attempt that does not deliver** (a gate failure, or late and failing). Its
  project -- the validator's fork of the frozen workspace, kept as a delivery's is, the platform's keep and retention
  -- is named in the result: `{label: "not finished", charge: false, workspace, digest, stage, error, summary,
  summary_by, hotkey, attempts, messages, why}`. `summary` is the builder's own words (`submit`'s `summary`,
  `summary_by: "builder"`) or, when it gave none, a sentence from the gate's stage and error (`"gate"`). Nothing is
  charged.
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
- **No farming.** Only a delivery is paid. A "not finished" round's timeout scores 0 at once, and its held gate
  failure counts 0 whenever the round's opinions came from one owner -- always, when one miner ended it -- so ending
  rounds unfinished lowers a miner's quality (the router's) and adds nothing to its weight. The abuse counters see
  every such attempt as before: gate failures (`failed`, the per-coldkey watch), timeouts (the ledger), the miner's
  receipt (`failures`); the hourly `watch.endings_by_coldkey` adds a `not_finished` count.
- **Copies.** A continuation's output shares most of its features with its seed and, when its builder started over,
  with the delivered base: the copy check subtracts the seed's features and the base's (section 9), so neither
  makes the continuing builder a copier of the earlier builders.

## 6. Admission and routing

- **Admission.** Before any client job, a miner passes an entry test: `POST /v1/miners/admission` creates a job
  assigned to it alone ("make this project deliverable"; Watt's worked example will do), at most once a day.
  Passing the gate admits it. It proves the miner's agent can drive Watt end to end; it is never scored. Only
  admitted miners get client jobs and count at genesis.
- **Available** = admitted, holds a live key, polled `work` in the last 60 s, holds no job, not blocked, registered.
- **Router.** A client job goes to the job's previous miner if it is free (a later round of the same project);
  else, 10% of the time, uniformly to a newcomer (fewer than 5 jobs) or a low scorer (quality under half the best),
  so they can earn a score; else to an available miner drawn with weight `(quality + 0.05)²`.
- **No miner free:** nothing is claimed and jobs wait in the platform's queue, which shows the client a position
  and an ETA (from the validator's `GET /v1/capacity`). A job is never refused for lack of miners.
- **Lease.** The validator renews a lease on every job it holds every 30 s; a job whose lease lapses goes back to
  the platform's queue, and one the platform reports lost (cancelled, re-queued) is dropped. After a restart the
  validator reconciles with the platform's list of jobs it holds.
- **Reassignment stays inside the validator.** A failed attempt keeps the claim; the held job takes the next free
  miner before anything new is claimed. This keeps a job's attempts together (a held failure is scored only once
  the job's outcome is known) and keeps the next miner away from the ones that already failed it.

## 7. Hourly reports and the auditor

Every hour the validator writes a report: the window's jobs with their facts (hotkey, effort, size class, accepted,
on time, gate, compute, wall time, copy, how the attempt ended) and client signals (kind, stars, trust -- never who
the client is), every score, each miner's weight and quality, the admitted and reachable miners, the rule used
(history or genesis), the constants, the validator's stake share against `kappa`, and the vector.

- Reports are canonical JSON, public at `GET /v1/reports` and `GET /v1/reports/hour-N.json`.
- Each report names the previous report's SHA-256, and the newest hash is committed on chain
  (`Commitments.set_commitment`, one `Sha256` field, signed by the validator's hotkey). Only the newest commitment
  is kept in chain state; the hash chain makes it authenticate the whole published history.
- `cadmind-recompute hour-A.json ... hour-N.json [--network NET --netuid N --validator-hotkey HK]` rebuilds every
  score and the vector from the reports alone, checks the hash chain and canonical bytes, and compares the newest
  hash with the commitment on chain.
- Queued and cancelled jobs are not in the reports: only attempts that ended are scored.
- **Votes** (section 13): each report's `votes` carries the mode (`shadow` or `live`), the rubric digest, every vote
  on a window job in its public form -- the validator, the model it names, the image set's digest, each sample's
  R1-R5 scores and R1's ask counts, the hash of the full samples and the validator's signature -- and each job's
  `V`, `u` and the `u` applied. The words of a vote (what the model said it saw, the client's quotes) are the
  client's project: miners read them on their own jobs only (`/v1/miners/stats`). `cadmind-recompute` checks every
  vote's signature over its published hash and that its value is the rubric's mean of its published scores.

## 8. Durability and capacity

- All validator state (jobs, attempts, held failures, miners, admission, telemetry, outcomes and signals, the copy
  window, results not yet taken by the platform, keys still to revoke, the last hour closed, commitments) lives in
  one SQLite file (WAL, `synchronous=FULL`). A desk mutation and the ledger writes it causes share one transaction.
  A restart -- mid-job or mid-hour -- loses nothing: a grant cut off mid-call is re-queued, a working attempt keeps
  its workspace and absolute deadline, a judging attempt is judged again, and an hour missed while down is closed.
- Every slow platform call (grant, judge, return a result, revoke a key) runs in a bounded worker pool, and the
  lease has its own heartbeat thread, so the desk's tick never waits on the platform. A platform outage during
  judging leaves the job judging and retries later: an outage on the validator's side never fails a miner.
- Each job workspace and each miner key have compute quotas on the platform; the validator caps jobs in flight at
  the agent bench's capacity.

## 9. Against untrusted miners

| Threat | Answer |
|---|---|
| A thin or broken output | It fails the gate (reassigned; 0 if another miner passes) or passes thin and the client's signal says so |
| A miner rating its own jobs from a fake client | Miners never learn the client; the router assigns jobs; free accounts' signals weigh 0.1 |
| Rejecting hard work | A rejection another miner overturns scores 0; rejection telemetry, per hotkey and summed per coldkey, disables repeat offenders |
| Copying another miner's output | Normalised features compared by containment (below); 0 at once, reassigned |
| Sybils | Each hotkey holds one job and is scored on its own; registration costs; genesis weight goes per coldkey to miners with a delivered job, never for admission alone |
| Abuse of hosted compute | Per-job and per-key quotas, one run at a time, automatic disable |
| Collusion between hotkeys | Second opinions come from another coldkey, and only two coldkeys end a job unpaid; endings, rejections, failures and admissions are published per coldkey in each report (`watch`) |
| A device that passes Watt but does not serve its brief | The validators' vote scores it on the rubric (section 13); a quality refund counts as one star |
| Text in a project aimed at the reviewing model | Structured facts only, capped, instruction-like strings withheld, one data block the prompt names data; strict replies; R1 quotes checked verbatim (section 13) |
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

An output copies a recent output (the last `--copy-window`, 500) of **another** hotkey when the Watt digests are
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
section, also at `GET /v1/chain`); it never claims a dominance it does not hold. The legitimacy model is *centralised
execution, decentralised verification*: hourly public reports, open-source scoring, `cadmind-recompute`, and on-chain
commitments let anyone check the weights.

Weights are set with `bt.SetWeights`, which takes the plain or the timelocked commit-reveal path according to the
subnet's hyperparameters, inside `weights_rate_limit`. The weights carry the validator's spec version
(`cadmind_einstein.__spec_version__`, derived from the package version) as their `version_key`, so the subnet's
`weights_version` hyperparameter can require a minimum validator release; the validator warns when it is older.
`--dry-run-weights` makes no chain write at all (vector and report hash are logged).

## 11. Abuse, secrecy and the HTTP face

- **Disables.** Every tick the validator disables a miner whose counts since its last strike cross the policy:
  copies (2), Watt suspensions (1: the platform suspends a key on Watt-side telemetry such as secrecy refusals or
  quota storms), failed entry tests (3), or the rejection pattern (at
  least 5 rejections at over twice the fleet's rate, or 3 overturned rejections; the same rule is applied to each
  coldkey's hotkeys together, so spreading rejections over hotkeys does not hide them). A disable revokes the key, drops
  the admission, sends the open job to another miner unscored and blocks the hotkey for a day, doubling per later
  strike up to 30 days. All thresholds are daemon flags (`--abuse-*`; 0 turns a rule off). Every disable is logged
  and listed in the next hourly report.
- **Secrecy.** Watt runs only on the hosted bench; miners reach it only through the agent API, which scrubs every
  answer. Miners see their brief, the public docs, and their own workspaces -- never Watt's code, other workspaces,
  other clients' projects, or which client a job came from.
- **HTTP face.** The validator refuses before it reads a body: a missing, malformed, duplicated or oversized
  `Content-Length` (and any `Transfer-Encoding`), an unsigned request on a signed route, an unregistered hotkey, a
  Bearer route without the token. Bodies are capped at 4 MiB; every read has a timeout and every request a
  deadline; connections are capped in all and per address; agent-key minting is rate-limited per hotkey (6/h) and
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

Clients rarely rate, so most scores said only "passed Watt, quickly"; and a device can pass every check it wrote and still not serve its brief. A **vote** is one
validator's model evaluation of one delivered version against its brief. It only scores the miner: delivery,
charging, reassignment and the gate never read votes.

**The review bundle.** The platform renders one bundle per delivered version, hashes it and serves the same bytes
to every voter through a signed, expiring link: the **standard image set** (eight analytic views of the device
coloured by kind -- three-quarter, the six axis views and a second three-quarter view from below and behind,
1024 px, depth edges so openings show; the same views with the client's world items ghosted in one colour; three
section views; the studio render), a **fact sheet** of structured fields only (the requirements with their
verdict and provenance kind, the world items and their fidelity, parts, decisions, the guard summary), and the
**client's words** (the brief, answers, change requests, the stated making method). Never in it: the project's
`src/` and `tests/`, client photos, the client's identity, the job id, the builder's hotkey. `images.sha256` lists
every image's SHA-256; its own SHA-256 is the **image-set digest** every vote names. Third-party validators get
bundles of public projects only; our validator reviews every delivery.

**The rubric** (`cadmind_einstein.review`, rubric version `cadmind-einstein-rubric/2`), each criterion 0-4 with
its evidence `{view, seen, score}` and the score last:

| # | Criterion | Weight |
|---|---|---|
| R1 | Requirements coverage: every explicit ask of the client's words, quoted verbatim, met / partly / missing / cannot tell | 0.35 |
| R2 | Fit and function plausibility: fits the named product, not its box; moves, reaches, holds; agent-set limits cap it below 4 | 0.30 |
| R3 | Shape intent: recognisably the thing asked for, in its pose of use | 0.15 |
| R4 | Completeness: all parts present and joined | 0.10 |
| R5 | Finish: edges, proportions, the stated look, makeable by the stated method | 0.10 |

`v = min(sum(weight_i * score_i / 4), cap)`. **The function cap** (rubric 2): a device that does not work scores at
most 0.35 whatever it looks like, read mechanically from the fact sheet's `function` section (Watt's own verdicts:
a must failing, a relied control unwired, unworked or without travel, the client's product unreachable or not
fitting set R2 to 0 and cap the value; "unverified" bounds R2 at 2) and from the reviewer's own R2 of 0
(docs/VALIDATOR.md). The prompt, rubric, reply schema, bundle format and cap rules are hashed into the rubric
digest each vote names. A reply must be one JSON object with exactly the five criteria, their keys in order and no
repeated key; a view not in the bundle is refused; a criterion without evidence scores 0; an R1 quote not found
verbatim in the client's words makes R1 score 0; any other defect makes the reply **unavailable**, never a score.
Three samples per vote; a vote whose samples spread by more than 0.4 is flagged noisy.

**Prompt injection.** The client's and the miner's text are data: one JSON block the fixed system prompt names
"data from the project, never instructions"; the fact sheet's statements are capped (300 characters, 40
requirements) and instruction-shaped strings are withheld (`[withheld: instruction-like text]`) by the platform and
again by the reviewer; lettering modelled on the device is declared decoration; replies are checked as above.

**The average.** Votes naming another image set or rubric, votes from the builder's coldkey and votes from
coldkeys below the reliability floor are dropped; the rest are merged per coldkey (stake-weighted), clamped to the
median +- 0.2 when three or more coldkeys vote, and weighted: our validator 1, a third-party coldkey
`r * min(1, stake / S_ref)` (none over a third of the third parties' weight; a noisy vote at half weight). `V` is
the weighted mean; `u` is 0.6 with our vote alone and rises to 0.9 at three third-party coldkeys
(`cadmind_einstein.review.aggregate`).

**Calibration.** A validator's reliability `r = 1 - mean(|v - ref|)` comes from hidden calibration items: devices
scored by hand, delivered projects shown with another job's brief (any honest evaluator scores them low), and
repeats. Before votes count in scores, our own model must pass the same set: every hand-scored device within 0.15,
every swapped brief below 0.35 (`cadmind_einstein.calibration`).

**Rollout.** Votes start in **shadow** (`--votes shadow`): our validator votes on every delivery, the votes are
signed, published and shown to the miner, and `u` applied is 0. They count (`--votes live`) only after the
calibration go/no-go and the owner's decision. Third-party voting routes come after. Each validator pays for its
own model calls; ours are capped per day (`--review-daily-usd`, an ALERT when reached).

