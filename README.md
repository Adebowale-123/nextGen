# NextGen Game

A real-time digital lottery platform: daily draws, provably fair RNG, a double-entry wallet ledger,
deposits and payouts through Nigerian payment providers, tiered KYC, responsible gaming controls,
and a back office for operations.

Built with Django 6.1 on PostgreSQL. It runs fully in **sandbox mode** out of the box, with a mock payment
gateway, mock KYC, and SMS/email printed to the terminal, so you can exercise every flow without any API keys.

---

## Quick start (Windows, PowerShell)

```powershell
cd nextgen-game
.venv\Scripts\Activate.ps1          # the virtualenv is already created
copy .env.example .env              # then edit .env (see "PostgreSQL" below)
python manage.py migrate
python manage.py seed_games --quick-draw 5   # 2 games + a test draw closing in 5 min
python manage.py createsuperuser             # your back-office login
python manage.py runserver
```

In a **second terminal**, run the draw scheduler. It closes sales, draws numbers, settles winners,
and opens the next draw:

```powershell
.venv\Scripts\Activate.ps1
python manage.py run_scheduler
```

Open http://127.0.0.1:8000. The back office is at `/backoffice/` and the Django admin at `/admin/`.

### PostgreSQL

Without `DATABASE_URL` the app falls back to SQLite. To use your local PostgreSQL 18, open
**SQL Shell (psql)** from the Start menu, log in as `postgres`, and run:

```sql
CREATE USER nextgen WITH PASSWORD 'choose-a-password';
CREATE DATABASE nextgen OWNER nextgen;
```

Then set this in `.env` and run `python manage.py migrate` again:

```
DATABASE_URL=postgres://nextgen:choose-a-password@localhost:5432/nextgen
```

On PostgreSQL, a database trigger also makes ledger rows physically un-updatable and un-deletable.

---

## Public demo on Render

`render.yaml` sets up the web service and a PostgreSQL database in demo mode:
- payments use the sandbox checkout, so no real money moves,
- sign-up codes appear on screen,
- a "no real money" banner shows on every page.

1. Open https://render.com/deploy?repo=https://github.com/Adebowale-123/nextGen and sign in with GitHub.
2. Enter **ADMIN_EMAIL** and **ADMIN_PASSWORD**. They become your back-office login and are stored only in Render.
3. Click **Apply**. The first build takes about 5 minutes; the site then appears at `https://nextgen-game.onrender.com` (or similar).

Free-plan limits:
- The site sleeps after 15 minutes without visitors, and the first visit afterwards takes about 30 seconds.
- The free database expires after 30 days unless you upgrade it.
- Uploaded ID photos are lost on each redeploy.

Games open, close and settle as visitors arrive, because the free plan has no always-on worker. For a real launch, upgrade the plan, add a background worker running `python manage.py run_scheduler`, and set `DEMO_MODE=false` and `SCHEDULER_ON_REQUEST=false`.

## The main game: NextGen Daily (spin game)

**Coins.** Players see everything in coins (1 coin = ₦10 by default; change it in Admin → Platform settings → Coins).
They buy coin packages, play for 50 coins, win coins, and cash out coins to naira at the same rate. Money is still
stored in kobo behind the scenes, and the back office and admin keep showing naira.

- **Price and numbers:** ₦500 per play. Pressing **Play Game** gives the player 4 random numbers from 1–90.
- **Two batches a day:** 8:00 AM–5:00 PM and 8:00 PM–12:00 AM (Lagos time). You can change this per game in Admin → Games.
- **After a batch closes:** sales stop automatically. An admin opens **Back office → 🎡 Spin** and presses **Spin now**. Setting `AUTO_SPIN_AFTER_MINUTES` makes the system spin automatically if no admin has.
- **Settlement:** 60% of sales becomes the prize pool and 40% goes to the company.
  - Matching all 4 numbers wins the ₦100,000 grand prize. This is guaranteed: the company tops up the pool if it's too small.
  - The rest of the pool pays ₦7,000 consolation prizes, best matches first. Ties are decided by a seed-based random order that anyone can verify.
  - Prize money too small for another ₦7,000 prize carries over to the next batch.
- **Welcome bonus:** new players get ₦500 bonus credit when they verify their account. It's play-only, never withdrawable, and is spent before cash. Anything it wins goes to the player's cash balance.

To test a spin right away, run `python manage.py seed_games --quick-batch 10`. That opens an extra batch closing in 10 minutes.

## Trying every flow in sandbox mode

| Flow | How to test it |
|---|---|
| **Sign up + OTP** | Register with a phone number or email. The 6-digit code is printed in the `runserver` terminal. |
| **Deposit** | Wallet → Deposit → sandbox checkout → "Simulate successful payment". The confirmation goes through the same signed-webhook path a real provider uses. |
| **Buy tickets** | Play → pick numbers (or Quick Pick) → Buy. If your balance is too low you're sent to Deposit. |
| **Draw + settlement** | Use `seed_games --quick-draw 2` for a draw closing in 2 minutes, with `run_scheduler` running (or press "Run draw scheduler now" in the back office). |
| **Verify fairness** | Results → any draw → "Verify in my browser". |
| **KYC Tier 2** | Account → Verify ID → NIN `12345678901` auto-approves. Any ID ending in `0000` goes to manual review (approve it in Admin → KYC submissions). |
| **Withdraw** | Requires Tier 2, and deposits must be played through once. Any 10-digit account number works. One ending in `9999` simulates a failed payout (funds return to the wallet), and one ending in `0000` fails name lookup. |
| **Manual review** | Withdrawals ≥ ₦500,000, accounts under 24h old, flagged users, more than 3 withdrawals/day, or bank accounts shared between players go to Admin → Withdrawals ("Approve & pay out" / "Reject"). |
| **Responsible gaming** | Account → Responsible gaming: daily deposit/play limits (decreases instant, increases after 24h) and self-exclusion. |

Run the tests with `python manage.py test apps`. To check that the books balance, run
`python manage.py check_ledger`.

---

## How the scope document maps to the code

| Scope item | Where |
|---|---|
| Player accounts, OTP, Google OAuth 2.0, KYC tiers | `apps/accounts/` (`services.py`, `oauth.py`, `kyc_providers.py`) |
| Wallet & double-entry ledger | `apps/ledger/` (`services.post_transaction` is the only way money moves) |
| Deposits, webhooks, payouts, risk engine (Flows B & E) | `apps/payments/` (`services.py`, `providers.py`) |
| Ticket purchase, draws, RNG, settlement (Flows C & D) | `apps/games/` (`services.py`, `rng.py`) |
| Limits, self-exclusion, AML play-through, risk flags | `apps/compliance/` |
| In-app / SMS / email notifications | `apps/notifications/` |
| Back office: dashboard, reports, ledger, review queues | `apps/backoffice/` plus the Django admin (`admin.py` in each app) |

### Ledger accounts

| Account | Type | Meaning |
|---|---|---|
| `player:<id>:NGN` | Liability | A player's wallet (can never go below zero) |
| `system:pool_hold:draw-<id>:NGN` | Liability | Stakes held for one draw until settlement |
| `system:withdrawal_pending:NGN` | Liability | Withdrawals held while being paid out |
| `system:provider_clearing:<provider>:NGN` | Asset | Money held at Paystack/Flutterwave |
| `system:house_revenue:NGN` | Revenue | Operator's gaming revenue (GGR) |

The back-office dashboard shows **fund segregation**: everything owed to players against what is held at the providers.

### Design decisions worth knowing

- **Double-spend protection uses database row locks (`SELECT … FOR UPDATE`), not Redis.** The scope
  document suggested locking funds in Redis. A row lock inside the same transaction that writes the
  ledger can't drift out of sync with it, and it doesn't need another service. Redis is still
  recommended in production for the cache (rate limiting) and the task queue.
- **Webhooks are a hint, not proof.** Every deposit is re-verified with the provider's API, and the
  amount and currency are checked, before the wallet is credited.
- **Everything that moves money is idempotent.** Repeated webhooks, double-clicked "Buy" buttons and
  re-run settlements can't double-credit.
- **Prize tiers** are either a fixed multiple of the stake or a percentage of the draw pool split
  between winners. If fixed prizes exceed the pool, the house tops it up. Leftover pool goes to house revenue.

---

## Going live: checklist

**Legal and compliance (do these first):**
- [ ] Operating licence: the National Lottery Regulatory Commission (NLRC) and/or the relevant state
      lottery/gaming board. Since the 2024 Supreme Court ruling, much of lottery regulation sits with the
      states, so get Nigerian legal advice on which licences you need.
- [ ] Certified RNG: regulators usually require independent testing (e.g. GLI, iTech Labs, BMM).
      `apps/games/rng.py` is written to be auditable, but it is **not certified**.
- [ ] NDPA 2023 data protection compliance (KYC data, retention, a DPO) and AML/CFT obligations (SCUML registration, STR reporting).
- [ ] Terms, privacy policy, and responsible-gaming help-line details in the footer.

**Integrations:**
- [ ] Paystack and/or Flutterwave live keys. Set webhook URLs to `{SITE_URL}/wallet/webhooks/paystack/` and `/wallet/webhooks/flutterwave/`.
      Re-check the provider docs against `providers.py`, since APIs change (Flutterwave is moving to a v4 API).
- [ ] Termii (SMS) and Dojah (KYC) live credentials.
- [ ] Google OAuth client (optional).

**Infrastructure:**
- [ ] PostgreSQL with automated backups. Redis for `CACHE_BACKEND`.
- [ ] A worker-backed `TASKS_BACKEND` (e.g. the `django-tasks` database backend) so payouts and SMS run outside the web request.
- [ ] `run_scheduler` as an always-on service, with only one instance.
- [ ] HTTPS, `DEBUG=false`, a strong `SECRET_KEY` and a separate `TICKET_SIGNING_KEY`, plus `collectstatic` behind a CDN/whitenoise.
- [ ] Monitoring and alerts on: stuck payouts, failed webhooks, `check_ledger` failures, and the fund-coverage badge.

## Phase 2 ideas (out of scope for Phase 1)

Crypto deposits/withdrawals · jackpot rollover · instant-win/scratch games · web push notifications ·
a mobile app on a REST API · public randomness beacon (e.g. drand) mixed into the client seed.
