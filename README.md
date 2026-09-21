# alphamachine

A diversity-first alpha research pipeline for WorldQuant BRAIN, built around
the way BRAIN actually scores consultants rather than around raw backtest hunting.

---

## Repository layout

| Path | What it is |
|---|---|
| `alphamachine/` | the pipeline: `catalog` → `generator` (+ `bandit`, `templates`, `priors`) → `executor` → `refine` → `metrics` → `correlate` → `submit`, on top of `client` (HTTP), `store` (SQLite) and `config` (every threshold). `audit` checks the live database; `cli` wires it together. |
| `scripts/` | unattended operation (`autoloop.py`, `night_check.sh`, `status.sh`, `progress.sh`), persona auth (`auth_wait.py`), the offline test (`smoke_test.py`), and the **Power Pool** track (`check_powerpool.py`, `dedup_powerpool.py`, `corr_powerpool.py`, `tag_powerpool.py`, `submit_powerpool.py` — see Part 6; `sync_submissions.py` — see Part 4). |
| `docs/` | rule references, in Traditional Chinese. `BRAIN_RULES.md` is the authority for every number; `新手指南.md` is a from-scratch walkthrough; `docs/README.md` explains the priority order. |
| `data/` | runtime state — **entirely gitignored** (`machine.db`, session cookie, caches, logs). It is per-machine: a `git pull` on another computer brings the code, never this state. |

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
>
> A third axis widened the **operator vocabulary**. BRAIN offers 85 operators
> and the library used about twenty — and *every* template ended in
> `group_rank`, a universal construction and a prime suspect for being crowded
> platform-wide. Eight templates now each change exactly one axis of an existing
> shape (`group_zscore` / `group_scale` in place of `group_rank`, `ts_av_diff`,
> `ts_decay_linear`, `ts_arg_max`, `days_from_last_change`, the library's first
> conditional via `if_else`, `quantile`) so the bandit's measurement stays
> attributable. All stay within Power Pool's ≤ 8 distinct operators.
> `Catalog.operators()` caches `/operators` the way fields are cached. No
> template was deleted: a weak one sinks on the bandit's own evidence.

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

**The third stage learns on `PROD_CORRELATION` alone, not on the whole deep
verdict.** `deep_verdict.ok` is true only when crowding, recency *and* yearly
consistency all pass. That one bit collapses "this transform shape is crowded"
(attributable to the arm) with "this field's history decayed" (a property of the
field), and since ~97% of candidates fail *something* it was 0 for every arm in
the account's history — the bandit was allocating on its prior at the stage that
decides everything. A real case: an alpha cleared `PROD_CORRELATION` at 0.49, then
failed `LOW_2Y_SHARPE`, and taught `deep_pass = 0`. So `arms.corr_trials` /
`corr_pass` isolate the half the arm controls and `sample()` reads those;
`deep_*` is still recorded and still shown by `report` as true end-to-end
conversion, it just no longer allocates. Teaching is idempotent — keyed on the
**lineage root** (`bandit_taught`), because sibling rung variants of one idea are
one bet, not several: 820 measured rows collapse to 152 independent lineages.

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
whole accumulated database, recomputed live rather than read off a stored flag.
This block is a **snapshot from 2026-09-02**; the database has grown since
(2026-09-13, last write: 77,712 simulations, 55,296 completed OK, 4,503 with
|Sharpe| ≥ 1.58 — re-run `gatelog --passing` for current gate counts):

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
| IS ladder Sharpe | per-alpha ladder | **BRAIN's own reported limit** (the other recency check — BRAIN sends this or 2Y, never both) | — |
| Portability (rung 5) | — | **real TOP1000/TOP500 re-simulation vs `subuniverse_floor`** — a champion that collapses under its own falsification test is blocked | — |
| Pyramid multiplier | 1.0 – 2.0 payout factor | — | additive tilt, capped at 0.15 (below one failed check's 0.25) |
| Neutralization survival | — | — | rung 1 only: Beta(1,3) posterior of `PROD_CORRELATION` survival, weight 0.20 |

The two `PREFER` tilts are payout and learning signals, never eligibility: each
is kept smaller than one failed check, so it breaks a near-tie and can never buy
a broken variant. The pyramid one exists because BRAIN reports the **minimum**
multiplier over every pyramid an expression matches (9,414 of 9,414 multi-pyramid
alphas — never the max, never the mean), so one `returns` / `cap` / `volume` /
`adv20` token pulls the whole alpha into the USA/D1/PV pyramid at 1.1. It cannot
live on a bandit arm: the regex `theme` agrees with BRAIN's actual pyramid
category on only 25% of simulations, so it is read per candidate.

`AutoGates` — the bar for submitting *without a human looking* — is a whole
tier stricter (Sharpe 2.10, prod-corr 0.45, turnover 0.22) and there the
asymmetry is deliberate, because nothing downstream reviews the decision.

---

## Part 4 — Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # add WQ_EMAIL / WQ_PASSWORD
python3 scripts/smoke_test.py # 413 offline checks, no network, no quota

python3 -m alphamachine --concurrency 13 catalog
python3 -m alphamachine priors                                # one-off, ~$0.70
python3 -m alphamachine --concurrency 13 scan   --budget 300
python3 -m alphamachine --concurrency 13 refine --top 25
python3 -m alphamachine gatelog --passing                     # what can I submit?
python3 -m alphamachine --concurrency 13 select --n 10
python3 -m alphamachine --concurrency 13 submit --n 3          # dry run
python3 -m alphamachine --concurrency 13 submit --n 3 --live   # for real
python3 -m alphamachine report --corr                         # observed prod-corr (by row AND by lineage)
python3 -m alphamachine report --pyramid                      # payout multiplier earned vs available
python3 -m alphamachine report --neutralization               # PROD_CORRELATION survival by neutralization
python3 -m alphamachine audit                                 # live-db consistency
```

`report --corr` prints by-row **and** by-lineage on purpose. Sibling rung
variants of one idea are not independent observations, and reading the row count
is exactly how an 8/10 template "win rate" turned out to be 5 wins in 152
independent lineages (3.3%) — every winning arm sitting on n = 1. The
neutralization finding survives the same dedup (new-family 7/41 against classic
0/305); a per-template ranking does not. The only template claim that holds is a
negative one: `resid_vs_returns` is 1 win in 90 lineages while consuming the
largest share of deep-check budget.

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

`--concurrency` **should equal your real slot count** — **13** for this account as of
2026-09-21 (it was 3), which is now the default. The platform exposes no number for
it (`/users/self` is profile only), so it is configuration, not something the code
can read; above the real count you only build a server-side queue. `submit` is a dry
run unless you pass `--live`, and is capped at `max_submits_per_day` (default **4**,
matching the counted-alpha rule above) — **read from the platform**, see below.

**Read what the platform knows; do not keep a shadow counter.** The daily cap (4/day)
and the quarterly submission-day count (≥ 20 distinct days gates quarterly payment)
used to be counted from a local `submissions` table that only this pipeline wrote,
so an alpha sent from the web UI was invisible: `status.sh` showed *submitted: 1*
while 22 were ACTIVE on BRAIN. Both now come straight from the platform — your
ACTIVE alphas' `dateSubmitted` (`Submitter._active`, `count_submitted_today`,
`submission_days`) — and `submit` / `submit_powerpool.py` **refuse to submit** if
they cannot read it, rather than guess. At the moment of submitting, the live
`/check`'s own `REGULAR_SUBMISSION` (value / limit) remains the final authority.
The local table survives as a history ledger only (`scripts/sync_submissions.py`,
insert-only, never overwrites or deletes): it still feeds the Genius-pyramid
coverage report and `status.sh`'s offline fallback. `status.sh` reads the count from
the platform when a session is alive and labels the line `[平台即時]`, else
`[本機紀錄,可能落後]`. Similarly, `session_fresh()` now reads the session's end from
the JWT `exp` in the cookie (the same value `_expires_at` already held) instead of
guessing "cookie file age + 4 h".

The number this made real: **3 distinct submission days so far this quarter**, against
the ≥ 20 the code is meant to track. The old table would have said 1.

Also fixed in `status.sh`: it reported the running stage as `--concurrency` (the first
word after `alphamachine`, which is a global option) and now names the subcommand and
autoloop separately; it read a catalog log from 2026-08-28 against a hardcoded 299 and
now counts cached datasets; and its "best candidates" list ranked `gated` rows by
|Sharpe| alone — one showed a turnover of 1.299 — and now marks what fails today's gates.
(`progress.sh`, the catalog-run progress bar, still hardcodes 299.)

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

**The platform allows only 25 biometric requests per day**, and two things make it
easy to blow:

* every `POST /authentication` that returns 401-persona **mints a new inquiry**,
  so a retry loop spends quota even when you were not asking for a face check;
* a poll loop spends 2 units per iteration (finalise + re-auth), so polling
  every 10s burns the entire day in about four minutes and earns a
  `429 BIOMETRICS_THROTTLED` lockout with no `Retry-After`.

So `scripts/auth_wait.py` spends **2 units per attempt**: one to raise the inquiry,
one to finalise it. Waiting happens on a local sentinel file
(`touch data/persona_done`), never on the network.

**The client does not count biometric units any more** (removed 2026-09-21). There
used to be a budget of 20 per rolling 24h kept in `data/biometric_attempts.json`.
It was added after an early retry loop burned the quota in minutes, and it later
caused more downtime than it prevented — a session that expires every ~4 h ran into
*our* number before the platform's. The platform exposes no count to read (checked:
`/users/self` is profile only, and the only rate-limit headers on `/authentication`
are per-minute), so it was a guess. What still protects the account: a `429
BIOMETRICS_THROTTLED` from the platform raises immediately and is never retried, and
`REAUTH_COOLDOWN` stops one process re-POSTing `/authentication` in a loop. One
mistake that costs a unit each time and is worth avoiding: leaving `auth_wait.py`
before it prints `AUTH OK` — the check is bound to the session that raised it, so a
closed terminal strands a check you already passed.

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
bundle occupies one slot. On a 13-slot account that is the difference between
**13 alphas in flight and 130** (3 and 30 when this was written) — the single largest
throughput lever available, and it costs nothing.

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
you **pay for the same expression twice** — which on a small-slot account is a real
tax. So the URL is persisted before the first poll, `pending()` excludes
anything in flight, and `scan`/`refine` call `reclaim()` before dispatching
anything new.

`report` shows the in-flight count so you can see when there is unharvested work.

**An in-flight marker can outlive its job, and a 404 is not "still running".**
BRAIN stops serving a progress URL some time after the job ends (`GET` returns
`404 {"detail": "Not found."}`; a run 261 h old was long gone). `poll()` used to
ignore the status code: that body has no `children` / `status` / `alpha`, so it was
read as "finished, payload not built yet" and polled to the deadline, and
`reclaim()` saw "timeout" and kept the marker as *still running* — so 28 candidates
were never re-simulated and every start burned ~4.5 min of dead polling. `poll()`
now gives up after 5 consecutive 404s (one is not enough: a URL queried the instant
after its POST can briefly not exist), with an error deliberately not worded as a
timeout. Verified on the live database: `reclaim: 0 recovered, 0 still running, 28
unrecoverable` in 64 s, in-flight rows 28 → 0.

Also worth knowing: `refine`'s opening query (un-refined survivors, ranked by
|Sharpe|, over ~80k candidates) is silent and CPU-bound for about five minutes on
the 2 GB database before its first log line. That is not a hang.

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
trailing Sharpes from simulations already on disk. It has since been done three
more times, each with no API call: `MATCHES_PYRAMID` (the payout multiplier, on
100% of OK simulations; 24,378 stored rows backfilled), `IS_LADDER_SHARPE` (the
other recency check, the larger bucket; 19,702 rows), and rung 5's own children
(a `portability_ratio` for 1,246 already-gated champions). The habit is the
lesson: before adding an endpoint call, look at what the stored payload already
carries.

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

**`ts_regression(..., rettype=0)`** — confirmed 2026-09-20 against BRAIN's own
operator documentation: `0` is the Error Term, so the RESIDUAL family does compute
residuals (see Part 6 for how to read the page).

**No auto-submit loop by design.** `submit` defaults to dry run and is
day-capped. Submissions are effectively irreversible; a human should look at the
basket. `submit --auto` exists but applies `AutoGates`, a whole tier stricter
than the manual gates (Sharpe 2.10 vs 1.58), and queues anything borderline for
review instead of sending it. The asymmetry is the point: a bad simulation
wastes a slot, a bad submission occupies book space and cannot be withdrawn.
`autoloop.py` runs the entire pipeline unattended and still never constructs
`--live`; every submission is a human decision. (Power Pool alphas are submitted
by hand too, through their own script — see Part 6.)

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

**Session lifetime is the real operational constraint**, not simulation budget.
In steady state you complete one face check and the persisted session carries you;
it becomes visible when a session lapses mid-run -- roughly every 4h under unattended
operation (`autoloop.py`), since each expiry needs its own face check by a human. The
interval is a function of **consultant tier** (~4h at Gold, up to 24h at Grand
Master), so tier is an operational variable and not only a payout one. The platform's
daily cap of 25 biometric requests is not tracked locally.

**Kill the parent, never the child.** `autoloop.py` classifies a stage
subprocess dying as FATAL and stops the whole pipeline. Sending a signal to the
`python3 -m alphamachine <stage>` process directly therefore halts everything;
`SIGTERM` to the `autoloop.py` parent is the designed path, and its handler
terminates the child cleanly.

---

## Part 6 — The Power Pool track

A separate, hand-driven track that sits *beside* the pipeline rather than inside
it. Power Pool alphas are small, simple, high-quality signals with their own
eligibility rules (`docs/Power_Pool_Alpha_規則.md`); the pool worth screening is
the **UNSUBMITTED** alphas already sitting in your account from simulation.

**Eligibility — all required:** Sharpe ≥ 1.0 · ≤ 8 distinct operators
(`ts_backfill` / `group_backfill` don't count) · ≤ 3 distinct data fields (group
fields such as `industry` or `sector` don't count) · Power Pool correlation < 0.5
(or Sharpe ≥ 10% above the most-correlated pool member) · turnover test and
sub-universe test pass. It also needs a compliant **Idea / Rationale** description
of at least 100 characters in Properties, or it earns no Power Pool payout even
if tagged.

**Why it has its own submit script.** A *pure* Power Pool alpha — one that fails
the Regular-alpha bar — is exempt from the thresholds `Submitter` applies.
Confirmed live on 2026-09-19: once an alpha carries `PowerPoolSelected` plus a
compliant description, BRAIN itself reclassifies `LOW_FITNESS`,
`PROD_CORRELATION` and `LOW_2Y_SHARPE` from FAIL to WARNING. Sending it through
the Regular path would apply this project's stricter private bar and reject
alphas the platform would accept. So `submit_powerpool.py` gates on BRAIN's own
live `/check` and nothing else.

```bash
# 1. screen: fetch every UNSUBMITTED alpha, evaluate locally, write a JSON report
#    (no --deep here: that is one live call per alpha)
python3 scripts/check_powerpool.py --since 2026-08-01T00:00:00+00:00 --json > pp.json

# 2. dedup, offline: thousands of eligible alphas are a few dozen ideas.
#    --against makes it count what you have ALREADY submitted (ACTIVE).
python3 scripts/check_powerpool.py --status ACTIVE --json > pp_active.json
python3 scripts/dedup_powerpool.py pp.json --against pp_active.json \
        --max-per-field 1 --max-per-dataset 2 --max-per-shape 3 -o pp_dedup.json
#    decayed alphas (recent Sharpe < BRAIN's limit) are dropped by default;
#    --allow-decayed keeps them. A report made before `recency` existed needs
#    --recency-db data/machine.db to fill it in.

# 2b. the gate that matters: recompute correlation from PnL against the WHOLE
#     ACTIVE book, exactly as BRAIN does (last 4 years). Re-run right before submitting.
#     Feed it a WIDE dedup (e.g. --max-per-field 3 --max-per-dataset 6
#     --max-per-shape 15), not the tight one: the tight caps are a proxy for what
#     this measures, and they threw away good candidates (see below).
python3 scripts/corr_powerpool.py --from-file pp_dedup_wide.json --top 200 --json > corr.json

# 3. deep-verify a small slice of the deduped list. The output order already
#    spreads pyramids, so --top N is a spread, not just the N highest Sharpes.
#    --from-file re-reads a saved report without re-fetching.
python3 scripts/check_powerpool.py --from-file pp_dedup.json --deep --top 10 --delay 10 --json > pp_deep.json

# 4. tag + describe the verified ones (never overwrites an existing tag/description).
#    The built-in template only fits "field changes -> price follows" signals;
#    for anything else write the descriptions yourself and pass them in.
python3 scripts/tag_powerpool.py pp_deep.json --dry-run
python3 scripts/tag_powerpool.py pp_deep.json --descriptions hand_written.json
#    --include-unverified also tags a record whose ONLY unresolved gate is
#    POWER_POOL_CORRELATION (see the note on a stuck queue, below).

# 5. submit by id — dry run unless --live; re-reads the alpha and confirms ACTIVE
python3 scripts/submit_powerpool.py ID1 ID2 ID3 ID4
python3 scripts/submit_powerpool.py ID1 ID2 ID3 ID4 --live
```

**Daily quota.** The 3-month rule in `docs/Power_Pool_Alpha_規則.md` §4: *after* a
consultant's first submission is three months old, pure Power Pool alphas are
limited to **1 per day and 10 per calendar month** (single scope). *Before* that,
the standard limit applies — **up to 4 alphas per day**. The rule is measured
from the first submission, which this database records as 2026-09-02; that would
put the switch near 2026-12-02, but the database only sees submissions made
through this pipeline, so confirm your true first-submission date on the
platform before relying on it.

**Things that will bite you:**

* **State is per-machine.** `data/` is gitignored, so `machine.db` — the
  candidates, simulations, bandit state and PnL cache — is not shared between
  computers, and a `git pull`
  brings only the scripts. The `--json` report is likewise a plain file you must
  carry across yourself (or re-generate: `check_powerpool.py` re-fetches from
  BRAIN). Today's submission count is not affected: it is read from the platform, so
  a submission from another machine or the web UI is counted.
* **"Today" is the code's assumption.** The counter uses a scoring day starting
  **03:00 America/New_York** (`submit.scoring_day_start()`), i.e. 15:00 in Taiwan
  during US daylight time and 16:00 otherwise. That boundary is the project's own;
  the docs in this repo mention 3 AM EST only for Community Activity updates, not
  for submission counting. Treat it as unverified.
* **`POWER_POOL_CORRELATION` is computed lazily, and pacing is not solved.**
  Without `--deep` it sits at `PENDING`; firing `/check` requests back-to-back
  enqueues the computation faster than BRAIN resolves it, so *every* candidate
  comes back `PENDING` regardless of quality (confirmed live 2026-09-19 — 193 of
  245 in the first batch). The default `--delay 2` is **not enough on its own**:
  a 10-candidate test at `--delay 3` still resolved 0 of 10. Test a small
  `--top N` first; if it is all `PENDING`, try 10 s or more, or spread a large
  list across several sessions. `/check` also timed out repeatedly under load
  (`timed out waiting for /alphas/.../check`), so stop and wait rather than push
  through. `PENDING` means unknown, never pass — such a candidate is reported
  provisional.
* **Do not hammer `/check`; a PENDING can outlast the session.** After a burst of
  roughly forty `/check` calls on 2026-09-20, `POWER_POOL_CORRELATION` stayed
  `PENDING` on every candidate for over an hour — including one that had resolved
  earlier the same day — consistent with issue #4's report that volume aggravates
  the queue. Tagging does not need the value, and `corr_powerpool.py` measures it
  without touching `/check`, so the pattern is: measure locally, tag with
  `--include-unverified`, let the queue drain, and let the submit attempt itself
  do the authoritative recompute.
* **Rank by recent performance, not whole-period Sharpe.** Every number in a
  screening report is a full-history aggregate, so a signal that earned Sharpe 3
  early and nothing since sorts to the top. On the real 2026-09-20 pull **11,715
  of the 17,509 structurally eligible alphas (67%) had a recent Sharpe below
  BRAIN's own limit** — and the three highest-Sharpe survivors of the first dedup
  (2.82, 2.57, 2.22) had a trailing Sharpe of **0.00**. One of them (`akLnG3XW`)
  showed `LOW_2Y_SHARPE 0.0` against 1.58, `LOW_DURATION 242` against 500 and
  `CONCENTRATED_WEIGHT 0.197` against 0.10 on `/check`; the last is exempt for a
  pure Power Pool alpha, the other two are not covered by the docs. `evaluate()`
  now records `recency` (BRAIN's `LOW_2Y_SHARPE` value, else `IS_LADDER_SHARPE`,
  each against its own per-alpha limit — the value, never the result string, which
  reads `WARNING` at simulation time and only flips to `FAIL` on `/check`) and
  `dedup_powerpool.py` drops the decayed ones before choosing each idea's best
  variant. Unknown recency is kept, not dropped.
* **Dedup is not optional, and field identity is not enough.** A full `--since`
  scan returns tens of thousands of near-duplicate ideas, and submitting twins
  fails the Power Pool correlation check the first twin just tightened.
  `dedup_powerpool.py` narrows in four stages: (1) **signature** — (region,
  delay, operator set, field set), best Sharpe wins; (2) **field cap** — an
  underlying field backs at most N kept alphas, with ubiquitous plumbing fields
  (`returns`, `cap`, `assets`, …) exempt because they are denominators, not
  ideas; (3) optional **dataset cap**, from the field catalog cache; (4) **shape
  cap** — at most N alphas per operator set. `--against` seeds all of it with the
  alphas you have already submitted.
  The fourth stage exists because of what stages 1–3 left behind. On the real
  2026-09-20 pull (55,845 UNSUBMITTED, 17,509 eligible) the funnel was 2,926
  signatures → 2,920 after dropping twins of the 18 ACTIVE → 397 at one per field
  → 157 at two per dataset — and **89 of those 157 had the identical operator set
  `{group_rank, ts_regression}`**: `-ts_regression(returns, <field>, 126, rettype=0)`,
  the residual of `returns` on some field, which is mostly `returns` again. One
  reversal factor written 89 times, each on a "different" field. Transform shape
  drives correlation more than field identity does — the same finding as
  `resid_vs_returns` converting at 1 in 90 lineages. With three per shape the
  list is **46 alphas across 21 operator sets** (2 → 33, 5 → 62, 8 → 73).
  Stage 1 lands exactly on the 2,926 that issue #4's ad hoc dedup got, which is
  the cross-check that stage is doing the same thing.
  **The caps are still a heuristic, not a measurement.** Two alphas of that shape
  each passed `POWER_POOL_CORRELATION` against the pool (0.26 and 0.17) yet
  correlate **0.7162 with each other** — submitting both fails the second, since by
  then the first is in the pool. The heuristics do not catch that on their own:
  with one `ts_regression` reversal alpha already submitted and `--max-per-shape
  3`, two more of that shape are still allowed, and `returns` is exempt from the
  field cap — so `qMjaRoNP` was still on the list after its rejection. The gate
  that does is `corr_powerpool.py` (next bullet); `--max-per-shape 1` is the
  conservative setting.
* **Screen wide, then measure; do not screen tight and hope.** The tight dedup
  (1 per field, 2 per dataset, 3 per shape) left 41 candidates, and 28 of the 33
  non-reversal ones measured over 0.5. Widening to 171 and measuring every one
  found candidates the tight caps had discarded — Sharpe 2.25 / 2Y 3.22 and
  Sharpe 2.41 / ladder 2.58 among them — and a greedy pick (take the best, add it
  to the book, recompute the rest) gave **15 alphas that are mutually compatible**
  at ≥ 0.10 under both limits. The order matters: fix what is already verified
  and tagged into the book *first*, or a higher-Sharpe newcomer displaces it.
* **The earlier batch of 52 was not deduped, and it shows.** Listed with
  `GET /users/self/alphas?status=UNSUBMITTED&tag=PowerPoolSelected` (the singular
  `tag` filters; `tags` is silently ignored and returns everything), 50 remain
  tagged. Many are copies of each other with identical correlation numbers (six on
  `inst6_num_of_institutional_holders`, five on `search_interest_1y_corporate_name`),
  and about half are variants of the four already submitted, at **correlation
  1.000** with them. Only a handful clear 0.5.
* **BRAIN correlates over the last four years, and the book is the WHOLE book.**
  `scripts/corr_powerpool.py` recomputes each candidate's correlation from PnL
  against every ACTIVE alpha — Power Pool ones (limit 0.5) and regular ones
  (`SELF_CORRELATION`, limit 0.7) — with `correlate.aligned_corr(years=4)`. The
  window is the finding: Pearson on daily PnL *changes* over the last four calendar
  years reproduces BRAIN's own numbers **to four decimals, eleven times** (`SELF`
  0.3434, `POWER_POOL` 0.3365, 0.1637, 0.7162 and seven earlier `/check` values such
  as 0.6258, 0.5404, 0.4797, 0.4938); the whole 10-year history read 0.03–0.05
  **low** every time — the unsafe direction for a "too correlated?" gate. One
  number does not reproduce (`SELF_CORRELATION 0.2271` for one alpha, mine 0.1791),
  unexplained. Two mistakes this corrected, both mine: the first pass measured
  candidates against only *that day's* submissions and missed the alphas submitted
  the day before, so `qMWWmAnK` — a Power Pool alpha that turns out to be the
  worst-correlated member of the book for almost every candidate — never entered
  the comparison; and it used the full window. Of 33 recency-healthy, non-reversal
  candidates, **28 were over 0.5**, several I had called safe (`zqYRPw6o` 0.56,
  `P07YOQlM` 0.62, `zqYW73a1` 0.62). Run it again immediately before each
  submission: the book changes with every alpha sent.
* **Operator/field counts are parsed heuristically** from the expression against
  the live `/operators` list. Usually right, not a substitute for the counts on
  the alpha's own page.
* **No robust-universe check by name.** No check in the live payload obviously
  maps to it; every check an alpha carries is in the JSON under `all_checks`.
* **Description phrasing is hand-authored, and the template is narrow.**
  `tag_powerpool.py`'s `FIELD_CHANNEL` maps each field to a sentence about what
  it measures; a field it doesn't know gets a vaguer description and a warning.
  Worse, the template's Idea sentence is "when the field changes, price follows",
  which misstates any alpha that is not a field-change signal — e.g. the
  `-ts_regression(returns, x, 126, rettype=0)` reversal family, where the idea is
  that the part of returns a field cannot explain tends to revert. Use
  `--descriptions FILE` (`{alpha_id: text}`) for those. Every description, template
  or hand-written, is validated before anything is PATCHed: the three headers
  (`Idea:`, `Rationale for data used:`, `Rationale for operators used:`) in order,
  and at least 100 characters.
* **Verify a write by reading it back, and know what "absent" means.** Before
  tagging, `/check` lists `POWER_POOL_DESCRIPTION_LENGTH` and `_FORMAT` as
  `WARNING`; after a compliant description they are simply no longer listed.
  That is the expected outcome, but it is absence, not an explicit `PASS`.
* **`rettype=0` is confirmed.** BRAIN's operator record only says "returns various
  parameters"; the operator's documentation page (`GET /operators/ts_regression`)
  gives the table, and `0` is **Error Term** (`1` intercept, `2` slope, `3`
  y-estimate, `4` SSE, `5` SST, `6` R², …). The old caveat in Part 5 is settled.
* **`--descriptions` does not fix the field lookup.** On 2026-09-20 the template
  path's `data_fields(search=…)` calls returned `400 maximum offset + limit for
  search query is 100`, so template descriptions would read "(說明未取得)" for
  every field. It was not failing on 2026-09-19 (the four submitted Power Pool
  alphas have full descriptions). Not yet fixed.
* **A "pure" Power Pool alpha is one that fails the Regular bar,** and once
  tagged BRAIN shows its would-be failures as `WARNING` (`PROD_CORRELATION 0.7285`
  against a 0.70 limit, seen live). Docs §3 say such an alpha must also match the
  *current* Power Pool theme, and `MATCHES_THEMES` read `WARNING` — but it did not
  block: `omqbbG2v` went `ACTIVE` on 2026-09-20 that way, classified `Power Pool
  Alpha` + `Cluster Alpha`, as did the four on 2026-09-19. Two observations, not a
  rule.
* **That `WARNING` is conditional on staying Power Pool eligible.** `qMjaRoNP`
  (Sharpe 2.85) was tagged with `LOW_FITNESS 0.97`, `CLUSTER_TEST 1.06` and
  `PROD_CORRELATION 0.74` all shown as warnings. It had passed
  `POWER_POOL_CORRELATION` (0.17) — until `omqbbG2v`, a same-shape alpha with
  daily-return correlation 0.7162, joined the pool. At submission the check
  recomputed to **0.7162** against the 0.5 limit, its Sharpe was not 10% above
  `omqbbG2v`'s 3.01 so the exemption did not apply, it stopped counting as a Power
  Pool alpha, and the Regular bar returned: `LOW_FITNESS` and `PROD_CORRELATION`
  became **FAIL** and the submit came back `403`. The shape cap and the pairwise
  PnL check exist to catch exactly this *before* a slot is spent.
* **"In order to submit this alpha, you have to add an alpha description
  following the given template"** is BRAIN's web UI refusing to submit an
  untagged, undescribed alpha — no API attempt is made (`GET /alphas/{id}/submit`
  is `404`, versus `403` with a check list for a real rejection). The fix is the
  tag and description step, but check the alpha first: it is the last thing to
  do, not the first, and it is easy to describe an alpha that should not be sent.
* **`/check` is cached; the submit attempt is not.** After `omqbbG2v` was
  submitted, `/check` on `qMjaRoNP` still said `POWER_POOL_CORRELATION PASS 0.1691`
  and `REGULAR_SUBMISSION 0/4`, while the submit attempt saw `0.7162` and `3/4`.
  A candidate's `/check` does not update when the pool changes under it, so
  recompute pairwise correlation against what is *now* in the book before
  trusting a PASS. A rejected attempt's checks can be read back with
  `GET /alphas/{id}/submit` (it returns the `403` body with the check list at
  rejection time) — that is where the real reason lives.
* **Diversity still applies.** Piling every pick into one pyramid helps neither
  Genius (which counts distinct completed pyramids) nor the Dynamic Pyramid
  Multiplier (which rewards under-used ones); `check_powerpool.py` prints each
  candidate's pyramids for that reason.

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
