# alphamachine

A diversity-first alpha research pipeline for WorldQuant BRAIN, built around
the way BRAIN actually scores consultants rather than around raw backtest hunting.

---

## Part 1 — The rules of the game (researched)

### How an alpha is counted

**Fitness** is the platform's composite quality metric:

```
Fitness = Sharpe × √( |Returns| / max(Turnover, 0.125) )
```

**The `max(·, 0.125)` clamp is the single most exploitable fact on the platform.**
Below 12.5% turnover you earn *zero* additional fitness from trading less. Most
people over-decay their alphas into the 3–5% turnover range, paying real returns
for a fitness credit that does not exist. The machine explicitly targets the
**12.5% knee** (`config.FITNESS_TURNOVER_KNEE`) and stops there.

### Submission gates — CONSULTANT, non-CHN

> **`docs/BRAIN_RULES.md` is the authority for every number here**, transcribed
> from the official documents. Where a web-sourced figure disagrees with it, the
> web is wrong. It also keeps a list of debunked figures, because several of
> them are widely repeated and each one costs real submission slots.

| Check | D1 | D0 |
|---|---|---|
| `LOW_SHARPE` | **> 1.58** | > 2.69 |
| `LOW_FITNESS` | > 1.0 | > 1.5 |
| `LOW_TURNOVER` | ≥ 0.01 | ≥ 0.01 |
| `HIGH_TURNOVER` | ≤ 0.70 | ≤ 0.70 |
| `MAX_STOCK_WEIGHT` | < 0.10 | < 0.10 |
| `SELF_CORRELATION` | < 0.70 | < 0.70 |
| `PROD_CORRELATION` | < 0.70 | < 0.70 |

**The most expensive myth on the internet is `LOW_SHARPE ≥ 1.25`.** That is the
**user-level Challenge** bar. A consultant D1 alpha needs **1.58**, so a gate
built on 1.25 cheerfully forwards alphas the platform will reject — spending an
irreversible submission slot to be told no. CHN runs a much harder ladder
(D1 Sharpe ≥ 2.08 *and* returns ≥ 8%); see `config.CHN_CHECKS`.

`LOW_SUB_UNIVERSE_SHARPE` is a **formula, not a constant** — for a `TOPXXX`
universe it is `0.75 × √(sub_size / universe_size) × alpha_sharpe`
(`config.subuniverse_floor`). `IS_LADDER_SHARPE` is likewise a per-year-count
ladder, and `CONCENTRATED_WEIGHT` / `LOW_DURATION` / `MATCHES_COMPETITION` are
pass/fail. Reported failure distribution: **`LOW_SHARPE` ~90% of rejections.**
Pass rates by data type: fundamental ~40% ≫ mixed ~13% ≫ pure technical ~5%.

### How consultant score is computed

Individual alphas are scored by a **non-public formula** weighting Sharpe,
turnover and fitness — but the part that matters for strategy is the
aggregation: **scores are combined such that baskets of strong *and
uncorrelated* alphas are rewarded.** Entry to the program is 10,000 points +
Gold; Master tier is ~$2,000/quarter, Grandmaster ~$8,000+/quarter.

**This is the whole strategic point.** Your score is a function of a *portfolio*,
not a sum over alphas. The 20th alpha in a correlated cluster is worth
approximately nothing. A mediocre orthogonal alpha is worth more than an
excellent redundant one.

### Submission cadence: days matter more than volume

Three rules that between them dictate the whole release schedule:

* only the **first 4 alphas per day** (+1 SuperAlpha) count toward base payment;
* quarterly payment requires submissions on **≥ 20 distinct days** in the quarter;
* **fewer than 20 submitting days in a rolling 3 months forces Value Factor to
  its minimum** — the single largest lever on quarterly pay.

So the optimal policy is **submit every day, 1–4 per day**, never bank a batch
and dump it. `Submitter.release_plan()` spreads a ranked basket across days for
exactly this reason. (There is no "2,000 points/day cap" — that belongs to the
user Challenge, not this programme.) Two more inversions worth knowing, because
they run opposite to Challenge intuition: **a larger universe is better** for a
consultant (TOP3000 over TOP200), and **D0 and D1 have been worth the same
since 2023** — there is no reason to contort a strategy for delay.

### API surface

| Endpoint | Notes |
|---|---|
| `POST /authentication` | HTTP Basic → 201, sets JWT cookie. May demand a browser biometric/persona check. |
| `POST /simulations` | dict = single; **list = multi-sim bundle** (tier-gated). Returns `Location` header. |
| `GET {location}` | Poll. Returns `Retry-After` while running; then `alpha` id or `children`. |
| `GET /alphas/{id}` | `is.{sharpe,fitness,turnover,returns,drawdown,margin,longCount,shortCount,checks}` |
| `GET /alphas/{id}/recordsets/pnl` · `/yearly-stats` | daily PnL curve, per-year stats. **Computed lazily** — see below. |
| `GET /alphas/{id}/correlations/self` · `/prod` | correlation vs your book / production |
| `GET /alphas/{id}/check` | authoritative pre-submission check |
| `POST /alphas/{id}/submit` | **201 ≠ live.** Re-read the alpha; confirm `status == ACTIVE`. |
| `GET /data-sets` · `/data-fields` · `/operators` | catalog, paginated |
| `GET /users/self/alphas` | your existing book |

Concurrency is your **account's simulation-slot count** — **confirmed 3** for
this account, which is the default. Setting worker count above your real slot
count only builds a server-side queue.

**Several endpoints compute lazily and lie convincingly while they do.**
`/check` and the correlation endpoints answer `204` or send `Retry-After`, which
is obvious enough. The recordsets (`pnl`, `yearly-stats`) are worse: they can
return **`200` with an empty `records` list** for an alpha that has plenty of
both, moments before returning the real thing. A bare `GET` therefore reports
"this alpha has no PnL", which upstream reads as a genuine absence and silently
drops a good candidate. `client._recordset()` retries a bounded number of times
rather than trusting the first empty answer.

---

## Part 2 — Design thesis

Most public BRAIN pipelines are *grid searchers*: fields × templates × settings,
run overnight, sort by Sharpe. That design optimises the wrong objective and
fails in three specific ways.

**1. It burns the scarce resource on the wrong axis.** Simulation slots are the
binding constraint. A uniform grid spends as much budget on arms that convert at
1% as on arms that convert at 15%.

**2. It manufactures correlated alphas by construction.** Two *level* signals on
different fundamental fields correlate 0.7–0.85 — they are all "cheap vs
expensive". A *level* and a *change* signal on the **same** field correlate
0.2–0.4. Grid search varies fields and windows, i.e. it varies exactly the
dimension that does *not* buy decorrelation. Then the basket dies at
`SELF_CORRELATION` after the slots are already spent.

**3. It submits at the floor.** An alpha at Sharpe 1.26 in sample is a coin flip
out of sample. A book of coin flips scores badly and cannot be un-submitted.

### The three principles

> **① Transform-first, field-second.** Diversity comes from the *shape of the
> hypothesis*, not the identity of the data. `templates.py` encodes ten distinct
> transform families — LEVEL, CHANGE, SURPRISE, ACCELERATION, STABILITY,
> RELATIVE, YIELD, RESIDUAL, CONSISTENCY, EVENT — each a different economic claim.

> **② Diagnose, don't sweep.** A survivor's failing metric tells you which knob
> to turn. Reading the failure and turning that one knob costs ~20 sims; a full
> grid costs ~500 for the same answer.

> **③ Gate on headroom, not on the bar.** Rank by the *weakest-link margin over
> every gate simultaneously*. Raw Sharpe happily selects a 60%-turnover alpha
> that can never pass `HIGH_TURNOVER`.

---

## Part 3 — The pipeline

```
   ┌─ catalog ──────────────────────────────────────────────────┐
   │  /data-sets + /data-fields → theme tags → coverage filter   │
   │  → drop over-mined fields → stratified sample ACROSS        │
   │    datasets (breadth buys decorrelation, depth buys twins)  │
   └────────────────────────────┬────────────────────────────────┘
                                ▼
   ┌─ generate ─────────────────────────────────────────────────┐
   │  Thompson sampling over (theme × transform) arms            │
   │  reward = the FULL FUNNEL conversion rate, not Sharpe:      │
   │    P(scan) × P(gate | scan) × P(deep | gate)                │
   │  LLM priors seed cold arms · per-theme decay/neutralization │
   └────────────────────────────┬────────────────────────────────┘
                                ▼
   ┌─ scan ─── bundles of 10 per slot, one fixed cheap setting ──┐
   │  gate: |Sharpe| ≥ 0.65   (sign is not a hypothesis)         │
   │  kept deliberately low: today's best alpha scanned at 0.94  │
   └────────────────────────────┬────────────────────────────────┘
                                ▼
   ┌─ refine ── greedy coordinate ascent, ~20 sims per survivor ─┐
   │  0 SIGN         1 sim   −1.6 Sharpe is a 1.6 alpha backwards│
   │  1 NEUTRALIZE   5 sims  biggest single Sharpe lever         │
   │  2 ROBUSTNESS   3 sims  backfill/winsorize → CONCENTR_WEIGHT│
   │  3 TURNOVER     6 sims  decay aimed AT THE KNEE, hump,      │
   │                         trade_when                          │
   │  4 TRUNCATION   4 sims  drawdown, weight concentration      │
   │  5 PORTABILITY  3 sims  falsification: survive TOP1000?     │
   └────────────────────────────┬────────────────────────────────┘
                                ▼
   ┌─ metric gate ──── free: everything the sim record knows ────┐
   │  sharpe · fitness · turnover · sub-universe ratio           │
   │  · drawdown · margin (bps) · long/short balance · positions │
   └────────────────────────────┬────────────────────────────────┘
                                ▼
   ┌─ deep gate ── costs API calls, so only for metric-passers ──┐
   │  yearly consistency   one-lucky-year filter                 │
   │  /check               PROD_CORRELATION et al                │
   │  The sim record CANNOT answer either. Unknown != pass.      │
   └────────────────────────────┬────────────────────────────────┘
                                ▼
   ┌─ select ── greedy max-MARGINAL-value, not top-N ────────────┐
   │  value = quality × (1 − max_corr vs everything kept)        │
   │  DAILY RETURNS, date-aligned. Never cumulative PnL.         │
   └────────────────────────────┬────────────────────────────────┘
                                ▼
   ┌─ submit ───────────────────────────────────────────────────┐
   │  local gates → /check (only source of PROD_CORRELATION)     │
   │  → submit → RE-READ, confirm status == ACTIVE               │
   │  → write outcome back → bandit learns → loop                │
   └─────────────────────────────────────────────────────────────┘
```

### Design decisions worth defending

**Bandit reward is the submittable rate, not mean Sharpe.** Optimising Sharpe
steers the machine toward crowded factors that look great in sample and die at
the correlation gate. Optimising the submittable rate steers it toward what
actually converts *for your account, against your current book* — which drifts
as the book fills.

**"Submittable" is the whole funnel, and each stage keeps its own denominator.**

```
P(scan_pass) × P(gate_pass | scan_pass) × P(deep_pass | gate_pass)
```

A scan simulation and a refine champion are different events with different
conversion rates. Pooling them into one `trials` counter charges an arm for
simulations that never reached the gate, so **winning budget made an arm look
worse** — a feedback loop pointing the wrong way. The third stage exists because
a simulation record reports `SELF_CORRELATION` and `PROD_CORRELATION` as
`PENDING` with no value, and says nothing at all about a one-lucky-year equity
curve; both need extra API calls, and both routinely block alphas that cleared
every local metric. An arm that keeps producing them converts far worse than its
gate rate implies. Run `alphamachine report` to see which stage an arm dies at.

**Unknown is neither pass nor fail.** When a deep check cannot be fetched the
verdict is *inconclusive*, recorded as NULL. An unverified alpha is never
reported as submittable, and the bandit is never taught a loss that was really
our failed request.

**The pessimistic Beta prior (1, 6).** One lucky hit on a fresh arm must not
hijack the budget. It sits on the gate stage — the one it describes — because
stacking pessimism on all three stages would crush every arm toward zero and
degrade the allocation into noise.

**Selection value = quality × (1 − corr).** Because score aggregates over a
*basket*, the marginal alpha is worth its quality discounted by its redundancy.

**The metric gate is not the bar — measured, not assumed.** From one full day of
running: **1,385 simulations → 334 scan survivors → 14 cleared every local
metric → 0 submittable.** Every one of the 14 died at the deep gate, in two
clean clusters:

| Cluster | Blocked by |
|---|---|
| analyst / earnings-revision family | `PROD_CORRELATION` — the theme is already crowded in BRAIN's production pool |
| `inst20_ra_sv` family | one lucky year (18% of years positive, single year 71% of \|PnL\|), plus `CONCENTRATED_WEIGHT`, `IS_LADDER_SHARPE`, `LOW_DURATION` |

Two lessons. First, **a simulation record cannot tell you whether an alpha is
submittable** — it reports both correlation checks as `PENDING` with no value
and says nothing about the shape of the equity curve. Second, the
`PROD_CORRELATION` cluster is *structural*: no amount of refinement fixes "this
hypothesis is crowded". That is a signal to rotate region or theme, not to tune.

**Portability (rung 5) is falsification, not optimisation.** An alpha that dies
on TOP1000 is a liquidity artefact. Better to kill it than to have it pass in
sample and lose money live.

**Daily returns, always.** Every cumulative equity curve trends up, so two
independent alphas show 0.9+ correlation on cumulative series. This one mistake
is the most common cause of a locally-vetted basket failing `SELF_CORRELATION`.

**Our gates vs the platform's** — set to the actual consultant floor from
`docs/BRAIN_RULES.md` (the user's own official documents; see Part 5), not an
arbitrary margin on top of it. Where a stricter number IS backed by official
guidance (turnover, sub-universe ratio, margin) that guidance is kept:

| | platform (consultant D1) | machine |
|---|---|---|
| Sharpe | > 1.58 | **1.58** |
| Fitness | > 1.00 | **1.00** |
| Turnover | ≤ 0.70 | **≤ 0.30** (official Value Factor guidance) |
| Self-corr | < 0.70 | **< 0.55** |
| Prod-corr | < 0.70 | **< 0.60** |
| Sub-universe ratio | — | **≥ 0.70** (official guidance) |
| Drawdown | — | **≤ 0.12** |
| Years positive | — | **≥ 70%, no year > 50% of PnL** |

---

## Part 4 — Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # add WQ_EMAIL / WQ_PASSWORD
python3 scripts/smoke_test.py # 141 offline checks, no network, no quota

python3 -m alphamachine --concurrency 3 catalog
python3 -m alphamachine priors                                # one-off, ~$0.70
python3 -m alphamachine --concurrency 3 scan   --budget 300
python3 -m alphamachine --concurrency 3 refine --top 25
python3 -m alphamachine gatelog --passing                     # what can I submit?
python3 -m alphamachine --concurrency 3 select --n 10
python3 -m alphamachine --concurrency 3 submit --n 3          # dry run
python3 -m alphamachine --concurrency 3 submit --n 3 --live   # for real
python3 -m alphamachine report
```

`--concurrency` **must equal your real slot count** — confirmed **3** for this
account, which is already the default. `submit` is a dry run unless you pass
`--live`, and is capped at `max_submits_per_day` (default **4**, matching the
counted-alpha rule above).

**`gatelog --passing` is the answer to "what do I actually have that I can
submit?"** It recomputes every verdict on the spot against whatever `Gates` are
in `config.py` right now, rather than trusting a stored flag — because a stored
flag silently becomes a lie the moment a threshold moves, and this project has
already moved its Sharpe gate 1.50 → 1.80 → 1.58. Metric-passers then get the
deep verdict, which needs the network; `--offline` skips it and reports those
candidates as **UNVERIFIED** rather than quietly calling them submittable.
Plain `gatelog` prints the history, including *why* each candidate was rejected
and under which thresholds.

`priors` classifies every dataset with an LLM once (~$0.70, cached in
`data/cache/arm_priors.json`) so the bandit starts with a direction instead of
sampling blind. It never overrides evidence — the scores become small Beta
pseudo-counts on the gate stage, so a handful of real trials outvote them.

### Authentication

BRAIN periodically demands a browser biometric (persona) check. Two traps:

* The inquiry is bound to the **session that raised it**, not to your account.
  Completing the browser check against a dead session loops forever on fresh
  inquiry ids. Use `scripts/auth_wait.py`, which holds one session open.
* After the finalise call succeeds the session is **already authenticated**.
  Re-POSTing `/authentication` at that point restarts the challenge. Verify
  with `GET /users/self` instead.

### Rate limits

BRAIN advertises its remaining budget on **every** response:

```
RateLimit-Limit: 5      RateLimit-Remaining: 4      RateLimit-Reset: 1
X-RateLimit-Limit-Minute: 5     X-RateLimit-Remaining-Minute: 4
```

The client reads these and pauses when the budget is nearly spent, rather than
discovering the limit by getting 429'd — a 429 landing on a poll can cost an
in-flight simulation, and on some endpoints repeated 429s escalate to a much
longer lockout. Observed limit on `/authentication` is **5 requests/minute**;
other endpoints may differ, which is exactly why the client trusts the headers
instead of a hard-coded number.

`429 BIOMETRICS_THROTTLED` is a **different counter** — it exposes no
`Retry-After` and is not reset by the per-minute budget (you can be throttled
with 4/5 requests still available). It is never retried; it raises immediately.

**BRAIN allows only 25 biometric requests per day**, and this is the constraint
that shapes the whole auth design. Two things make it easy to blow:

* every `POST /authentication` that returns 401-persona **mints a new inquiry**,
  so a retry loop spends quota even when you were not asking for a face check;
* a poll loop spends 2 units per iteration (finalise + re-auth), so polling
  every 10s burns the entire day in about four minutes and earns a
  `429 BIOMETRICS_THROTTLED` lockout with no `Retry-After`.

So `scripts/auth_wait.py` spends **exactly 2 units per attempt**: one to raise
the inquiry, one to finalise it. Waiting happens on a local sentinel file
(`touch data/persona_done`), never on the network. `BrainClient` additionally
enforces a client-side budget of **10 per rolling 24h** and raises rather than
requesting an 11th — a hard stop, not a warning.

A verified session's cookie jar is persisted to `data/cookies.json` (mode 600,
gitignored) and reused, so in normal operation you do the face check once and
never spend quota again.

### Suggested weekly cadence

| Day | Action | Budget |
|---|---|---|
| Mon | `catalog --refresh`, `scan --budget 400` on a **new dataset family** | ~400 sims |
| Tue–Wed | `refine --top 25` | ~500 sims |
| Thu | `select --n 10`, `submit --n 3` | — |
| Fri | `report` → read the arm table, prune dead arms | — |

Rotate the **region** every few weeks (`--region EUR/ASI/CHN`). Region rotation
is the cheapest decorrelation available: the same hypothesis in a different
market is a genuinely different alpha, and non-USA regions are far less mined.

### Throughput: a slot holds a *request*, not an alpha

`POST /simulations` accepts a JSON **list** of up to 10 payloads, and the whole
bundle occupies one slot. On a 3-slot account that is the difference between
**3 alphas in flight and 30** — the single largest throughput lever available,
and it costs nothing.

Bundling changes two things elsewhere in the machine. `children` come back in
payload order, so the child index is the only thing tying a result to the
expression that produced it; the `inflight` table stores it. And on shutdown
unfinished bundles are `DELETE`d rather than orphaned, which hands the slots
straight back instead of leaving them busy on work nobody will read.

### Polling: obey `Retry-After`, do not invent an interval

BRAIN returns `Retry-After` on every progress read — a positive value while the
job runs, and **`0` when it is finished**. Two failure modes follow from getting
this wrong: treat any `Retry-After` as "still running" and the poll loop hangs
forever; treat its absence as "done" and you read a half-built payload. Obeying
the server's own number is also what keeps polling inside the request-rate
budget without a hand-tuned interval.

### Crash resumption

Everything is designed so that killing the process is safe and re-running the
same command resumes:

| State | Where it lives | On restart |
|---|---|---|
| Simulation results | `sims` table, written **as each lands** | already-done candidates are skipped |
| Candidate identity | `sha1(expr + settings key)` | re-generating produces the same ids, so no duplicates |
| **In-flight simulations** | `inflight` table, URL stored **before** polling | `reclaim()` harvests them instead of re-paying |
| Field catalog | `data/cache/fields_*.json` | reused unless `--refresh` |
| Alpha PnL | `pnl` table | fetched once per alpha |
| Deep checks | `deep_checks` table | yearly kept forever; `/check` expires on TTL |
| Gate verdicts | `gate_history` table | append-only trail, with the thresholds in force at the time |
| Bandit evidence | `arms` table, per funnel stage | schema migrates in place on open |
| Session | `data/cookies.json` | reused; no repeat face check |

**The deep-check cache is a learning signal, never a submission decision.**
`yearly-stats` is in-sample data and immutable, so it is kept indefinitely.
`PROD_CORRELATION` drifts with your book *and* with BRAIN's production pool, so
it expires on a TTL — and `Submitter.server_check()` deliberately does not read
the cache at all. Submit always re-checks live.

The in-flight table is the one that actually costs money to get wrong. BRAIN
charges the slot the moment `POST /simulations` returns and finishes the job
whether or not anyone is listening. If the progress URL only ever lived in
memory, a crash mid-poll means the candidate still looks pending on restart and
you **pay for the same expression twice** — which on a 3-slot account is a real
tax. So the URL is persisted before the first poll, `pending()` excludes
anything in flight, and `scan`/`refine` call `reclaim()` before dispatching
anything new.

`report` shows the in-flight count so you can see when there is unharvested work.

### Where the quality actually comes from

1. **Rung 1 (neutralization)** — biggest Sharpe lever, 5 sims. Always run it.
2. **Rung 3 aimed at 12.5%** — most people decay past the knee and lose returns
   for nothing.
3. **The lucky-year filter** — cheapest overfitting filter that exists, and
   measured above as roughly half of all deep-gate rejections.
4. **Breadth across datasets** — the thing that keeps `SELF_CORRELATION` off you.
5. **Not submitting at the floor** — the 1.58-Sharpe alphas you *don't* submit
   are worth more than the ones you do.

A worked example, straight from the database — the whole thesis in four lines:

```
scan    sharpe 0.94  INDUSTRY,    raw field      <- a 1.0 bar would have killed it
 rung 1 neutralization -> SUBINDUSTRY
refine  sharpe 1.18
 rung 2 field is sparse -> wrap ts_backfill(..., 120)
gated   sharpe 1.73   fitness 1.57   turnover 0.040
```

**0.94 → 1.73 on two knobs**, found by reading which check was failing rather
than by sweeping. Then the deep gate rejected it anyway on `PROD_CORRELATION`:
the hypothesis is sound and already crowded. Both halves of that are the system
working.

---

## Part 5 — Caveats

**Two tiers of trust, and they are not the same.** The *thresholds and payment
rules* in `docs/BRAIN_RULES.md` are transcribed from official documents and are
authoritative — `config.py` defers to that file, and where a number in this
README disagrees with it, that file wins. The *endpoint paths, check names and
API mechanics* are community reverse-engineering, and BRAIN changes them.
Confirm against your own account: your slot count; whether multi-sim (a list
payload to `/simulations`) is enabled for your tier; and the check limits for
regions beyond non-CHN D1 (`config.CONSULTANT_CHECKS` / `CHN_CHECKS`).

**Field names in `templates.py` are placeholders.** `cap`, `assets`, `returns`,
`volume`, `adv20` exist in USA; the YIELD templates will error in regions where
the denominators are named differently. Run `catalog` first and check.

**`ts_regression(..., rettype=0)`** — confirm the rettype your BRAIN version uses
for residuals before relying on the RESIDUAL family.

**No auto-submit loop by design.** `submit` defaults to dry run and is
day-capped. Submissions are effectively irreversible; a human should look at the
basket. `submit --auto` exists but applies `AutoGates`, a whole tier stricter
than the manual gates (Sharpe 2.10 vs 1.58), and queues anything borderline for
review instead of sending it. The asymmetry is the point: a bad simulation
wastes a slot, a bad submission occupies book space and cannot be withdrawn.

**`--concurrency` above your slot count does nothing good.** It builds a
server-side queue and costs you the ability to react to results as they land.

**Biometric quota is the real operational constraint**, not simulation budget.
The client hard-stops at 10 requests per rolling 24h against a platform cap of
25. In steady state you complete one face check and the persisted session
carries you; the quota only becomes visible when a session lapses mid-run.

### Sources

- [Fitness / turnover / settings reference](https://github.com/alexisdpc/WorldQuant-alpha-trading)
- [Submission API automation](https://github.com/RussellDash332/WQ-Brain)
- [Check names, thresholds, correlation methodology, pass rates by data type](https://github.com/QuantML-Research/wq-alpha-research)
- [Submission process and quality filters](https://deepwiki.com/xiegengcai/world-quant-brain/5.1-alpha-submission-process)
- [API endpoints, concurrency, backoff](https://deepwiki.com/zhutoutoutousan/worldquant-miner/8.1-worldquant-brain-api)
- [Pipeline architecture prior art (IQC 2026)](https://github.com/angel4angelov-glitch/wq-alpha-pipeline)
- [Consultant program tiers and payment](https://worldquantbrain.com/consultant)
- [Scoring: fitness, aggregation, diversity reward](https://jglazar.github.io/projects/wq_project/)
- [Combinatorial alpha generation](https://quantjourney.substack.com/p/decoding-successful-alphas-the-combinatory)
