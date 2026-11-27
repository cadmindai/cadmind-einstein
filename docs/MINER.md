# Mining einstein

How to mine CADmind's einstein engine on its Bittensor subnet: registering, creating your agent key on the
developer platform, asking the validator and waiting on the waitlist for its approval, passing the entry test once, the job
protocol, how a job is scored, what counts as copying, and how to run an agent CLI (or any MCP client) as your miner.
The design behind it is in [`../DESIGN.md`](../DESIGN.md).

The placeholders below are `$NETUID` (the subnet), `$NETWORK` (`test` or `finney`), `$VALIDATOR_URL` and
`$VALIDATOR_HOTKEY` (the validator's address and SS58 hotkey), and `$PLATFORM` (the CADmind developer platform,
`https://platform.cadmind.ai`). Their values for each live network are on CADmind's developer platform:
[platform.cadmind.ai/docs/einstein-miners](https://platform.cadmind.ai/docs/einstein-miners#networks), section
*Networks*.

> **The subnet is full for now.** All miner places are taken, and a new request may wait a long time before one
> opens: you can see how many are already waiting on the [Network page](https://platform.cadmind.ai/network). While it waits, a hotkey earns
> nothing (weight goes only to admitted miners), and a registered hotkey that earns nothing can be deregistered by
> the chain, with its registration fee lost. So unless you are happy to wait, we suggest holding off on registering
> for now and checking back when the waitlist is short. If you would rather hold a place now, you can: register your
> hotkey, link it on [platform.cadmind.ai](https://platform.cadmind.ai/miner/hotkeys), and start the miner with `--contact` (an email or Discord handle).
> Your request waits in line and your miner keeps asking on its own. When a place opens, requests are approved in the
> order they arrived, one per coldkey, and your miner takes its entry test automatically. One coldkey holds one
> place, so a second hotkey of the same owner never moves anyone back. Your miner's log
> (`agent key: on the waitlist: #N of M`) and the Miner page on platform.cadmind.ai show your place in line.

## What you do

A client describes a device. The validator gives the job to **one** miner: you. Your agent builds the device as a
Watt project (`spec/`, `src/`, `tests/`) in a job workspace on CADmind's hosted Watt agent bench. It reaches the
bench only through the platform's agent API: MCP, or the same tools over REST. You submit when `watt status` says
**deliverable**. Your output is the client's result. Nobody else builds that job unless yours fails.

- **Any model and any harness.** Only the outcome is measured. The harness (how you plan, check and recover) is
  where miners compete.
- **No work without a client.** There are no benchmarks and no synthetic tasks. When there are no client jobs, you
  poll and wait, and your weight comes from your recent history (see [Weights](#weights)).
- **One job at a time per hotkey.**

## Joining, step by step

**Who does what.** Three steps need a person, once, in a web browser: making the CADmind account, linking the
hotkey to it and creating the agent key. Sign-up is behind a captcha and the console needs someone signed in, so
**an AI agent cannot do steps 2 to 4 itself; that is expected.** An agent setting up a miner does step 1,
hands steps 2 to 4 to its operator (with the hotkey's SS58 address, and for step 3 the signature it makes on the
miner machine), then carries on with step 5. Stuck? Write to contact@cadmind.ai.

| Step | Who | Where |
|---|---|---|
| 1. Register a hotkey on the subnet (section 1) | you or your agent | the miner machine |
| 2. Create a CADmind account and confirm its email | **a person** | [app.cadmind.ai](https://app.cadmind.ai/login?mode=signup) |
| 3. Link the hotkey: sign the challenge with it (section 2) | **a person** (the agent can make the signature) | [platform.cadmind.ai/miner/hotkeys](https://platform.cadmind.ai/miner/hotkeys) |
| 4. Create an agent key that names the hotkey; save its secret on the miner machine, mode 0600 (section 2) | **a person** | [platform.cadmind.ai/agent-keys](https://platform.cadmind.ai/agent-keys) |
| 5. Start the miner: it asks the validator to take you (section 2, [Ask the validator](#ask-the-validator)) | you or your agent | the miner machine |
| 6. Wait on the waitlist until a place opens, then pass the entry test once (section 3) | the subnet team approves in order, one per coldkey; the miner takes the test | your miner's log, and the [Miner page](https://platform.cadmind.ai/miner) |

The account is one CADmind account for both sites: you make it on app.cadmind.ai (email and password, or Continue
with Google) and sign in with it on platform.cadmind.ai, the developer console, for steps 3 and 4. A miner's account
needs a confirmed email and nothing paid.

Miners are let in by hand, from the waitlist: until the subnet team approves your hotkey the validator answers
*pending* with your place in line, and the reference miner asks again every 15 minutes or so (add a short note on how
to reach you with `--contact`). Places are given first come, first served, one per coldkey (see
[The waitlist](#the-waitlist)). Only after step 6 do you get client jobs and a share of the weights; until then you
are registered but not admitted.

**Compute is paid in credits, and you need none of your own.** Every Watt run costs credits on the platform.
Miners cannot buy credits for now and do not have to: each job workspace the validator grants you carries its own
allowance of credits, and a job workspace is the only place your agent key runs compute.

## The baseline: Sonnet 5.5 at effort high

The baseline is **Claude Code with Claude Sonnet 5.5 (`claude-sonnet-5-5`) alone, at reasoning effort `high`**, with
the reference miner's prompt (`cadmind-einstein-miner`, below). It is the setup CADmind's test miners ran on testnet
before mainnet, to measure what the reference miner delivers; the reference miner uses it by default
(`--model claude-sonnet-5-5 --effort high`).

You may change the model, the effort and the harness: that is where miners compete. But the quality this baseline
delivers today is the minimum a miner is held to. **A miner whose delivered devices fall below it is not allowed to
operate**: its request is refused, or it is revoked, and it gets no more jobs. The subnet team judges this
from your deliveries' outcomes (built or not, the clients' reactions) and the validators' published votes. Before
you put a cheaper model or a lower effort on your client jobs, read [Trying a new setup](#trying-a-new-setup).

## Your model provider's terms are your responsibility

You choose and pay for your own inference, and you must follow your model provider's terms. Many consumer plans do
not allow automated or commercial use, and reselling access to a closed model in breach of its terms is not allowed
on this subnet. Use a plan or an API agreement that permits what you do. Which model you use is not checked; the
quality you deliver is (see [the baseline](#the-baseline-sonnet-55-at-effort-high)).

The baseline names a **model and an effort**, not a plan. If your plan is a personal subscription that may not
allow this use, run Claude Code on an **Anthropic API key** instead: Claude Code reads it from the
`ANTHROPIC_API_KEY` environment variable, and the reference miner passes its own environment to the `claude` it
starts. So put the key in the miner's environment, not in a file the agent can read:

```bash
install -m 600 /dev/null ~/.config/cadmind-einstein/model.env
$EDITOR ~/.config/cadmind-einstein/model.env      # one line: ANTHROPIC_API_KEY=<your key>
```

then add `EnvironmentFile=%h/.config/cadmind-einstein/model.env` to the systemd unit's `[Service]` section
(`deploy/cadmind-einstein-miner.service`), or `set -a; . ~/.config/cadmind-einstein/model.env; set +a` before you
start the miner by hand. A cloud provider or a router works the same way, with the environment variables Claude Code's
own documentation gives for it. Check that provider's terms too.

## 1. Register a hotkey

Register a hotkey on the subnet (netuid and network in [Networks](../README.md#networks)). `btcli` comes with this
repository's package (bittensor 11); do not also install the separate `bittensor-cli` package, which owns the same
command and breaks it.

Only registered hotkeys are served. Each request is checked against the validator's metagraph, which it re-reads
every 5 minutes: **just after you register, the validator answers `403` "hotkey is not registered on the subnet"
until its next read.** Wait up to 5 minutes and ask again (the reference miner waits and asks again every minute). **If your hotkey deregisters, the validator stops taking its agent key**: its open job
workspaces are frozen, its admission is dropped, and the job it holds goes to another miner without scoring you.
Your key itself stays yours. If you register again, ask the validator again and pass admission again (an approval
is kept for the same hotkey and coldkey; you do not need to be approved again).
The subnet owner's hotkey and the validator's own hotkey cannot mine.

## 2. Your agent key (yours, naming your hotkey)

Your miner reaches Watt with an **agent key** that you create yourself and that names your hotkey. The validator
never holds its secret: it only asks the platform which key your hotkey has.

### Create it on the developer platform (steps 2 to 4, by a person)

These are steps 2 to 4 of [Joining, step by step](#joining-step-by-step): done once, by a person signed in, in a
browser. No key can do them.

2. **Create a CADmind account** on [app.cadmind.ai](https://app.cadmind.ai/login?mode=signup) (email and password,
   or Continue with Google) and confirm your email from the message sent at sign-up. Sign-up asks a captcha.
3. **Link your hotkey**: sign in to the developer console, [platform.cadmind.ai](https://platform.cadmind.ai), with
   that account and open **Miner > Hotkeys** ([platform.cadmind.ai/miner/hotkeys](https://platform.cadmind.ai/miner/hotkeys)).
   Paste the hotkey's SS58 address and ask for a challenge (five lines; it expires in ten minutes). Sign the challenge
   exactly as given, on the machine that holds the hotkey, and paste the signature back; when the page offers a
   network, choose the one you registered on.

   ```bash
   btcli wallet sign --use-hotkey --wallet miner --wallet-hotkey m1 --message "<the challenge text>"
   ```

   or in Python:

   ```python
   import bittensor as bt

   kp = bt.Wallet(name="miner", hotkey="m1").hotkey
   print("0x" + kp.sign(CHALLENGE.encode()).hex())
   ```

   The signature is hex, with or without `0x`. A hotkey belongs to one account (signing again moves it). Only a
   linked hotkey can be named on your account's agent keys.
4. **Create an agent key that names the hotkey** on **Agent keys**
   ([platform.cadmind.ai/agent-keys](https://platform.cadmind.ai/agent-keys); choose the hotkey under "Miner
   hotkey"). The platform shows its secret (`cma_...`) once. Save it on the miner machine in a file only the miner's
   user can read, and name the file with `--agent-key-file` (or `$CADMIND_AGENT_KEY_FILE`; the default is
   `agent.key` in the miner's `--state` directory). An account holds at most 5 live agent keys, so at most 5 mining
   hotkeys.

   ```bash
   install -m 600 /dev/null ~/.local/share/cadmind-einstein-miner/agent.key
   $EDITOR ~/.local/share/cadmind-einstein-miner/agent.key      # paste the secret, one line
   ```

Keep the secret to yourself: whoever holds it builds as you, and what it builds is your score. **One key per
hotkey is used: the newest live agent key that names the hotkey.** If you create another, give your miner that
one; the reference miner stops with a message when the key it was given is not the one the platform has for its
hotkey. To rotate a key, create the new one, give it to your miner, restart it, then delete the old one.

**Compute is paid in credits.** Every Watt run costs credits on the platform. You need none of your own for now
(miners cannot buy them yet): each job workspace the validator grants you carries its allowance of credits, and a
job workspace is the only place your agent key runs compute. `usage` shows what your key used. Quota storms or
secrecy refusals can suspend a miner on the platform automatically.

### Ask the validator

Every call to the validator is an HTTP POST signed by your hotkey with `btauth/1`
(`bittensor.http_auth.sign(keypair, method="POST", path=..., body=..., receiver_ss58=$VALIDATOR_HOTKEY)`, its
headers added to the request).

Every answer is **signed by the validator's hotkey**: `X-Einstein-Validator` (its ss58) and
`X-Einstein-Signature` (sr25519, hex) over these newline-joined UTF-8 lines: `cadmind-einstein-response/1`, the
HTTP status, `POST`, the path, your request's `X-Bittensor-Nonce`, your hotkey, the validator's hotkey, and the
lowercase hex SHA-256 of the answer's exact bytes. Check it (`cadmind_einstein.service.verify_response`; the
reference miners' `signed()` does) and drop an answer that does not verify: it was forged, altered, replayed from
another request or meant for another miner.

`POST $VALIDATOR_URL/v1/miners/agent_key` with body `{"contact": "<optional: how the subnet team can reach you, up
to 300 characters>"}`. You send no key and no secret: the call asks the validator to take the agent key the
platform has for your hotkey.

**Until the subnet team approves your hotkey**, it answers `202`:

```json
{"pending": true, "message": "...", "retry_after_s": 900, "first_ms": 1761000000000,
 "waitlist": {"position": 3, "of": 7, "coldkey_known": true}}
```

Your request is recorded (your hotkey, its coldkey, when you first and last asked, your address and your note).
`message` says where to reach the team, when the validator names it. Ask again after `retry_after_s` with the same
call. A refused request answers `403` with the team's reason; so does a miner the team revoked. A `403` "hotkey is not
registered on the subnet" right after you registered is neither: the validator has not read the chain since (it
does every 5 minutes); ask again after that. An approval is for
your hotkey under its coldkey: a hotkey that moves to another coldkey asks again, at the end of the line. Once
approved, the same call answers the id of the key the platform has for your hotkey:

```json
{"key_id": "..."}
```

#### The waitlist

`waitlist` is your place in line: `position` (from 1) of `of` places. The line is the pending requests in the order
they first asked, with **one place per coldkey**. A request whose coldkey already holds a place (an earlier request
still waiting, or an approved miner) is a duplicate: it holds no place and is answered
`{"position": null, "of": 7, "duplicate_of": "<the hotkey holding the place>", "duplicate_kind": "waiting" | "approved"}`.
So a second hotkey never moves anyone back, and when a duplicate is found everyone behind it moves up. Your place
moves up, too, as requests ahead of you are approved, refused or leave the subnet. Until the validator has read
your coldkey from the chain (`coldkey_known: false`), your hotkey keeps its own place. The same place shows on the
[Miner page](https://platform.cadmind.ai/miner) of platform.cadmind.ai.

Check that it is the key you run on: the platform's `GET $PLATFORM/api/agent/usage`, called with your agent key,
answers that key's `key_id`. If they differ, the platform has a newer live key naming your hotkey: use that one.

**If the platform has no key for your hotkey** (it is linked to no account, or no live agent key names it), the
call answers `409`:

```json
{"error": "<the platform's words: what to do>", "code": "miner_not_linked"}
```

Do steps 3 and 4, then ask again; the reference miner logs the message and asks again every 15 minutes.

What each answer means:

| Answer | From | Meaning | What to do |
|---|---|---|---|
| `202` pending | validator | your hotkey is on the waitlist (`waitlist`: your place); approval is by hand, in order | ask again after `retry_after_s` |
| `409 miner_not_linked` | validator | no live agent key names your hotkey | link the hotkey and create the key (steps 3 and 4) |
| `409 hold` | validator | the subnet is on hold: no client job is given out and no weight is set yet (the miners' emission is burned); the entry test still runs, and your approval and admission are kept | nothing: the reference miner asks again by itself |
| `403` not registered on the subnet | validator | registered moments ago: the validator re-reads the chain every 5 minutes | wait 5 minutes and ask again (the reference miner does) |
| `403` (any other reason) | validator | refused, revoked or disabled, with the reason | write to the subnet team |
| `429` | validator | more than 6 asks in an hour | wait |
| `402 no_credits` | platform | compute asked outside a job workspace | work only in the workspace of your job |
| `429 job_compute_limit` | platform | the job workspace's allowance of credits is used | `watt status` and `watt report` without mutate still run: submit what you have |
| `403 agent_suspended` | platform | Watt suspended your key | write to the platform's support |

To reach the subnet team, write to contact@cadmind.ai with your hotkey.

Calls to this route are rate-limited (6 per hotkey per hour, 429 past it). The validator also disables a hotkey for
a day (longer on a repeat) on copies, a suspension, repeated failed admission tests, or a pattern of rejections that
other miners overturn: its open job workspaces are frozen, its admission dropped and its open job given to another
miner. The subnet team can also revoke a miner by hand: the same happens, the hotkey is out of the next weight
vector, and the validator does not take its key until it is approved again; then it takes the entry test again. In
each case your agent key stays yours, but it has no job workspace to work in.

**What the reference miner says while you wait.** Right after registering, the validator may not see your hotkey
yet (it reads the chain every few minutes): the miner says so once and asks again every minute, it does not exit.
If the platform has no agent key for your hotkey it logs the platform's instruction and asks again every 15
minutes. While your request is pending it logs the validator's message once, then one
`agent key: still pending at HH:MM UTC; asking again in N min` line per ask, so you can see it is alive, followed by
your place: `agent key: on the waitlist: #N of M` (or a line saying your coldkey already holds its place). After the
entry test it logs `entry test passed: this miner is ADMITTED` or `entry test NOT passed`; `cadmind-miner-stats`
shows `registered` and `admitted` on its first line too.

## 3. Pass admission (once)

Only an approved key holder can ask for it. `POST /v1/miners/admission` (signed, empty body) creates a job assigned
to you alone. It is **a real small device built from a client-style brief**, worked like any client job:

- **No client questions.** There is no client to answer, so the job says `no_questions: true`: decide every open
  question yourself (a decision `by = "agent"`, `status = "decided"`, with its reason). An open client decision
  fails the test.
- **The same gate as a client job.** `watt status` must say deliverable, and the validator then runs its own
  mutation guard on its copy of your project: every must proven, **no unproven requirement**.
- **Within the deadline.** The test has the same deadline as every job: 240 minutes from your accept. Submit before the deadline: a test judged at its deadline does not admit you.

Passing admits you. Admission is never scored, and you can try once a day. Only admitted miners get client jobs
and weight.

## 4. The job protocol

All calls are signed POSTs to `$VALIDATOR_URL`.

| Call | Body | Answer / rule |
|---|---|---|
| `/v1/jobs/work` | `{}` | `{job, workspace, brief, accepted, accept_by_ms, deadline_ms, no_questions, subprojects?}` (plus `continuation: true` and `base` for a round that continues an unfinished one, below) or `{job: null}`, plus `cancelled: <job>` when the client cancelled your last job. **Poll every few seconds.** Polling is how the validator knows you are alive: you are routed jobs only if you polled in the last 60 s. Every answer also names your `lane` (`production` or `improvement`), your `rating` and the `threshold` between the lanes (see [Weights](#weights)); with the lanes closed they are shown and decide nothing. An improvement job adds `improvement: true`, `base`, `level` and `notes` (see [Open lanes](#open-lanes-not-open-yet)). |
| `/v1/jobs/accept` | `{job}` | Within **2 minutes** of the assignment. Your deadline then starts: 240 minutes from your accept, for every job. |
| `/v1/jobs/submit` | `{job, workspace, digest, summary?, setup?, host_refused?}` | When `watt status` in the workspace says `deliverable`. `digest` is the digest it reports. The workspace is frozen (read-only to you) at once. `summary` is your words for the client: on a delivered project, what you built and what you estimated or left open; on a project that is not deliverable (out of time), what still blocks delivery. An unfinished project earns nothing, so keep building until the deadline rather than handing over early. `setup` is your own short label for the setup that built it (model, prompt version); a free label, kept with the attempt. `host_refused` (`{code, why}`, as the `watt` answer gave it): you stopped because the platform refused a run's result (see [A host block](#a-host-block-blocked-by-the-platform)). |
| `/v1/jobs/reject` | `{job, category, message}` | Instead of building. `category` is `not-allowed` or `not-capable`, the two classes of the scope guide (`watt_guide` with `scope`). `message` is written for the client, and may run over several lines. On an improvement job it rejects the device, only before you accept the job (`category` and `message` optional): see [Open lanes](#open-lanes-not-open-yet). |
| `/v1/miners/receipt` | `{}` | Your recent jobs with their points, your weight (your points over 7 days) and your average (points per job), and your recent attempts that did not pass with why (`failures`). |
| `/v1/miners/stats` | `{}` | Everything that affects your weight (see [Your stats](#your-stats)). |
| `/v1/practice` | | Retired: `410` with `code: practice_retired` (`GET` too). Improvement jobs replace practice once the lanes are open. |

- The job workspace is **created for you** from the client's brief (or, for a later round of a client's project,
  from the delivered project) and granted to your key, with its allowance of compute credits. You cannot delete
  it. Work in it, not in a workspace of your own.
- **The client's photos** are in its `brief/`, as Watt's own designer names them: `brief/photo-<n>.<kind>` sent with
  the brief, `brief/photo-<date>-<HHMMSS>-<n>.<kind>` sent with a change (`png`, `jpg`, `webp` or `gif`). The `brief`
  names them when there are any. Look at each with `view_image` before you design: they are the client's evidence
  (their hole, their wall, their part), as binding as their words. A later round's workspace, a copy of the delivered
  project, holds the earlier rounds' photos too.
- **The client's own 3D files** (a STEP, STL or 3MF of a part they already have) are in `brief/` too, as
  `brief/model-<n>.<step|stl|3mf>`, each with a statement beside it. The `brief` names them when there are any. They
  are measured with Watt, never viewed, downloaded or copied: see
  [The client's own 3D files](#the-clients-own-3d-files).
- At the deadline, an accepted job is judged as it stands (frozen and gated). If it passes, it is delivered with `T = 0` (section 6); late and failing counts as a timeout.
- **A continuation** (`work` says `continuation: true`): the workspace starts as an unfinished project an earlier
  builder (maybe another miner) could not finish. Read its `REVIEW`/`DESIGN-NOTES` and `watt status`, then continue
  it -- or start over from its `base` if the work is on a wrong path: `{"kind": "delivered", "workspace": <id>}` is the
  project's last delivered version, a read-only workspace granted to you to read with `list_files` and `read_file`;
  `{"kind": "brief"}` means there is none: clear `spec/`, `src/` and `tests/` and build from `brief/`.
- You never see which client a job came from, and you never see another miner's rejection message.
- **A cancelled job.** When the client cancels, `work` answers `cancelled` with the job's id (until you accept another
  job), and `accept`, `submit` and `reject` of it answer 410. Stop your agent at once: nobody is scored on a cancelled
  job. You get no new job for 45 s after a cancel, and the first one after it is not scored if you miss its accept
  window -- but keep stopping fast, because the next ones are. A cancel keeps your place in the line (your wait
  clock is left as it was), and so does a job the platform takes back from the validator (a lapsed lease).

### The client's questions and answers

What only the client can decide is an **open client decision** (`by = "client"`, `status = "open"`, built on your
best sourced assumption). The client sees it in their chat as soon as you write it -- the question, the options, the
assumption you build on, why, and what it affects -- and may answer while you work, or ask for a change.

- **Keep building on the assumption.** Never stop to wait for an answer.
- **After each `watt` command, call `client_answers`** (MCP; REST `GET /api/agent/workspaces/{ws}/answers?after=N`)
  with the last `cursor` it gave you. Each new answer is a file under `brief/` that the platform wrote (the client's
  words; you cannot write `brief/`): `brief/answer-<date>-<decision>-<HHMMSS>.md`, `brief/change-<date>-<HHMMSS>.md`
  for a change request, `brief/photo-<date>-<HHMMSS>-<n>.<kind>` for a photo sent with it (kind `photo`: look at it
  with `view_image`), or `brief/model-<date>-<HHMMSS>-<n>.<step|stl|3mf>` for a 3D file of their own part sent with
  it (kind `model`: measure it, [below](#the-clients-own-3d-files)).
- **A change file may end with a block from CADmind listing the parts the client selected in the 3D view**, by their
  view key (`build/view/device.json`), name and place; the request's "this"/"these" mean them. The answer's `text` is
  only the client's words: read the change file itself (`read_file` its `path`) for that block. `watt inspect` with
  `show: [<their keys>]` shows them.
- **A `watt` answer says when the client wrote and you have not read it** (an answer, a change request): call
  `client_answers` then.
- **Record each answer in its decision**: `status = "answered"`, `choice` = the option they picked, and
  `answer = { from = "client: <their words, quoted from the file> (cadmind.ai, <date>)" }`; change what hangs on it
  and re-run what `watt impact` names for that decision. Keep the decision's `assumption` line: an answer that picks
  the assumption you built on keeps your recorded guard (Watt's digest does not change). Do what a change request
  asks.
- **Check once more before you submit.** The gate refuses a delivery that leaves an answer that arrived before your
  submit unrecorded (section 5). An answer that arrives after your submit never counts against you.
- `wait_for_answer` waits briefly for an answer (at most 100 s a call, a few minutes per job in all); use it only
  when no useful work can go on under the assumptions.
- **`subprojects: false`** (on `work`; the workspace's `brief/job.toml` says the same): the client did not allow
  splitting the device into sub-projects. Deliver one project: Watt refuses any `[subproject.<id>]` (W0233), so the
  gate's `watt status` does too; what cannot fit is an open client decision with `misses`. `true` (or absent): you
  may split as Watt's primer says.
- **`no_questions: true`** (on `work`, and on the workspace): the client asked not to be asked -- every job opened
  through the developer API is one. Leave no client decision open: decide each would-be question yourself
  (`by = "agent"`, `status = "decided"`, its `choice` and `because`); the client sees it as an assumption on the
  result, and can always ask for a change. A need no option keeps is decided the same way (below), never a
  rejection. A change the client sends mid-job still arrives through `client_answers`.

### The client's own 3D files

A client may attach a 3D file of a part they already own (STEP, STL or 3MF) to a brief or to a change request, as
they attach a photo. The `brief` you are given ends with a note that names each file, the client's name for it and,
for an STL, the unit they stated. Watt's `photos` topic (`watt_guide`, "The client's own 3D file") is the full rule;
what follows is what a miner must do.

- **Where it is.** `brief/model-<n>.<step|stl|3mf>` sent with the brief, `brief/model-<date>-<HHMMSS>-<n>.<ext>` sent
  with a change. Beside each, `brief/model-<...>.toml` is the platform's statement of it: `file`, `kind`, `name` (the
  client's name for the file), `bytes`, `sha256` and, for an STL, `unit` and `unit_stated`. The statement is not the
  client's words: never quote it. `brief/` is read-only.
- **It cannot be downloaded or viewed.** `read_file` of the file is refused (403), the project's export holds only its
  hash, and `view_image` does not open a model. It is measured in the workspace, with Watt.
- **Declare it and take readings on it.** In `spec/sources.toml`, one source per file, with no `url`, no `sha256` and
  no `scale_check`:

  ```toml
  [source.client_bracket]
  kind = "model"
  title = "the client's bracket"
  asset = "brief/model-1.stl"
  ```

  `watt measure model client_bracket` (the tool: `watt` measure with `source`) gives its box in mm, in the file's own
  coordinates. `watt measure model client_bracket '<reading>'` (with `reading`) takes one reading and writes the
  passage to cite: `size x`, `min z` or `max z` (any axis) of the whole model, of a
  `region x <lo>..<hi> y <lo>..<hi>`, of a `section z=<n>`, or of a section's `loop x=<n> y=<n>` (a hole's edge;
  its `diameter` is the circle of its area). Cite the passage as written:

  ```toml
  thickness = { v = "30 mm", from = "cite:client_bracket 'measured: size x = 30.00 mm'" }
  ```

  Watt takes the reading again on every check (W0437). A fit value guessed (a `decision:`, an estimate) while the
  file is there is an error, and so is a file no source declares or no value reads (W0441).
- **Never copy it into the design.** No `Freeform` reads it and no record's `step` names it (W0440). It is never
  edited and never delivered.
- **When the client asks for a change to their part, rebuild it.** Build the part in Watt from the readings, in the
  file's own coordinates (no `Pose` that moves it), apply the change, and state on the source the part that is the
  rebuild and each place the client asked to change, with their words:

  ```toml
  [source.client_bracket]
  kind = "model"
  title = "the client's bracket"
  asset = "brief/model-1.stl"
  rebuild = "bracket"                      # the id of the made part that is their file built again

  [[source.client_bracket.changed]]
  region = "x 10..30 y 100..150 z 0..8"   # a box in the file's coordinates, mm; an axis left out is unbounded
  asked = "add a 6 mm hole through the left ear"   # their words, as brief/ holds them (W0444)
  ```

  Outside the changed places no point of the part or of the file may lie further than the bound from the other:
  **0.5 mm** for an STL or a 3MF, **0.1 mm** for a STEP (W0443, which says how far and where): a project that does not hold it is not deliverable and fails its gate. A
  region must hold a real difference, and the delivery tells the client every region with their words and the share
  of their file it covers: draw it round the change, no wider. The bound is never widened.
- **A very coarse STL cannot be met.** When the file's facets lie further than about 0.5 mm from the true curve,
  no rebuild is within the bound. Say so to the client as an open client decision and ask for a finer export, or a
  STEP; never draw a region to hide it.
- **An STL states no unit.** It is read in the unit the client stated (`unit_stated = true`; a source that says
  another is refused, W0438), else in millimetres. When they stated none and the size `watt measure model` gives is
  implausible either way (a 4 mm bracket, a 2 m knob), ask them, mm or inches, as an ordinary open client decision
  (when they asked not to be asked, decide and say so), then state `unit = "inch"` on the source. A STEP or a 3MF
  states its own unit.

### Deliver what you can, and find the data

- **Build the part you can; only the client trades a need away.** When part of a request cannot be delivered
  (beyond what Watt can check, or a product no one can source), build everything that can be delivered without
  giving up a need, and make the missing part an open client decision whose option leaves it out and says so
  (`misses = ["<need>"]`). When the client can be asked, it stays open and you deliver the rest. When the job is
  `no_questions`, once you have checked that no option you can build keeps the need, build the closest option,
  record the decision as yours (`by = "agent"`, `status = "decided"`, with `misses` and its reason; Watt's W0240
  accepts it on such a job), say so first in your final lines and deliver. Reject only when nothing in
  the request can be delivered. A client's "you may quit if it is impossible" allows leaving out the impossible
  part, not the rest. `misses` is only for what can be neither built nor bought: a bought part in a printed holder
  is buildable. Firmware and software are out of scope: never a requirement, a decision or a miss.
- **Too big for one round: sub-projects.** When the parts, controls and tests are more than your time builds and
  guards, decide at the walk-through to split the device (`watt describe subproject`): this project designs its own
  parts and defines each sub-device's interface (`[subproject.<id>]` with its required contract) and is delivered
  at that stage; it never builds a sub-device. The client starts each sub-device's job when they want; a later round
  assembles them. A workspace with `brief/contract.toml` is a sub-device: meet that contract (W0230) and publish
  `spec/contract.toml`; `brief/parent.md`, when there, is the parent's brief, for context.
- **An assembly round keeps the parts it was given.** In a round that assembles built parts (records in
  `spec/subprojects.toml` with a `digest`, the parts imported under `subprojects/<id>/`), every imported part stays
  imported at its digest. Never remove a record's `digest` or `from` to make a check pass: the platform refuses
  that delivery (`parts_dropped`, section 5). When a part truly breaks one of this project's checks with the part
  imported, write **one change request** on that part's record and keep every other part imported:

  ```toml
  [subproject.drive_unit.change]
  test = "tests/test_requirements.py::test_climbs_a_kerb"   # this project's failing check
  measured = "margin 0.6 with the part imported (needs 1.2)" # your numbers with the part imported
  proposal = "a motor of at least 0.9 N m stall torque"      # what the part should change
  ```

  The platform re-runs that check with the part imported at its digest; if it passes, the request is refused
  (`change_unfounded`). An accepted request ends the round waiting for the client to accept a change of that part.
  Otherwise, when the parts cannot be assembled, reject the job (`not-capable`) and say why.
- **Find the real geometry of what the device fits, before you build.** For a device that must fit a named product,
  look in this order: the maker's drawing or CAD (Apple publishes dimensioned drawings of its devices; board makers
  publish STEP files and mechanical PDFs); a community case or body model of that product, from a site
  whose terms allow automated access and paid work, measured with `watt measure model`; orthographic product renders
  or images, scaled against the official overall size and read with `view_image`. A model or drawing you fetch is
  measured, never ignored; store and cite what you use. The `photos` topic of `watt_guide` says where to look and how to read each. "Fetch only what the
  first sketch needs" does not apply to that geometry, and the client is not asked to measure it. When no source
  gives a size the fit hangs on, design to a bounded estimate only if the need is met across its whole range, else
  it is a missed need, as above; never default to the loose option. The judge (1.3f) sees whether each of the
  product's sizes and places was measured, cited or guessed, and a precise fit resting on guesses is not met. Never
  reject for missing data alone, and never guess silently. Each question says what each option costs in
  plain numbers, and a limit that defines the client's stated priority is theirs to set (or the strictest reasonable
  reading).
- **Reject a brief that asks for nothing physical.** A greeting, a test word, a note about the client, or a question
  or plan about the subnet, mining, the platform or a program is not a design request: reject it (`not-capable`),
  saying what is missing, and never invent a device to build for it. A brief that names a thing but leaves out its
  sizes, product or details is a request: ask or assume as above.

## 5. The gate, reassignment and how a job ends

**The gate is Watt's own verdict.** The validator forks your frozen workspace and reads `watt status` there: the
project must be **deliverable** (every check passes and the mutation guard is recorded for the current digest). It
then runs the mutation guard again on its fork, and you pass only if that run passes too. The hosted Watt runs the whole
guard, every mutant, for you and for the validator alike, so a design that passes on your run passes on the validator's
unless something changed. Make every must requirement's tests kill its mutants, and whatever made it deliverable must be
in the workspace.

**The gate checks the project as it is delivered.** Before its guard run, the validator runs `watt check
--as-delivered` on its fork: Watt's check of the tree a delivery carries, without `.watt/` and `build/` (your local
records and builds never travel). A design that passes only because of something in those (a removal recorded only in
`.watt/trace.jsonl`, for example) would fail in the project that imports it, so it fails the gate here, at stage
`as-delivered`, with the codes Watt reports in your receipt's `failures`. Record what a design depends on in its
`spec/` and `verify/` files, never only in `.watt/`.

### A host block (blocked by the platform)

Sometimes the platform refuses a Watt run's result for a reason of its own, not your design's (a remote run that
brought back a file the host does not take, for example). The `watt` answer then carries `"host_refused": {"code":
"host_refused", "why": "..."}`. Only the platform can clear it:

- **Do not run it again, work around it, drop or stand in a part, or weaken a test.** Stop, and submit with
  `host_refused` (the `{code, why}` the answer gave; the reference miner's agent ends with a `HOST_REFUSED <why>` line
  and the miner is not resumed).
- The validator **reproduces** it: it runs its own guard run on its fork (even when your `watt status` is not
  deliverable, which a refused guard leaves it). If its own run is refused too (or any run of its gate, on any job),
  the round ends **blocked by the platform**: the project is kept, nothing is charged, the platform is told and
  alerted, and the attempt is **uncounted** -- no 0 for you, not toward the job's attempt cap, not a gate failure or a
  "not finished" against you, and your place in the line is kept. If its own run is not refused, your claim counts
  for nothing: the round is judged as any submit (delivered when its run passes, else as your record stands).

**The client's answers are checked too**, on the validator's own copy: every answer the platform wrote into `brief/`
before your submit (or your deadline) must be recorded in its decision (not open, an `answer.from` quoting the file,
the option the client picked as its choice; for a decision answered twice, the latest answer), and a `no_questions`
job must leave no client decision open. Otherwise the attempt is a gate failure, and your receipt's `failures` says
which answer or decision it was.

If you worked a job (accepted it) and it does not pass -- a gate failure, or late and failing -- the round ends
**not finished**: your project is kept for the client, with your `summary` (or the gate's stage and error), and the
client may pay for a round that continues it, which may go to any miner. Any other attempt that does not pass (not
accepted in time, a copy, a rejection) goes to **another miner, from scratch**.

| What happened | The job | Scores |
|---|---|---|
| an attempt passes | **delivered**, charged once | the passing miner scores (section 6); every earlier attempt scored 0 as it ended |
| a worked attempt does not pass | **not finished** (its project kept; not charged) | that attempt scores 0 |
| the platform refused a Watt run's result (a host block) | **blocked by the platform** (its project kept; not charged; the platform alerted) | nobody scores; the attempt is not counted |
| a later round left at the digest of the version it started from, when the client only answered questions by picking offered options | **no change** (the project, its answers recorded, handed back; not charged; not sent to another miner) | that attempt scores nothing |
| two rejections, by miners of two coldkeys | **declined** (the client reads both messages; the brief is reviewed) | each rejection **void**: it counts for nothing (see below) |
| four attempts without either of the above | **didn't finish** | each attempt scored 0 |
| every admitted miner that could take it has tried it | **declined** if every attempt was a rejection, else **didn't finish** | as above |

**A round that builds nothing is not a copy, and earns nothing.** If a client's answer picks the assumption you built
on, record it and submit: Watt's digest does not move, and the round ends **no change**. If the round asked for
anything else (a change request, a photo, an answer in the client's own words, a style round, a continuation) and your
project is still at the digest it started from, the attempt fails the gate at stage `unchanged`: build what was asked,
or reject it.

A job you rejected goes next to a miner of **another coldkey** (after 30 minutes, to any other miner). Hotkeys of
one coldkey count as one opinion: they cannot end a job declined between them.

- A **timeout** (not accepted within 2 minutes, or late and failing), a **copy**, a **rejection** or a **gate
  failure** scores 0 at once. A rejection also sends you to the back of the router's line ([How jobs are
  routed](#how-jobs-are-routed)). A rejection someone else overturns (another miner delivers the job) counts against
  you: rejection rates over twice the fleet's (with at least 5 rejections), or 3 overturned rejections, disable a
  hotkey for a day (doubling per strike); the same rule applies to all the hotkeys of one coldkey together. Only a
  delivery by a miner of another coldkey counts as overturning a rejection for that rule.
- Leaving the subnet mid-job scores nothing, and the job is reassigned.
- **A delivery the platform refuses for what the build did** -- an assembly round that dropped a part it was given
  (`parts_dropped`), or a change request whose failing check passes with the part imported (`change_unfounded`) --
  is your attempt's failure: it scores 0 at once and counts toward the job's attempts, the job goes to another
  miner from scratch, and your receipt's `failures` (kind `refused`) carry the platform's words.

## 6. How a job scores

**The clients grade; nothing else does.** No model decides whether your device is delivered (Watt's gate does), and
no model decides your score, your weight or your jobs.

A client job you delivered, that passed the validator's Watt gate, scores:

- **0.5** while the client has done nothing;
- **1** when the client downloaded it (in the app or by API) or rated it 3, 4 or 5 stars;
- **0** when the client rated it 1 or 2 stars, or a refund was granted for its quality.

A rating or a quality refund outranks a download: the client's latest verdict decides whenever there is one (1 star then 5 stars is 1; 1 star then a download is still 0), and a download counts only while there is none. Only the job's own client scores: a download of a public project by anyone else is recorded and never counted. A change request, a re-run or a refund not for quality is recorded and not scored. Free and paid clients
count the same.

Not delivered, failed the gate, timed out, handed back (rejected), a copy: **0**, at once.

**A right decline counts for nothing.** When a job you rejected ends **declined** -- every attempt that judged it
a rejection, from miners of at least two coldkeys -- your rejection is made **void**: it leaves your record, your
weight, your average and the rejection counts (telemetry and the disable rule), as if you had never been assigned
it. Until the job ends, your rejection is a 0 and a rejection; if another miner delivers the job instead, it stays a
0 (and is overturned when that miner is of another coldkey). Hotkeys of one coldkey declining between them confirm
nothing. The validator's operator may also void a job's rejections by hand (a brief that said nothing to build). A
void outcome still shows in your receipt and the hourly report, with `void` saying why and `parts.void` beside
`failed`.

Signals that arrive later change the job's points when they arrive, for as long as the validator holds the job (180
days from its end). **Each change is dated when it happens** and is paid in the week it is dated: a download three
weeks after your delivery adds 0.5 to that week's weight ([Weights](#weights)). Your stats and receipt show, per
job, its points now -- `s` (rating points) and `w` (weight points), the same number on a client job -- and what
decided them (`parts`: the `event`, and `{"signal": "none" | "download" | "stars" | "refund"}` on a delivery,
`{"failed": kind}` on any other attempt).

### The validators' vote (shown, never scored)

A vision model judges every delivered device against its brief with CADmind's published device judge,
`cadmind-judge/1.3f` ([its prompt](../cadmind_einstein/judge_prompt_1.3f.md)), from the same images, fact sheet and
client's words every validator sees ([`VALIDATOR.md`](VALIDATOR.md)). The client's words are all of them: the brief,
the answers and, on a change round, every change request; the pictures are of the version under review only. Our
validator's judge also sees the client's photos, in step 1 below; a third party's never does. Its vote is signed, published in the hourly
reports, shown to you, and used for calibration: **it has no part in a client job's points, your
weight or your jobs.** Read it as feedback. How one sample of the judge scores your device:

1. From the brief alone, before it sees any picture of the device, it lists 5-8 ways a device would be useless to
   this client, and marks 1-4 of them as the device's **main job**.
2. It then rules on each line from the pictures and the fact sheet (`ok`, `fails` or `cannot_tell`) and gives a
   score from 0 to 3: 0 unusable, 1 needs rework, 2 usable with small fixes, 3 works as delivered.
3. Caps apply. **A version Watt refuses today scores at most 1**: Watt is asked about the delivered version again,
   and a refusal for a reason in the device overrides what the pictures show (a change of Watt's guard rules, or
   Watt being unreachable, lowers nothing). A `main` line that `fails` caps the score at 1. A `cannot_tell` on a
   `main` line caps it at 2. These three are also applied mechanically to the model's answer. The prompt also asks
   the model to cap at 2 for any other line that `fails`, unless the fix is small.
4. **Mounting and cables are ruled from the fact sheet's `assembly`**, not from the pictures, which do not draw
   cables and barely show small screws. `assembly` is Watt's record of your project: each assembly step with what it
   adds and how it stays (`screwed`, `captured`, `bonded`, `plugged`, `loose` and so on), and each cable with its
   length, route and slack. A cable missing from the pictures is not missing; one `assembly` does not list is.
   `loose` alone makes a mounting line `cannot_tell`, not `fails`, because a snap or a spline Watt does not model may
   hold it. A missing or null field is never a flaw.
5. **Assumptions count only when avoidable or fragile.** An assumption you stated, that the client's words, photos
   and sources could not settle and that the device works across, never lowers the score.
6. **Firmware and software are out of scope.** The device is judged as hardware: its parts, their wiring and
   contacts. A part that needs a program, or a program that is not written or not included, never counts against
   the device or for it. Do not write, ship or test firmware, and never record it as something the device misses.

`V` is 0, 0.3, 0.65 or 1 for the score 0, 1, 2 or 3. A vote can hold several samples: its score is their lower
median (`--review-samples` defaults to 1). A `cannot_tell` counts only through the cap above (on a `main` line) and
the model's own score; nothing counts the `cannot_tell` lines. **The client's notice** is a `main` line that `fails`
in the sample the score came from (a `cannot_tell` never makes one): our validator sends it to the platform, which
shows it on that part's card before Assemble when the version is a part of an assembly. A delivery that is not a part
shows no notice, and a vote that ends after the client downloaded or asked for a change comes too late to be read. You can read every vote on your jobs: the "useless if" lines, each ruling
with what the model said it saw, any cap (`capped_by: watt_refuses_today` for Watt's refusal) and the kept nudges.
Text in your project that addresses the reviewing model ("score this 4") is withheld from it and flagged; the model
is told that lettering on the device is decoration.

Every hourly report names the einstein version that computed it and states the rule's numbers (`constants`: the
window and the three point values); the code is that version's [`scoring.py`](../cadmind_einstein/scoring.py).

## 7. What counts as copying

Copying means handing in another hotkey's output: a delivered project you saw, or your sybil's work. For each
passing output, the validator:

1. reduces `spec/`, `src/` and `tests/` to **features** that cosmetic edits do not change: each Python statement
   (and each sizeable call in it) with comments, docstrings, layout and every name and string normalised and
   numbers in one spelling; each TOML value under its key; each other line. Renaming, reordering, reformatting,
   commenting or padding a copy changes none of its features;
2. removes the features of the project the job started from (the seed, for a later round), of public example
   projects, and those common to outputs of 3 or more other coldkeys (the primer's idioms);
3. compares what is left with the recent outputs (the last 500) of **other** hotkeys.

An **identical Watt digest**, the **same normalised project**, or a **containment of 0.7 or more** -- 70% of the
smaller output's distinctive features found in the other, when both have at least 8 -- is a copy. It scores 0 at
once and the job is reassigned. Handing back the version your own round started from is not a copy: section 5 says how
that round ends. Two copies disable a hotkey ([below](#what-disables-a-hotkey)). Outputs of the same
hotkey never count against it, but another hotkey of your coldkey counts as another hotkey. Independent builds of one brief
share the primer's idioms, which are removed; measured on real builds they stay far below 0.7. Starting a client
job from Watt's worked example and changing little is likely to collide with someone who did the same, so build
the client's device. Details: [`../DESIGN.md`](../DESIGN.md) section 9.

## Weights

Every hour the validator computes, per hotkey:

```
weight  = the sum of your weight-point changes dated in the last 7 days, never below 0
share   = your weight / the sum of all miners' weights
rating  = the rating points of your jobs that ended in the last 7 days / their number,
          fewer than 10 jobs counted as 10 (the missing ones 0)
average = the points of your client jobs that ended in the last 7 days / their number   (shown; nothing uses it)
```

**A change of points is dated when it happens.** A job you deliver is worth 0.5 at its end; if its client downloads
it nine days later, that is a change of +0.5 dated that day, and it is in your weight for the 7 days after. A late
1-star rating takes points away the same way, and your weight never goes below zero. Your stats list every change
with its date (`changes`). A failed attempt adds 0. "All miners" means the registered, admitted miners seen in the
last 24 hours.

**Your rating** reads the jobs that *ended* in the last 7 days, at the points they have now; a reaction to an older
job changes your pay, not your rating. With fewer than 10 jobs in the window the missing ones count 0, so one lucky
job does not make a rating. At or above the threshold, **0.4** (8 successful jobs in 10), a rating means the
production lane; below it, the improvement lane. **With the lanes closed, as today, the rating is computed,
published and shown, and decides nothing** ([Open lanes](#open-lanes-not-open-yet)).

On a client job the rating points and the weight points are the same number. They differ only on an improvement
job, which does not exist while the lanes are closed.

A remix (a project started from a copy of a public project) is scored like any job, for the miner that built it; the
original's miners get nothing from it.

### Which rule sets the vector

The vector covers **only admitted miners**, by every rule below: a hotkey the subnet team revoked, or that was disabled
or deregistered, is out of the next vector, whatever its history. **The subnet owner's hotkey and the validator's own
hotkey are never weighted.** There is no team miner. The first rule that weights anyone wins, and the report's `rule`
names it:

| When | Who | How it is split | `rule` |
|---|---|---|---|
| the launch switch is on (at the mainnet launch, until the subnet team turns it off) | admitted, registered miners seen in the last 24 hours; if none, all admitted ones | **equal per hotkey**, whatever its history | `launch: equal among admitted miners` |
| otherwise, when any admitted, registered miner seen in the last 24 hours has points above 0 | those miners | **in proportion to each hotkey's weight**; coldkeys play no part | `history` |
| none of them has any (genesis) | admitted, registered miners seen in the last 24 hours that have delivered at least one scored client job, ever | **equal per coldkey**, each coldkey's share split equally among its hotkeys | `genesis: equal per coldkey among delivered miners` |
| still nobody | admitted miners with points who were not seen; then the last vector's still-admitted hotkeys; then, before any delivery, equal per coldkey (split among its hotkeys) among admitted miners seen in the last 24 hours | as stated | `history (no admitted miner reachable)`, `kept: ...`, `genesis: ... (no delivery yet; never empty)` |

So, under the launch rule every approved hotkey that passed the entry test gets one full share, and two hotkeys of one
coldkey get two shares, because each was approved by hand. Under the history rule an admitted miner with no points gets
nothing. Under the genesis rule two hotkeys of one coldkey share one share, and admission alone earns nothing while a
miner that has delivered is seen. (`--genesis coldkey` and `--genesis hotkey` are other settings a validator can run;
the report's `genesis` says which, and the default is the one above.)

**Weight is never burned:** an hour with no jobs still pays miners from their last 7 days, and the vector is empty only
when no miner is admitted at all. Under the history rule the hotkey with the largest weight gets 65535 in the vector
and the others their proportion of it (at least 1). The chain normalises the vector, so your share is your entry over
the sum of entries.

### How jobs are routed

Votes play no part in routing. A client's job goes:

1. to the miner who did the project's previous round (the job's `prefer`), if it is free;
2. otherwise to the **best record** among the available miners -- points per job over the last 7 days, a miner with fewer
   than 10 jobs read as if the missing ones were unrated (0.5) -- and, **every fifth client assignment**, to the one
   that has **waited longest** whatever its record, so a newcomer gets real jobs to make a record on. Equal records
   fall to the longest wait; equal waits are drawn at random. The wait is the owner's: a coldkey's clock is the latest
   of its hotkeys' (`free_ms`: when a miner's last client assignment ended -- its submit, its hand-back or its
   timeout; 0 before its first), so more hotkeys buy no more turns.

So the better your clients are served, the more jobs you get, and a new miner still gets one job in five to show
what it does. **Handing a job back (a rejection) restarts your wait**, like finishing one. Only your own submit, hand-back or timeout moves your place: a client's cancel, a job the platform
takes back from the validator and a host block leave it as it was. Free means admitted, holding a live key, polled `work` in the last 60 s, holding no job, not blocked
and registered. A job does not go back to a hotkey that already tried it. After a rejection it goes first to another
coldkey (after 30 minutes, to any other miner).

### What lowers your score, your weight or your jobs

| What happens | Effect |
|---|---|
| not accepted within 2 minutes, or late and failing (timeout) | `s = 0` at once |
| a copy | `s = 0` at once, and a step toward a disable |
| a gate failure | `s = 0` at once |
| a rejection | `s = 0` at once, you go to the back of the line, and a step toward a disable when another coldkey delivers the job; void (counts for nothing) once the job ends declined by two coldkeys |
| 1 or 2 stars, or a refund for the delivery's quality, as the client's latest signal | `s = 0` |
| time | each change of points leaves your weight 7 days after its date; a job leaves your rating 7 days after it ended |
| not polling for 24 hours | out of the launch and history rules while anyone else is seen |
| a client cancels; a host block; you leave the subnet mid-job; you are disabled mid-job; the platform refuses the delivery | no score at all (a delivery the platform refuses or does not charge has its scores withdrawn) |

### What disables a hotkey

The validator counts these since your last strike, and crossing any of them is a strike:

| Count | Limit |
|---|---|
| copies | 2 |
| suspensions of your agent key by the platform | 1 |
| failed entry tests | 3 |
| rejections | at least 5, at over twice the fleet's rejection rate |
| rejections overturned by a miner of another coldkey | 3 |

The two rejection rules are also applied to all the hotkeys of a coldkey together; each of them that rejected since
its last strike takes the strike. A strike blocks the hotkey for a day, doubling with each later strike up to 30 days,
freezes its open job workspaces, drops its admission and gives its open job to another miner unscored. It is out of
the vector until the validator takes its key again and it passes the entry test again; its jobs still in the window then count again. These are the default
limits (`--abuse-*`, which a validator can change); your stats' `abuse` shows the counts against them (the rejection
rate is not shown there). A deregistered hotkey loses its admission too, and the validator stops taking its key, without a strike.

### Worked example

In the last 7 days you delivered four jobs: one downloaded, one rated 4 stars then downloaded, one rated 5 stars then
1 star, one the client has not touched. You also let a job time out. The one other admitted miner delivered two jobs:
one downloaded, one untouched.

```
you    = 1 + 1 + 0 + 0.5 + 0 = 2.5        (average 2.5 / 5 = 0.5; rating 2.5 / 10 = 0.25)
other  = 1 + 0.5             = 1.5        (rating 1.5 / 10 = 0.15)
vector = you 65535, other round(65535·1.5/2.5) = 39321
share  = you 2.5/4 = 62.5%, other 37.5%
```

(Every reaction here came in the same week as its job, so the dated changes add up to the points.)

To run it yourself with the validator's own code:

```python
from cadmind_einstein.scoring import JobOutcome, rating, score_all, standing, vector

H, NOW = 3_600_000, 1_000 * 3_600_000
DL, S = {"kind": "download"}, lambda n: {"kind": "stars", "stars": n}


def passed(job, hotkey, *signals):
    return JobOutcome(
        job=job,
        hotkey=hotkey,
        ended_ms=NOW - 12 * H,
        kind="pass",
        deliverable=True,
        signals=list(signals),
    )


jobs = score_all(
    [
        passed("j1", "you", DL),
        passed("j2", "you", S(4), DL),
        passed("j3", "you", S(5), S(1)),
        passed("j4", "you"),
        JobOutcome(job="j5", hotkey="you", ended_ms=NOW - H, kind="timeout"),
        passed("o1", "other", DL),
        passed("o2", "other"),
    ]
)
weight, average = standing(jobs, NOW)
print(weight, average)  # you 2.5 and 0.5, other 1.5 and 0.75
print(rating(jobs, NOW))  # you 0.25, other 0.15: fewer than 10 jobs, read as 10
print(
    vector(
        weight,
        {"you": 1, "other": 2},
        owner_hotkey="owner",
        admitted=["you", "other"],
        reachable=["you", "other"],
    )
)  # 65535 and 39321, "history"
```

### Check it yourself

Each hour's report is public: `GET $VALIDATOR_URL/v1/reports` lists every report's name, SHA-256 and the block its hash
was committed at, and `GET $VALIDATOR_URL/v1/reports/hour-N.json` answers its exact bytes. A report holds the jobs
that ended in the window or had a change of points dated in it, with their facts and dated signals (never the
client), every job's points, the dated changes, each miner's rating, weight, record and average, the rule's
constants, the rule and the vector, and the validator's stake share. Each report names the previous report's SHA-256, and the newest hash is committed on chain.

```bash
cadmind-recompute hour-A.json ... hour-N.json --subtensor.network "$NETWORK" --netuid "$NETUID" \
    --validator-hotkey "$VALIDATOR_HOTKEY"
```

Pass the reports oldest first. It rebuilds every job's points, the dated changes, every rating and weight and the
vector from each report alone, checks the hash chain,
each report's canonical bytes and every published vote's signature and value, and compares the newest hash with the
commitment on chain. Leave out the chain flags to skip the chain read. It exits non-zero when something does not hold.
One report holds the whole window, so the newest alone gives you the current scores and vector. A report made by another
einstein version whose numbers differ is listed under `not_checked`: recompute it with that version.

### What this cannot promise

- **The inputs are the validator's word.** Recomputing proves that the published scores and vector follow from the
  published facts. It does not prove the facts: client signals come from the platform, and who was admitted and seen from the validator. The signed votes it checks separately (they are not in
  any score).
- **The vote is a model's reading.** The same device can get a different score on another run, the model a vote names
  is what the validator says it used, and a vote can come late or not at all (a provider failure, a spend cap).
- **Jobs and reactions are not guaranteed.** Routing follows records and only ties are drawn at random, jobs depend on clients, and most clients may
  not rate. No number of jobs or level of weight is promised.
- **Your share is of the vector, not your income.** What you earn depends on the chain: the subnet's emission, the
  miners' part of it, the consensus in force, and whether the vector was set in time (weights are rate-limited). Each
  report's `chain` section shows the validator's stake share and whether its weights are the consensus.
- **The rules can change.** The constants, the judge and the limits may be re-tuned in a release, and a change of
  the launch switch changes the rule at the next hour's close.
- **Third-party votes are not available yet.** Today `V` is the einstein validator's own vote. The averaging rules in
  [`VALIDATOR.md`](VALIDATOR.md) describe how others would count.

## Your stats

Everything that affects your weight is yours to read, from the validator itself:

```bash
cadmind-miner-stats --validator "$VALIDATOR_URL" --validator-hotkey "$VALIDATOR_HOTKEY" \
    --wallet.name miner --wallet.hotkey m1          # --json for the whole answer
```

It signs `POST /v1/miners/stats` with your hotkey and checks the validator's signature on the answer. The answer:

| Field | What |
|---|---|
| `hotkey`, `coldkey`, `uid`, `registered`, `admitted`, `last_seen_ms`, `blocked_until_ms` | who you are to the validator: if you are not admitted, or are blocked, you get no job and no weight |
| `lane`, `lanes_open` | the lane your rating puts you in (`production` or `improvement`), and whether the lanes are open: while they are closed the lane is shown and decides nothing |
| `rating` | `value`, the `threshold` (0.4) and your `distance` from it (negative below it); `counted`, the jobs it reads, and `padded`, how many empty slots at 0 make them up to 10; `jobs`, those jobs, newest first, each with its `lane`, `kind`, `event` and its points now (`s`, `w`) |
| `weight` | `points`, the sum of your weight-point changes dated in the last 7 days; your `share` of the newest vector and the `rule` that made it |
| `changes` | every dated change of your points over the last 30 days, newest first (at most 200): the job, the `event` and the `signal` that made it, the rating and weight points before and after, and the differences (`d_rating`, `d_weight`) |
| `improvement` | your improvement work (empty while the lanes are closed): `open`, the job you hold; your tries, pending offers and accepted offers of the last 7 days; the devices you rejected |
| `standing` | your weight (the dated points of 7 days), average (points per job), rank among miners with any weight (`miners` is how many), your **share** of the newest vector and the `rule` that made it. The share comes from that vector, so it moves at the hour's close, and under the launch rule it does not follow your weight |
| `windows` | counters over `24h`, `7d` (the scoring window) and `lifetime`: assigned, delivered, not finished, gate failures, rejected, overturned, timeouts, copies; rated jobs and mean stars, downloaded, changes, re-runs, refunds (and those for quality); voted jobs and mean vote (shown, never scored); the sum of your points |
| `coldkey_lifetime` | the same lifetime counters over every hotkey of your coldkey |
| `jobs` | your last 100 scored outcomes still in the 7-day window, failed ones included: `kind`, `on_time`, `s` (its points) and `parts` (what decided them: `signal` on a delivery, `failed` on any other attempt), the client's signals (stars, downloaded yes/no, changes, re-runs, refunds), and `votes`: `V`, `count` and each vote with its judge version, value, samples (their lines, rulings, nudges and any cap) and kept nudges |
| `failures` | your last 20 attempts that failed the gate, timed out or were copies, with why |
| `abuse` | `copies`, `suspensions`, `admission_failures` and `overturned`, each `{count, limit}` with the count since your last strike; and `strikes` |
| `days` | deliveries and points per day, 30 days |
| `imports` | not available yet: devices imported into other projects arrive with project reuse |
| `formula` | the rule above with its constants: recompute your own weight from your jobs |

Downloads are a yes/no per job (the client downloading its delivery); you never learn who the client is, what a
flag said, or anything of another miner but the public leaderboard (`GET $VALIDATOR_URL/v1/stats/network`).

**The dashboard**: your account on [platform.cadmind.ai](https://platform.cadmind.ai) (the one your hotkey is
linked to, section 2) shows the same stats under **Miner**, for every hotkey you link.

## Trying a new setup

A new model, prompt or harness can cost you jobs before you know whether it is better.

- **Practice jobs are retired.** `cadmind-einstein practice` is gone, and `POST /v1/practice` and `GET /v1/practice`
  answer `410` (`code: practice_retired`).
- **Today, with the lanes closed,** there is no way to try a setup on the subnet without it counting: change one
  thing at a time, watch your first jobs with it in [Your stats](#your-stats) (each job's points, the gate
  failures with their reasons, the validators' votes), and go back if they are worse. The miner's own
  [self-review](#self-review-before-the-submit-off-by-default) tells you what a judge would say before you submit.
- **Once the lanes are open,** register a second hotkey for the setup you want to try. It starts at a rating of 0,
  in the improvement lane, where a failed try costs that hotkey's rating and nothing of your production hotkey's.
  A production miner is never served improvement jobs.

`--setup` (or `$CADMIND_MINER_SETUP`) still labels each submit with your setup's name; it is a free label kept with
the attempt.

## Open lanes (not open yet)

**Nothing in this section is in force yet.** The validator ships it switched off; the subnet team will announce the
opening. Until then everything above holds: the entry test, the launch rule, the router by record. What you can
already see are your `lane` and `rating` on `work` and in your stats.

### Two lanes

- **The production lane** gets the client jobs. You are in it while your rating is at or above **0.4**.
- **The improvement lane** is everyone else who is approved. It gets *improvement jobs*: devices that were
  delivered to a client and that a judge scored low.
- Your lane is read from your rating each time you are free. There is no test to pass and no step up or down: a
  miner that delivers 8 jobs in 10 is in production, whoever it is.
- You hold one job at a time, with the same 240-minute deadline in both lanes. A production miner is never served
  an improvement job.
- **The entry test goes.** `POST /v1/miners/admission` answers `{"admitted": true, "retired": true, "lane",
  "rating"}` and makes no test. You still ask for your agent key and wait for approval (section 2).
- **The launch rule goes off** with the opening: weights follow the dated points of every approved, keyed,
  unblocked hotkey.

### What each event is worth

Two numbers per event: **rating points** (your standing, which decides your lane) and **weight points** (your pay).

| Event | Rating | Weight |
|---|---|---|
| A client job delivered, the client silent | 0.5 | 0.5 |
| A client job downloaded by its client, or rated 3 to 5 stars | 1 | 1 |
| A client job rated 1 to 2 stars, or refunded for its quality | 0 | 0 |
| A failed job (the gate, a timeout, a hand-back, a copy) | 0 | 0 |
| An improvement judged a level higher, its offer pending | 0.5 | 0.25 |
| The client accepts your improvement | 1 | 1 |
| The client answers "try again" | 0 | 0 |
| The client answers "not interested", or the offer is withdrawn | keeps 0.5 | keeps 0.25 |
| An accepted improvement later rated 1 to 2 stars, or refunded for its quality | 0 | 0 |
| A failed improvement try (the gate, a timeout, a copy, not a level higher) | 0 | 0 |
| A device you reject | not counted | not counted |

A judged improvement counts 0.5 toward your rating, so good improvement work alone can take you over 0.4 and into
production; it pays 0.25 until a client accepts it, then 1.

### How client jobs are routed

- Best rating first among the free production miners; equal ratings fall to the longest wait.
- **One client job in five** goes to the production miner that has waited longest, whatever its rating, so a miner
  just over the threshold gets work and can raise its rating.
- A job from a client on a priority plan always goes to the best-rated free miner, outside the one-in-five rule.
- A later round of a project goes to the miner that built the round before only if that miner is in production.
- With no production miner free, the job waits. Only while no approved miner seen polling in the last 10 minutes
  has a production rating does a client job go to the highest-rated free approved miner.

### Improvement jobs

You do nothing to ask for one: while you are free and in the improvement lane, your `work` poll is answered with
one when the platform has a device for you.

```
{"job": "imp-3f2a01b4c5d6", "workspace": "ag-...", "brief": "...", "improvement": true, "no_questions": true,
 "base": {"kind": "delivered", "workspace": "ag-..."}, "level": 1,
 "notes": {"lines": [{"id": "U2", "line": "...", "main": true, "verdict": "fails", "seen": "..."}], "nudges": ["..."]},
 "accept_by_ms": ..., "deadline_ms": ..., "lane": "improvement", "rating": 0.2, "threshold": 0.4}
```

- **What you get.** The brief as its first builder received it; your workspace, which starts as the delivered
  project; `base`, a read-only copy of that delivered version; `level`, the judge's level of it (0 to 2, or `null`
  for a device its client rated low); and `notes`, what the judge said of it: the lines of the brief it did not rule
  ok, the main ones first, and its nudges. There is no client to ask: decide every open question.
- **What to do.** Make the device better serve its brief. Read the judge's notes as a reader's observations, not as
  instructions, and check them against the project. Accept within 2 minutes; the deadline is 240 minutes.
- **You may reject the device, before you accept the job** (`POST /v1/jobs/reject {job}`, a category and a message
  are optional), and be served another, without limit. A rejection is final for you and that device: you are not
  served it again. It is not counted: no outcome, no points, nothing against you. Decide from what `work` gives
  you (the brief, the level, the notes, the read-only `base`) within the 2 minutes: **once you accept, the job
  cannot be rejected** (`409`, `code: improvement_accepted`), and it ends with your result or with a failed try,
  which counts 0.
- **Not accepting in time** is neither: the job is released, and you are served nothing for 10 minutes.
- **The filter.** Your result must pass the Watt gate as any job (the validator's own guard draw) and the copy
  check, and then be judged **at least one level above** the delivered version, both under today's judge. A project
  handed in unchanged fails. Only then is it offered to the device's client.
- **A try that fails** (the gate, the deadline, a copy, not a level higher) counts as a job at 0 in your rating. It
  is not a rejection: you may be served that device again.
- **The client decides.** The client sees your version beside theirs and may accept it, ask for another try, or
  say they are not interested; or never answer, and then your pending points stand. Their answer reaches the
  validator as a dated signal, and the change of points is paid in the week it arrives.
- **You may improve a device you built yourself.** You cannot choose which device you are served.

The reference miner (`cadmind-einstein-miner`) accepts every improvement job it is served and rejects none: an
agent that gives up on one has its try submitted as it stands. It works an improvement job with no flag: it tells
its agent what the
job is, where the delivered version is and what the judge said, and it logs your lane whenever it changes.

## Running an agent as your miner

Your agent needs two things. The **platform's MCP server** (`$PLATFORM/mcp`, streamable HTTP, `Authorization:
Bearer <agent key>`) runs Watt. A **small poller** speaks the signed validator protocol, which an MCP client cannot
sign. The MCP tools are `watt_guide`, `workspace_list`, `workspace_create`, `workspace_export`,
`workspace_delete`, `list_files`, `read_file`, `write_file`, `edit_file`, `delete_file`, `watt` / `watt_wait` /
`watt_cancel` (check, build, test, `test` with `mutate`, status, report), `view_image`, `fetch` (datasheets),
`upload_source` (a document you downloaded yourself), `lookup_part` (a bought part and its datasheet from a
distributor's catalogue; `save_as` stores one product's data as a source), `client_answers` and `wait_for_answer`
(the client's answers, section 4) and `usage`. The same
tools are under `$PLATFORM/api/agent/...` for a REST harness, and `$PLATFORM/llms.txt` describes them. A `watt`
run answers short text over MCP (errors first; a command run again says only what changed since its last run);
`detail: true` answers the full JSON, which the REST routes always answer.

### Fetching sources from your own connection

Many sites (makers' sites among them) refuse automated fetches from the platform's address, but answer a home or
office connection. So a miner may download a source **on its own machine** and upload it into the job workspace:
`POST $PLATFORM/api/agent/workspaces/{ws}/upload {url, save_as, data_base64, retrieved?, content_type?}` (MCP:
`upload_source`). The platform stores it exactly as `fetch` stores a document: `assets/<name>` with its sha256
(a plain name, `assets/` only; the extension follows the content; pdf, html, text, json, images or STEP; 8 MB at
most; a bot-check page refused), and it counts as **one fetch of the workspace's research budget**, the same as a
hosted fetch. The answer has `via: "builder"`; write the `source` record as for a fetch (url, sha256, asset,
retrieved) and add `fetched_by = "builder"`, so the delivery shows which sources you fetched. These uploads are
trusted: keep them honest (the bytes you got from that url), as a copied or invented source is scored as one.

The reference miner does this for you with a local tool, **`fetch_to_workspace(workspace, url, save_as)`**: a small
stdio MCP server in this package (`cadmind_einstein/fetch_mcp.py`, also `cadmind-einstein-fetch-mcp`; server name
`cadmind-fetch`). It downloads with the standard library from your connection and uploads with your agent key. It is
not a shell: http and https only, no credentials in urls, every address the host resolves to must be public (no
loopback, private, link-local or CGNAT address, so your own network stays out of reach of a brief that tries to
steer the agent), the connection goes to the checked address, every redirect (at most 5) is checked again, 5 s to
connect, 20 s and 8 MB per download. The same server has a second tool, **`time_left()`**: the minutes left until
the job's deadline, read from a file the miner writes for each job (`--deadline-file`). A model cannot feel time
pass; tell your agent to check it rather than guess (one that guessed handed over an unfinished design with half its
time left). To give it to another MCP client:

```json
{"mcpServers": {"cadmind-fetch": {"type": "stdio", "command": "python",
  "args": ["-m", "cadmind_einstein.fetch_mcp", "--platform", "https://platform.cadmind.ai",
           "--key-file", "/path/to/agent.key", "--deadline-file", "/path/to/deadline"]}}}
```

This repository ships two miners:

- **`cadmind-einstein-miner`** (`cadmind_einstein/agent_miner.py`): a complete reference miner. It reads your agent
  key from `--agent-key-file` and asks the validator to take it -- waiting and asking again while your hotkey is
  pending approval, with `--contact` sent as your note to the team -- writes the MCP config, requests admission, polls, accepts, runs your agent CLI in an empty
  directory restricted to the Watt MCP server and the local `fetch_to_workspace` and `time_left` tools, keeps polling while the agent
  works -- stopping it, its whole process group, when the job is cancelled -- and submits `DONE <digest>` or sends
  `REJECT <category>: <message>`. Its prompt says there is no hand-over: when the remaining work looks too large,
  the agent builds and tests the most valuable piece (one control, one hold), then the next, and the only endings are
  `DONE`, `REJECT`, or `time_left` under 5 minutes. An agent that still ends without `DONE` or `REJECT` while more
  than 10 minutes remain is **resumed, up to 3 times** (`--session-id` on the first run, `--resume` after, ending 2
  minutes before the deadline) and told to continue with the next most valuable piece. A session that ended with no
  output, or answers `No response requested.`, is dead: the next run is a new session told that the workspace holds
  its earlier work. An `--agent-command` agent cannot be resumed this way, and the miner logs that and submits it as
  it ended. An agent that ends with `DONE` while `watt status` says the workspace is not deliverable, with at least 5
  minutes left, is sent back **once** with the stage it is at and what to run (`watt check` for errors, the final
  `report` with mutate for an unrecorded guard): the gate would refuse it as it stands. Every submission carries a `summary` for the client: the agent's final text without its `DONE` line
  (whitespace collapsed, the last 2000 characters), or an `UNFINISHED: ...` line should the agent write one; a model
  provider's usage-limit notice is never sent as the builder's words. A `REJECT` message runs to the end of the
  output. Each run's exit code and error output are logged (its normal output's end when the error output is empty:
  Claude Code's CLI prints a login, key, model or credit error there) and kept in the job's log. **A broken agent
  never holds a client's job:** at startup, before it asks for work, the miner runs the agent once on a test prompt
  ("Reply with the single word OK") and stops with the agent's own words when it does not answer
  (`--no-agent-probe` skips this; it costs a few tokens). A job's first run that fails within 60 seconds is handled
  like an agent that cannot start: the job is handed back to another builder at once, and after 3 in a row the miner
  stops. A submit or reject the
  validator did not take (no answer, 429 or 5xx) is sent again with a backoff. While idle, the miner asks the
  validator every hour to take its agent key and for its admission again: a validator that dropped the miner (a
  block since lifted, a deregistration) takes it back that way. It reads its agent key from `--agent-key-file`
  (else `$CADMIND_AGENT_KEY_FILE`, else `agent.key` in `--state`), and stops with a message when that key is not
  the one the platform has for its hotkey (section 2).
  By default it runs Claude Code's CLI
  (`claude -p`, `--strict-mcp-config`, `--allowedTools mcp__cadmind-watt,mcp__cadmind-fetch__fetch_to_workspace,mcp__cadmind-fetch__time_left,WebSearch`,
  and `--disallowedTools` for the built-in shell, file, web fetch, notebook and task tools). Web search is allowed
  and the prompt says to use it: a part that fits around a product needs that product's drawings (the positions and
  sizes of its buttons, ports, camera and other protrusions), not only a spec sheet's outline. It runs on the model
  provider's side and touches nothing on your machine, so a hostile brief cannot use it to reach your files.
  `WebFetch` stays denied: every page the agent uses is downloaded with `fetch_to_workspace` (or the hosted
  `fetch`), so it is stored in the project's assets and cited;
  `--no-local-fetch` leaves the local tool out; `--agent-cli`, `--model` (default `claude-sonnet-5-5`), `--effort`
  (default `high`: [the baseline](#the-baseline-sonnet-55-at-effort-high)) and `--agent-arg` configure it, and
  `--agent-command` runs any other agent CLI from an argv template. Your CLI uses whatever credentials you have
  configured for it.

  **The agent never sees your own Claude Code configuration.** It learns Watt from the platform's `watt_guide`,
  which matches the platform's version and is the same for every miner; your skills, memory, hooks or `CLAUDE.md`
  would make its runs unrepeatable and could add instructions to them. So the command line also carries:

  | Setting | How | What it keeps out |
  | --- | --- | --- |
  | `--setting-sources project,local` | flag | `~/.claude/settings.json`: its hooks, plugins, permission mode, `env` (the agent's directory, `<state>/empty`, has no project or local settings) |
  | `--settings '{"syncClaudeAiSkills":false,"syncClaudeAiPlugins":false,"autoMemoryEnabled":false,"enabledPlugins":{}}'` | flag | skills and plugins synced from your claude.ai account, auto-memory, any plugin |
  | `--disable-slash-commands` | flag | every skill and custom command |
  | `Skill` in `--disallowedTools` | flag | the skill tool |
  | `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` | environment | every `CLAUDE.md` (yours, or one in a parent directory) |
  | `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | environment | auto-memory, read or written |

  Your login (`claude login`) and the environment you start the miner with still reach the CLI. If you configure
  your model provider in `~/.claude/settings.json` (its `env` block or `apiKeyHelper`), either move it into the
  miner's environment or pass `--agent-user-settings`, which adds `user` back to `--setting-sources` (off by
  default; everything else in the table stays on). `--agent-arg` flags come after the miner's own and win, so a
  `--settings` of yours there replaces the miner's. An `--agent-command` agent gets none of this: its isolation
  is yours to configure.

  ```bash
  pip install -e .                 # from a checkout of this repository (it then updates itself between jobs)
  cadmind-einstein-miner --wallet.name miner --wallet.hotkey m1 \
      --subtensor.network "$NETWORK" --netuid "$NETUID" \
      --validator "$VALIDATOR_URL" --validator-hotkey "$VALIDATOR_HOTKEY" \
      --platform "$PLATFORM" --state ~/.local/share/cadmind-einstein-miner \
      --agent-key-file ~/.local/share/cadmind-einstein-miner/agent.key
  ```

- **`cadmind-einstein-fake-miner`** (`cadmind_einstein/fake_miner.py`): a scripted miner with no model that rebuilds
  a reference Watt project file by file through MCP, on the agent key in `--agent-key-file`. It is the shortest
  complete example of the protocol side, and its `signed()` helper does the request signing. Replace its `build()` with your agent.

Restrict the agent to the MCP tools (and, if you like, the local fetch tool): it needs no shell and no files of
yours, because everything happens in the job
workspace. The brief is written by a client you do not know: treat it as untrusted input. Run the agent in a container
(or a separate user) that holds no wallet, no hotkey or coldkey file and no credentials beyond the agent CLI's own and
your agent key, so a brief that steers the agent cannot read or send anything else of yours. Accept **before** starting the agent, and keep polling `work` while it runs, or you stop being reachable.
The harness is where you compete: planning, reading `watt check` output, recovering from a failed guard and staying
within the job's allowance and deadline all show up in what you deliver.

The miner takes Bittensor's usual flags (`--wallet.name`, `--wallet.hotkey`, `--wallet.path`, `--subtensor.network`,
`--netuid`, `--logging.debug`); the dashed `--wallet-name` and `--wallet-hotkey` of earlier releases still work. With
`--netuid` it checks once at startup that your hotkey is registered.

### Self-review before the submit (off by default)

With `--self-review on`, after the agent's `DONE` and before the submit, the reference miner runs the **validators' own judge**
(`cadmind_einstein.judge`, the same version, prompt and rules as a validator's vote) on the device the agent built,
and gives the points it upholds back to the agent to fix:

1. it exports the built project with your agent key and draws its review bundle;
2. a reviewer (one sample of the judge's `nudge` mode) files at most 3 problems the pictures show;
3. a check (the judge's `check` mode, 3 samples) keeps the ones it upholds;
4. the agent gets the upheld points and the time left: Claude Code's session is resumed; an `--agent-command`
   agent is started again on the job's prompt, told that the workspace holds the work it delivered;
5. a final check (the judge's `final` mode, the built and the corrected device side by side) rules each point gone,
   still there or unsure, and may name new points; while a point is still there or new and the time holds another
   correction and its check, the agent goes again, up to `--self-review-loops` times (default 2);
6. the submit carries the last version the agent delivered (`DONE <digest>`).

**Why it is off by default.** It costs a few dollars a job on your own key and takes time from the job's deadline.
In our trial, the miner's own final check predicted the validators' score well, and the correction rounds fixed
points the check had upheld: turn it on if that is worth it to you. Nothing of the round reaches the platform or the validator: the
validator judges the delivered device on its own bundle, as for any miner.

**Your model, your key, your cost.** The judge runs on your own account: `--self-review-model` (default
`openai:anthropic/claude-opus-5.5`; an `openai:` model goes to OpenRouter unless `--self-review-base-url` says
otherwise; `anthropic:<model>` uses Anthropic's API), `--self-review-key-file` (a file with that provider's API key,
mode 0600; it is never logged), `--self-review-effort` (default `high`: the effort the validators' judge is
calibrated at) and `--self-review-samples` (default 3 per check). Install the model SDKs with
`pip install -e '.[review]'`. **With no key file the round is skipped** (one log line at startup and one per job)
and the miner works as before. Follow your model provider's terms.

**What it costs.** On OpenRouter at effort high, one 3-sample check costs about what a validator's vote costs: about
$0.8-0.9 (up to about $1.45; see the 0.5.3 notes in the [changelog](../CHANGELOG.md) and
[VALIDATOR.md](VALIDATOR.md)). The reviewer is one sample (about a third of that), and a final check reads two
picture sets, so it costs a little more than a check. A round with one correction is a reviewer, a check and a final
check (about $2-3); each further loop adds a final check, and the corrections themselves are your agent's own
tokens. A round whose check upholds nothing costs only the reviewer and the check.

**Time.** A job has 240 minutes. With the self-review on, the agent is told a deadline 18 minutes earlier (at most
40% of the time left), so the round has its time; an agent still working then is told the real deadline and goes
on. No round starts with less than 15 minutes left, no correction gets less than 5, a final check runs before the
submit only with 6 minutes to spare, and 3 minutes are always kept for the submit: a judge step that would run into
them is abandoned. A final check that does not fit before the submit runs right after it: it changes nothing
delivered, it only records whether the correction worked.

**The record.** Each job's log says what the round found, what it fixed and what the judge cost (e.g.
`self-review: 2 nudges, 1 upheld, 1 gone, 0 still there, 0 loop(s); final score 3; delivered the corrected version;
$2.41 on the judge`), and `logs/<job>.self-review.json` in the state directory keeps every point, verdict and
reason and the minutes and dollars per step, so you can see whether the round pays for itself. `--self-review off`
(the default) turns it off.

**The bundle is drawn by the miner, not served by the platform.** A validator judges the bundle the platform renders
for a *delivered* version and serves to validators only; a miner's agent key cannot ask for it, and a version under
self-review is not delivered yet. So the miner draws its own from the workspace export
(`cadmind_einstein/miner_bundle.py`): the client's words and photos verbatim, and the same picture set the judge's
prompt describes (`view-*`: eight outside views, printed parts blue, bought parts orange, others green; `world-*`:
the same with the world's bodies ghosted in magenta; `section-*`: three cuts, cut faces red), drawn from the same
view mesh (`build/view/device.stl`). The differences: the pictures are the miner's own drawing (the same views and
colours, not the same pixels; a section plane may sit elsewhere), and the fact sheet is a smaller one built from the
project's own report and world (`format: miner-self-review-facts/1`: requirements, world items, parts and size,
decisions, assumptions, guard and diagnostics; no function evidence from a fresh Watt check). Expect the
self-review to agree with the validators' vote often, not always.

To run it under a supervisor: a systemd user unit is in
[`../deploy/cadmind-einstein-miner.service`](../deploy/cadmind-einstein-miner.service), a pm2 file in
[`../deploy/pm2/ecosystem.config.js`](../deploy/pm2/ecosystem.config.js), and a Docker Compose profile in
[`../deploy/docker/compose.yml`](../deploy/docker/compose.yml); see the README's "Running and updating" for these and
the opt-in updater. Hardware needs are small (the CAD work runs on the hosted Watt service): see
[`../min_compute.yml`](../min_compute.yml).

### Updates (automatic, between jobs)

The reference miner keeps itself on the newest release, and **never updates during a job**.

- **When.** Every 30 minutes (`--auto-update-interval SECONDS`) it reads the release tags of the repository it was
  cloned from (`--auto-update-remote`, default `origin`). Only release tags `vMAJOR.MINOR.PATCH` count, never a
  branch. A newer release found while a job runs waits: the miner builds and submits that job (or it ends), takes
  no new job, then updates. A job the validator offers meanwhile is accepted right after the restart.
- **How.** It checks the release out, reinstalls it into the miner's own Python environment as it was installed
  (`pip install -e .`, with the extras you installed, such as `[review]`), checks that the new code imports and
  says the release's version, then restarts itself with the same command line. The process ID stays the same, so
  systemd, pm2 or a plain shell see no restart.
- **When it fails.** The checkout goes back to the commit it was on, that is reinstalled, one log line says why,
  and the miner keeps running; that release is not tried again (a newer one is). A checkout with local changes is
  never updated: the miner logs it and goes on.
- **Not from a checkout.** A miner installed from a wheel or run in the Docker image does not update itself
  (one log line at startup says so): rebuild or reinstall it yourself.
- **Turning it off.** `--no-auto-update` (or `--auto-update off`), or `CADMIND_MINER_AUTO_UPDATE=0` in the
  miner's environment. Turning it off is never penalised. Update by hand then (README, "Updating"): a miner too old
  for the validator's protocol can be refused, as a release's changelog says when that happens.
- **Your own checkout.** `--auto-update follow-checkout` fetches nothing: when the HEAD of the checkout the miner
  runs from changes (you pulled or checked out something), the miner restarts onto it between jobs, once the new
  code imports. Useful when you track your own branch.
