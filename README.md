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
| `LOW_2Y_SHARPE` | ≥ **1.58** (per-alpha) | ≥ 1.58 |

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

**Prefer the limit BRAIN reports over any local formula.** Every simulation
payload carries each check's own `limit` alongside its value, already scaled to
that alpha. Two of the numbers above are per-alpha in practice:
`LOW_SUB_UNIVERSE_SHARPE` scales with the alpha's Sharpe, and `LOW_2Y_SHARPE`
has been observed live at **2.02** on `/check` while every simulation-time
payload for the same alphas said 1.58. So `metrics.check_limit()` reads the
reported number and the `Gates` constant is only the fallback for a record
that predates the check.

**`LOW_2Y_SHARPE` is the one check that is not a whole-period aggregate**, and
that makes it the one worth singling out. Every other metric here is computed
over the full history, so a signal that earned Sharpe 9 in 2015–2016 and
nothing since still scores like a working one. A real example from this
database — full-period Sharpe **2.39**, trailing-two-year Sharpe **0.48**:

```
year   2014  2015  2016  2017  2018  2019  2020  2021  2022  2023
sharpe 2.81  7.43  9.52  4.24 -0.60 -0.18 -0.03  0.59  1.06  0.47
```

BRAIN sends this value on ~44% of simulations (it sends `IS_LADDER_SHARPE` for
the rest) with the same value `/check` will later report — only the `result`
string flips `WARNING` → `FAIL`. Judge the **value**, never the string.
Absence means BRAIN sent the other check, which is unknown, not fine.

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
> A second axis was added once the first one hit its measured ceiling: **how
> many data sources**. `needs_field2` templates pair two fields drawn from
> *different themes*, weighted by `Catalog.field_value` (which carries the
> platform's own `alphaCount`, i.e. a direct crowding measure). Four of the five
> are minimal generalisations of shapes already in the list, and every one
> reuses an existing family so `priors.py` scores them with no extra LLM call.

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
   ┌─ refine ── greedy coordinate ascent, ~25 sims per survivor ─┐
   │  0 SIGN         1 sim   −1.6 Sharpe is a 1.6 alpha backwards│
   │  1 NEUTRALIZE   8 sims  biggest single lever, 9 values tried│
   │  2 ROBUSTNESS   3 sims  backfill/winsorize → CONCENTR_WEIGHT│
   │  3 TURNOVER     6 sims  decay aimed AT THE KNEE, hump,      │
   │                         trade_when                          │
   │  4 TRUNCATION   4 sims  drawdown, weight concentration      │
   │  5 PORTABILITY  3 sims  falsification: survive TOP1000?     │
   └────────────────────────────┬────────────────────────────────┘
                                ▼
   ┌─ metric gate ──── free: everything the sim record knows ────┐
   │  sharpe · fitness · turnover · sub-universe (BRAIN's limit) │
   │  · drawdown · margin (bps) · long/short balance · positions │
   │  · RECENCY: LOW_2Y_SHARPE, which the payload always carried │
   └────────────────────────────┬────────────────────────────────┘
                                ▼
   ┌─ deep gate ── costs API calls, so only for metric-passers ──┐
   │  yearly consistency   one-lucky-year filter                 │
   │  /check               PROD_CORRELATION et al                │
   │  /correlations/prod   the whole HISTOGRAM, not just the max │
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

**The metric gate is not the bar — measured, not assumed.** The funnel over the
whole accumulated database, recomputed live rather than read off a stored flag:

```
28,535   simulations dispatched
22,691   completed OK
 2,414   |Sharpe| >= 1.58
   524   clear every local gate
           top blockers: sub-universe 254, 2Y sharpe 132, then margin, fitness
   529   paid to measure PROD_CORRELATION      (across every gate version to date)
    19   came in under the 0.70 limit          <- the wall
     1   submitted, status ACTIVE
```

**A simulation record cannot tell you whether an alpha is submittable** — it
reports both correlation checks as `PENDING` with no value. That is why the
deep gate exists, and why "unknown" is recorded as NULL rather than as a pass.

### `PROD_CORRELATION` is the binding constraint, and it is extreme-value statistics

Of 529 alphas we have paid to measure, **510 failed** — median 0.87, and for a
long stretch not one of 99 consecutive ideas came in under the 0.70 limit. That
is not bad luck. `PROD_CORRELATION` scores an alpha on its **maximum**
correlation against every alpha in BRAIN's production pool, and that pool is
**~2.48 million**. The maximum of N draws is governed by

```
expected_max ≈ mean + sd × z(N),        z(2,478,170) = 4.945
```

N is fixed, so **mean and sd of the correlation profile are the only two levers
an alpha has**. A profile centred at 0.11 with sd 0.16 "expects" a maximum near
0.92 no matter how novel the idea feels. This is why `/correlations/prod` is
stored as a whole histogram in the `prod_corr_hist` table rather than as a
single number: the max tells you that you failed, the profile tells you whether
anything you changed actually moved the distribution. Two real rows:

| alpha | mean | sd | expected max | actual |
|---|---|---|---|---|
| `58lKRxQJ` | **−0.016** | **0.051** | 0.237 | 0.538 |
| `pwj9oMKg` | 0.138 | 0.154 | 0.899 | 0.865 |

**What moved it: neutralization, and only neutralization.** A positive mean
correlation against the production pool is a *factor-exposure signature*, and
neutralization is the lever that removes factor exposure. `NEUTRALIZATIONS` had
listed eleven values since day one and was read by nothing — the ladder only
ever tried five, so `STATISTICAL`, `CROWDING`, `FAST` and `SLOW` had **never
been simulated once**. On a probe expression three of them beat everything the
old tuple could reach (`STATISTICAL` 3.48, `SLOW` 3.11, `CROWDING` 3.04 against
a previous best of 2.98). The result on the real database is unambiguous — all
19 sub-0.70 alphas came from the neutralizations the ladder had never tried:

```
STATISTICAL 11    SLOW 6    FAST 1    SLOW_AND_FAST 1
INDUSTRY 0   SUBINDUSTRY 0   SECTOR 0   MARKET 0
```

The other lever tested against the same wall, **two-field templates**, was
pre-registered with a decision rule and then honestly scored as a null result:
median 0.837 across 135 measured two-field alphas versus 0.873 across 394
one-field ones. Real, small, and nowhere near the size of the neutralization
effect. It stays in the library; it is not the answer.

**Some crowding is still structural.** No amount of refinement fixes "this
hypothesis is already in production" — that is a signal to rotate region or
theme, not to tune.

**Portability (rung 5) is falsification, not optimisation.** An alpha that dies
on TOP1000 is a liquidity artefact. Better to kill it than to have it pass in
sample and lose money live.

**Daily returns, always.** Every cumulative equity curve trends up, so two
independent alphas show 0.9+ correlation on cumulative series. This one mistake
is the most common cause of a locally-vetted basket failing `SELF_CORRELATION`.

**BLOCK vs PREFER — the distinction this project got wrong for a long time.**
`docs/BRAIN_RULES.md` carries two different kinds of number, and they were
being applied as if they were one:

* **Submission requirements** (line 17–20) — what BRAIN actually *rejects*:
  turnover 1%–70%, self/prod correlation < 0.70.
* **Value Factor guidance** (line 76–77) — advice on what *earns well once
  submitted*: turnover < 30%, retain 70% of Sharpe in the sub-universe,
  margin > 4 bps.

The second set was wired in as a hard rejection, so a preference about payout
was silently deciding what was allowed to exist. **Measured cost:** of 100
distinct ideas clearing Sharpe 1.58 and Fitness 1.0, only **19** survived —
turnover alone hid 39 and the sub-universe rule hid another 34. Among the
hidden was the best candidate the pipeline had ever produced (Sharpe 2.64 /
Fitness 1.63), whose real server check came back PASS on every line except
`PROD_CORRELATION`.

So the two are now separate. **BLOCKS mirror what BRAIN rejects. PREFERENCES
steer `headroom()`, and through it the refinement ladder**, toward the payout
sweet spot without discarding anything BRAIN would have accepted. Widening a
block costs no extra API budget: deep checks are capped per run and spent in
score order, so a bigger eligible pool only improves what the cap buys.

| | platform (consultant D1) | machine BLOCK | machine PREFER |
|---|---|---|---|
| Sharpe | > 1.58 | **1.58** | — |
| Fitness | > 1.00 | **1.00** | — |
| Turnover | 0.01 – 0.70 | **0.01 – 0.70** | **≤ 0.30** |
| Self-corr | < 0.70 | **< 0.70** | — |
| Prod-corr | < 0.70 | **< 0.70** | — |
| Sub-universe | per-alpha formula | **BRAIN's own reported limit** | ratio ≥ 0.70 |
| 2-year Sharpe | ≥ 1.58 (per-alpha) | **BRAIN's own reported limit** | — |
| Drawdown | — | **≤ 0.12** | — |
| Margin | — | — | **≥ 4 bps** |
| Years positive | — | **≥ 70%, no year > 50% of PnL** | — |

`AutoGates` — the bar for submitting *without a human looking* — is a whole
tier stricter (Sharpe 2.10, prod-corr 0.45, turnover 0.22) and there the
asymmetry is deliberate, because nothing downstream reviews the decision.

---

## Part 4 — Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # add WQ_EMAIL / WQ_PASSWORD
python3 scripts/smoke_test.py # 279 offline checks, no network, no quota

python3 -m alphamachine --concurrency 3 catalog
python3 -m alphamachine priors                                # one-off, ~$0.70
python3 -m alphamachine --concurrency 3 scan   --budget 300
python3 -m alphamachine --concurrency 3 refine --top 25
python3 -m alphamachine gatelog --passing                     # what can I submit?
python3 -m alphamachine --concurrency 3 select --n 10
python3 -m alphamachine --concurrency 3 submit --n 3          # dry run
python3 -m alphamachine --concurrency 3 submit --n 3 --live   # for real
python3 -m alphamachine report --corr                         # observed prod-corr
python3 -m alphamachine audit                                 # live-db consistency
```

**Two test surfaces, and they answer different questions.**
`scripts/smoke_test.py` proves the **write path** is correct against a fresh
temp database, driven the way the test drives it — offline, no quota, no
network. It says nothing about whether the *actual accumulated* database,
written by real runs, crashes, migrations and interrupted processes over its
whole lifetime, still satisfies the invariants that write path assumes.
`alphamachine audit` is the compensating control: eleven checks that open the
live db **read-only** and recompute each cross-table invariant from first
principles. SQLite foreign keys are off and the schema has no `CHECK`
constraints, so every one of those invariants is enforced purely by caller
discipline — exactly the kind of thing that erodes silently. It exists because
two real bugs were found only because a human happened to ask about one
specific candidate; checks 2 and 3 are those two bugs' exact shapes.

### Running it unattended

```bash
nohup python3 scripts/autoloop.py --scan-budget 300 --refine-top 25 \
      --select-n 10 --submit-n 4 >> data/logs/autoloop.log 2>&1 &
```

`autoloop.py` sequences `scan → refine → gatelog → select → submit` forever, each
stage as a fresh `python3 -m alphamachine <stage>` subprocess. It reimplements
none of their logic, so a stage behaves identically whether launched from the
loop or from a terminal.

**Invariant, enforced in that one file: `submit` is invoked WITHOUT `--live`.**
No code path in it constructs that flag under any condition. Automating the loop
is not a reason to relax the one boundary worth keeping — submission stays a
dry-run report, never an action.

The loop's real job is handling the two failure signatures neither `cli.py` nor
`executor.py` turns into a clean stop: a dead session raises `BiometricRequired`
out of `_setup()` (uncaught, non-zero exit with a traceback), and `Executor.run`
catches it *per bundle*, sets its own halt flag, and returns having quietly done
less work than asked. Both mean the same thing — only a human can revive the
session. The loop logs one `NEEDS_AUTH` marker, polls a **free** endpoint
(`GET /users/self`, no biometric quota) until the session is valid again, then
retries the same stage. Retrying costs nothing: `scan` dedupes via
`already_simulated`, and `refine` replays a part-finished ladder from stored
results.

`scripts/night_check.sh` is a read-only overnight snapshot appended to
`data/logs/overnight.md`, with one exception — it restarts `autoloop.py` if the
process has died outright. It never touches submission, and never tries to solve
a biometric block; it just records the wait.

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
enforces a client-side budget of **20 per rolling 24h** and raises rather than
requesting a 21st — a hard stop, not a warning.

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
| Prod-corr profile | `prod_corr_hist` table | histogram kept verbatim, one row per alpha |
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

1. **Rung 1 (neutralization)** — biggest lever by a wide margin, 8 sims, and
   the *only* thing measured to move `PROD_CORRELATION`. Always run it, and run
   it over all nine values, not the obvious five.
2. **Rung 3 aimed at 12.5%** — most people decay past the knee and lose returns
   for nothing.
3. **The lucky-year filter** — cheapest overfitting filter that exists.
4. **The recency check** — nearly as cheap, and it was free all along. Without
   it the ladder actively *prefers* decayed signals, because every other term in
   `headroom()` is a whole-period aggregate: a Sharpe-2.39 / 2Y-0.48 alpha
   scored 0.96 and beat a steady Sharpe-1.75 / 2Y-1.8 one. After the fix it
   scores ~0.30 and loses, which is correct.
5. **Breadth across datasets** — the thing that keeps `SELF_CORRELATION` off you.
6. **Not submitting at the floor** — the 1.58-Sharpe alphas you *don't* submit
   are worth more than the ones you do.

**Read what the payload already contains before adding an API call.** Both the
recency fix and the sub-universe fix were the same bug twice: BRAIN sends every
check's value *and* its per-alpha limit in `is.checks` on every simulation, and
the pipeline stored the whole list and read three fields out of it. Fixing that
cost zero extra requests and recovered 6,989 two-year Sharpes and 15,707
trailing Sharpes from simulations already on disk. `MATCHES_PYRAMID`, which
carries the payout multiplier, is arriving free in that same list today and is
still unread — the same shape, still open.

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

And the one that made it all the way through — the first alpha this pipeline
ever submitted:

```
group_neutralize(rank(ts_delta(ts_backfill(eur_director_value_2, 120), 66)), industry)

neutralization  FAST          <- one of the four the ladder had never tried
sharpe 1.76   fitness 1.04   turnover 0.148   sub-universe 0.85 (limit 0.76)
PROD_CORRELATION 0.557        SELF_CORRELATION 0.098
2Y sharpe 1.93                pyramid USA/D1/OTHER ×1.4
status ACTIVE
```

Note the shape of it: a **1.76** Sharpe, well under the 2.6s that kept failing.
Nothing about it is spectacular in sample, and its `test` segment (2.09) beats
its `train` segment (1.68) — it is *getting better*, which is exactly what the
recency gate was built to find. The metric that had blocked all 99 predecessors
is the one it passes comfortably.

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
`autoloop.py` runs the entire pipeline unattended and still never constructs
`--live`; the one alpha submitted so far was sent by hand.

**A `200` is not evidence.** Several endpoints compute lazily and return `200`
with an empty body while they do. This caused a real bug worth guarding against
by name: `alpha_check()` accepted a `200` carrying an empty `checks` list and
cached it as a clean pass, so a candidate read as submittable for a full
`deep_check_ttl_hours`. Re-fetching the same alpha 123 seconds later returned 20
real checks including `PROD_CORRELATION FAIL 0.857`. Both halves needed fixing —
`client._recordset()` now retries on a `present` predicate rather than on the
status code, and `deep_verdict()` treats an empty checks list as *inconclusive*
whether it came fresh or from cache. `audit` checks the live table for the
poisoned shape.

**Bundling is all-or-nothing on the request, per-child on the error.** BRAIN
`400`s the *whole* multi-sim bundle if any single member is malformed, and the
error body is a per-child array in which valid members appear as `{}`. Reading
that as "everything was rejected" produces exactly the wrong conclusion — a
neutralization probe once reported all 7 variants invalid when 5 were fine.
Re-probe separately before believing a bundle-level rejection.

**`--concurrency` above your slot count does nothing good.** It builds a
server-side queue and costs you the ability to react to results as they land.

**Biometric quota is the real operational constraint**, not simulation budget.
The client hard-stops at 20 requests per rolling 24h against a platform cap of
25. In steady state you complete one face check and the persisted session
carries you; the quota only becomes visible when a session lapses mid-run --
which happens roughly every 4h under unattended operation (`autoloop.py`),
since each expiry needs its own re-authentication. The interval is a function of
**consultant tier** (~4h at Gold, up to 24h at Grand Master), so tier is an
operational variable and not only a payout one.

**Kill the parent, never the child.** `autoloop.py` classifies a stage
subprocess dying as FATAL and stops the whole pipeline. Sending a signal to the
`python3 -m alphamachine <stage>` process directly therefore halts everything;
`SIGTERM` to the `autoloop.py` parent is the designed path, and its handler
terminates the child cleanly.

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
