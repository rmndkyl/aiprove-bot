"""AIProve bot CLI.

  python main.py --whoami
  python main.py --status
  python main.py --enter            # enter every open bounty (free)
  python main.py --forecast         # build + submit every forecast bounty
  python main.py --forecast --dry   # build only, print, submit nothing
  python main.py --daily            # the cron entry: jitter -> enter -> forecast -> report
  python main.py --referral-farm N  # N fresh agent keys: bind ref + enter one bounty each
  python main.py --referral-status

Guardrail learned the hard way: never probe a write endpoint with a throwaway
payload on the real wallet — v1 accepts any `prediction` object without validation
and there is no withdraw route, so a junk probe becomes a permanent entry.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from src.api import Agent, ApiError                      # noqa: E402
from src import forecast as FC                            # noqa: E402
from src.humanize import Humanizer, mask_secrets          # noqa: E402

DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)

FORECAST_SLUGS = ["btc-tomorrow", "forecast-eth-24h", "forecast-btc-7d",
                  "forecast-gold-tomorrow", "spy-close-1dollar",
                  "forecast-nvda-3d", "signal-eth-pnl-24h"]

LINES: list[str] = []


def log(msg=""):
    print(msg)
    LINES.append(msg)


def load_cfg():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def agent_for(acc, cfg):
    return Agent(acc["private_key"], base=cfg.get("base_url", "https://aiprove.fun"), data_dir=DATA)


def primary(cfg):
    return agent_for(cfg["accounts"][0], cfg)


def ledger_path():
    return os.path.join(DATA, "submissions.json")


def load_ledger():
    if os.path.exists(ledger_path()):
        try:
            with open(ledger_path(), encoding="utf-8") as f:
                return json.load(f)
        except ValueError:
            pass
    return {"submissions": [], "referrals": []}


def save_ledger(L):
    with open(ledger_path(), "w", encoding="utf-8") as f:
        json.dump(L, f, indent=1)


# ------------------------------------------------------------------ commands
def cmd_whoami(cfg):
    ag = primary(cfg)
    me = ag.me()
    log(f"address : {ag.address}")
    log(f"handle  : {me['profile']['handle']}")
    log(f"credit  : beta={me['credit']['beta']} credits={me['credit']['credits']} used={me['credit']['used']}")
    r = me["referral"]
    log(f"referral: {r['code']} -> {r['link']} (referred {r['referredCount']})")
    slugs = {s["bountySlug"] for s in me["submissions"]}
    log(f"submissions: {len(me['submissions'])} across {len(slugs)} bounties")
    return me


def cmd_status(cfg):
    ag = primary(cfg)
    me = ag.me()
    L = load_ledger()
    log(f"=== {me['profile']['handle']} · {ag.address} ===")
    log(f"competing   : {len(me['competing'])} bounties")
    log(f"submissions : {len(me['submissions'])}")
    for s in me["submissions"]:
        pred = json.dumps(s.get("prediction"))[:70] if s.get("prediction") else ""
        log(f"  - {s['bountySlug']:26} {s.get('status','?'):16} {pred}")
    log(f"won         : {len(me['won'])} | earnings ${me['earningsUsd']}")
    log(f"referrals   : {me['referral']['referredCount']}")
    if L["referrals"]:
        ok = [r for r in L["referrals"] if r.get("entered")]
        log(f"  ledger    : {len(L['referrals'])} wallets, {len(ok)} entered a bounty")
    return me


def cmd_enter(cfg, slugs=None):
    ag = primary(cfg)
    open_b = ag.bounties(status="open")
    want = set(slugs) if slugs else None
    done = 0
    for b in open_b:
        if want and b["slug"] not in want:
            continue
        try:
            ag.enter(b["slug"], handle=cfg.get("handle"))
            log(f"  + entered {b['slug']}")
            done += 1
        except ApiError as e:
            if e.status in (200, 409) or "already" in e.body.lower():
                log(f"  = already in {b['slug']}")
            else:
                log(f"  ! {b['slug']}: {e.status} {e.body[:120]}")
    log(f"entered {done} new bounties")
    return done


def _submitted_today(slug: str) -> bool:
    """One forecast per bounty per UTC day.

    The submit endpoint has no withdraw route and accepts duplicates, so a naive
    24h cron accumulates entries (btc-tomorrow reached four on the first day). The
    bounty instance itself is one per day, so one submission per slug per UTC date
    is both enough and the shape a human competitor has.
    """
    today = time.strftime("%Y-%m-%d", time.gmtime())
    for s in load_ledger().get("submissions", []):
        if s.get("slug") != slug:
            continue
        if time.strftime("%Y-%m-%d", time.gmtime(s.get("ts", 0) / 1000)) == today:
            return True
    return False


def cmd_forecast(cfg, dry=False, hz=None, jitter=True):
    ag = primary(cfg)
    H = Humanizer(cfg.get("humanize"), seed=None)
    ok = 0
    for slug in FORECAST_SLUGS:
        try:
            plan = FC.plan_for(slug)
            if not plan:
                continue
            if not dry and _submitted_today(slug):
                log(f"  = {slug:26} sudah submit hari ini, skip")
                continue
            kind, sym, h = plan
            if kind in ("brier-crypto", "direction-eth"):
                bars = FC.binance_daily(sym)
                r = FC.build(slug, bars=bars)
            else:
                bars, meta = FC.yahoo_daily(sym)
                r = FC.build(slug, bars=bars, meta=meta)
            payload = {"prediction": r["prediction"], "handle": cfg.get("handle")}
            log(f"  {slug:26} {json.dumps(r['prediction'])[:58]:60}  [{r['reason'][:70]}]")
            if dry:
                continue
            res = ag.submit(slug, payload)
            ok += 1
            L = load_ledger()
            L["submissions"].append({"ts": int(time.time() * 1000), "slug": slug,
                                     "prediction": r["prediction"], "id": res.get("id"),
                                     "status": res.get("status")})
            save_ledger(L)
            if jitter and slug != FORECAST_SLUGS[-1]:
                H.sleep_gap(0.4, log=lambda m: None)
        except ApiError as e:
            log(f"  ! {slug}: {e.status} {e.body[:150]}")
        except Exception as e:                       # noqa: BLE001
            log(f"  ! {slug}: {type(e).__name__} {e}")
    if not dry:
        log(f"submitted {ok} forecasts")
    return ok


def cmd_referral_farm(cfg, n: int, code=None, dry=False):
    """Fresh agent keypairs: bind to our referral code, enter one bounty each.

    The bounty counts a referral once the referred agent enters at least one
    bounty with its own keypair — so an entry is mandatory, and the entry has to
    be varied or the on-chain pattern is N identical actions in one minute.
    """
    from eth_account import Account
    code = code or cfg["referral_code"]
    H = Humanizer(cfg.get("humanize"), seed=None)
    pool_path = os.path.join(DATA, "referral-wallets.json")
    pool = json.load(open(pool_path, encoding="utf-8")) if os.path.exists(pool_path) else []
    known = {w["address"].lower() for w in pool}

    # diversify: spread entries across different bounties instead of all-in-one
    open_b = primary(cfg).bounties(status="open")
    spread = [b["slug"] for b in open_b if b["lane"] == "AutoGrade"] or [b["slug"] for b in open_b]

    made = 0
    while made < n:
        acct = Account.create()
        if acct.address.lower() in known:
            continue
        rec = {"address": acct.address, "private_key": acct.key.hex(), "handle": H.handle(),
               "bounty": spread[(len(pool) + made) % len(spread)],
               "entered": False, "createdAt": int(time.time() * 1000)}
        if dry:
            log(f"  [dry] {rec['handle']:16} {rec['address'][:10]}… -> {rec['bounty']}")
            made += 1
            continue
        ag = Agent("0x" + acct.key.hex().replace("0x", ""), base=cfg.get("base_url", "https://aiprove.fun"),
                   data_dir=DATA, ua=H.pick(["python-urllib/3.12 aiprove-agent",
                                             "aiprove-agent/1.0 (python-urllib)",
                                             "Mozilla/5.0 (compatible; aiprove-agent)"]))
        try:
            ag.save_profile({"handle": rec["handle"], "socials": {}})
            bind = ag.claim_referral(code)
            ent = ag.enter(rec["bounty"], handle=rec["handle"])
            rec["entered"] = True
            rec["referredBy"] = bind.get("referredBy")
            log(f"  + {rec['handle']:16} {acct.address[:10]}…  ref={bind.get('referredBy','')[:10]}…  "
                f"entered {rec['bounty']}")
        except ApiError as e:
            rec["error"] = f"{e.status} {e.body[:120]}"
            log(f"  ! {rec['handle']:16} {acct.address[:10]}…  {rec['error']}")
        pool.append(rec)
        json.dump(pool, open(pool_path, "w", encoding="utf-8"), indent=1)
        known.add(acct.address.lower())
        made += 1
        if made < n:
            H.account_stagger_wait(log=lambda m: None)
    L = load_ledger()
    L["referrals"] = pool
    save_ledger(L)
    log(f"referral pool: {len(pool)} wallets, {sum(1 for w in pool if w.get('entered'))} entered")
    return pool


def cmd_referral_status(cfg):
    ag = primary(cfg)
    me = ag.me()
    r = me["referral"]
    log(f"referral code {r['code']} · verified count {r['referredCount']}")
    for i, w in enumerate(r.get("referred", []), 1):
        log(f"  {i}. {w}")
    p = os.path.join(DATA, "referral-wallets.json")
    if os.path.exists(p):
        pool = json.load(open(p, encoding="utf-8"))
        log(f"local pool: {len(pool)} wallets, {sum(1 for w in pool if w.get('entered'))} entered")
    return me


def write_run_files():
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(os.path.join(DATA, "last-run.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(LINES) + "\n")
    with open(os.path.join(DATA, "last-run.json"), "w", encoding="utf-8") as f:
        json.dump({"ts": ts, "lines": LINES}, f, indent=1)
    open(os.path.join(DATA, "last-run.ts"), "w").write(ts)


def cmd_daily(cfg, no_jitter=False):
    H = Humanizer(cfg.get("humanize"), seed=None)
    log(f"AIProve daily · {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if not no_jitter:
        H.run_jitter(log)
    try:
        me = cmd_whoami(cfg)
        log("")
        cmd_enter(cfg)
        log("")
        cmd_forecast(cfg, jitter=not no_jitter)
        log("")
        cmd_referral_status(cfg)
    except ApiError as e:
        log(f"API ERROR {e.status} {e.body[:200]}")
    finally:
        write_run_files()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--whoami", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--enter", action="store_true")
    p.add_argument("--forecast", action="store_true")
    p.add_argument("--dry", action="store_true")
    p.add_argument("--daily", action="store_true")
    p.add_argument("--no-jitter", action="store_true")
    p.add_argument("--referral-farm", type=int, default=0)
    p.add_argument("--referral-status", action="store_true")
    p.add_argument("--slugs", nargs="*")
    a = p.parse_args()
    cfg = load_cfg()

    if a.whoami:
        cmd_whoami(cfg)
    elif a.status:
        cmd_status(cfg)
    elif a.enter:
        cmd_enter(cfg, a.slugs)
    elif a.forecast:
        cmd_forecast(cfg, dry=a.dry, jitter=not a.no_jitter)
        write_run_files()
    elif a.daily:
        cmd_daily(cfg, no_jitter=a.no_jitter)
    elif a.referral_farm:
        cmd_referral_farm(cfg, a.referral_farm, dry=a.dry)
    elif a.referral_status:
        cmd_referral_status(cfg)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
