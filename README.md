# aiprove-bot

An autonomous agent for the [AIProve](https://aiprove.fun) bounty arena. It holds
its own keypair, reads the board, enters what is open, builds real entries for the
objective bounties, and submits with a provenance transcript.

It is also the reference implementation for `sample-agent-skillmd` — the bounty
that asks for exactly this loop: read `skill.md`, authenticate with a keypair, list
open bounties, enter one.

## What it does

| command | what happens |
|---|---|
| `--whoami` | address, handle, credit state, referral link |
| `--status` | bounties you are competing in, submissions and their status |
| `--enter` | enters every open bounty (free during beta) |
| `--forecast` | builds and submits every forecast bounty from live price data |
| `--daily` | cron entry: jitter → whoami → enter → forecast → report |
| `--referral-farm N` | N fresh agent keys, each binds your referral code and enters one bounty |
| `--referral-status` | verified referral count |

## Install

```bash
pip install eth-account numpy
cp config.json.example config.json     # then paste a private key
python src/main.py --whoami
```

## Auth

Identity is an EVM keypair — no signup, no email. The flow is three calls:

```
GET  /api/auth/challenge?address=0x..   -> { address, challenge }
     sign the challenge string with personal_sign (EIP-191)
POST /api/auth/verify {address, signature} -> { token, expiresAt }
```

`src/api.py` caches the token in `data/session-*.json` and re-signs when it is
within 10 minutes of expiry. `POST /api/me/agent-token` mints a long-lived token
(~1 year) if you would rather not keep the key in the runner.

## The three submission shapes

The arena accepts three different payloads and rejects the others, so the client
has one method per shape:

| bounty type | payload |
|---|---|
| forecast / prediction | `{"prediction": {"answer": "...", "confidence": 0.52}}` |
| code, AutoGrade | `{"repoUrl": "...", "commit": "<sha>", "transcript": "<log>"}` |
| content, PosterJudge | `{"link": "https://..."}` |

`link`, not `url`. The API answers `400 {"error":"submit a link (URL) to your work"}`
for a missing or wrongly-named field, and there is no withdraw route — so a probe
with a throwaway payload on a real wallet leaves a permanent bad entry. Probe on a
disposable key instead.

## Forecast model

`src/forecast.py` pulls daily closes from Binance (BTC/ETH) and Yahoo (SPY, NVDA,
GLD), then:

- **direction bounties (Brier score)** — logistic regression on five price features,
  fitted on the first 70% of history and scored on the last 30%. If it does not beat
  the base rate out of sample it is **shrunk to the base rate**, and the run output
  says so. A coin-flip market does not reward a confident guess; Brier score
  punishes one.
- **level bounties (absolute error)** — random walk for a one-day horizon (the
  current close is the median-optimal forecast), plus a half-shrunk drift term for
  multi-day horizons.

Every run prints the evidence next to the number:

```
btc-tomorrow   {"answer":"yes","confidence":0.5029}  [P(up)=0.503 base=0.503 model_used=False brier_oos=0.25367/0.25011]
```

## Humanization

`src/humanize.py`, and it is deliberately narrow. AIProve kills sybil on the money
side by itself — each wallet needs its own 100,000 $PROVE credit, so spreading
across wallets multiplies the capital you must post for the same reward. The only
sybil surface is the **referral** bounty, and that one is verified "from on-chain
activity — no self-referrals, one credit per wallet".

So what gets randomised:

- **handles** read like agent names (`LucidOtter69`), not `acc01`
- **stagger of 90–420 s** between referred wallets, and `--daily` sleeps a random
  0–120 s at start so a cron at 07:00 never fires at 07:00:00
- **entry spread** — each farm wallet enters a *different* bounty in rotation, so
  the on-chain pattern is not N identical actions inside one minute
- **gaps between submissions** with a 12% chance of a long "distracted" pause
- **transcripts** are the real session log, framed with a session header and the
  occasional retry — never invented content, and `mask_secrets()` strips private
  keys, JWTs and `sk_`/`ghp_` tokens before anything leaves the machine

What is *not* randomised: the work. The transcript has to match the commit or the
entry is rejected, which makes fabricating one pointless.

## Provenance

`transcriptHash` is what the server stores — you can see it in the submit response.
The transcript itself is never published. `report()` writes both `data/last-run.txt`
and `data/last-run.json` so a run can be replayed.

## Cron

```
python src/main.py --daily            # deterministic; wire as a no_agent cron job
```

Scheduling it before 07:00 WIB means the forecast bounties have already rolled over
to the new day.

## Files

```
src/api.py        keypair identity, EIP-191 session, the API surface
src/forecast.py   price feeds, Brier model with out-of-sample validation, point forecasts
src/humanize.py   timing, handle generation, transcript framing, secret masking
src/main.py       CLI + the referral farm
work/             bounty entries (code-golf, bug-hunt, fastest-primes, eth-pnl)
data/             session cache, ledger, referral pool  (gitignored)
```

## Licence

MIT.
