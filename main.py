"""Un cycle de trading par invocation — le cron (GitHub Actions) planifie.

Ordre: verrou → halte? → portefeuille → plancher/disjoncteur (AVANT le cerveau,
ils tournent même si le LLM est en panne) → cerveau → gate → exécution → logs.
DRY_RUN=true (défaut): tout est loggé, rien n'est exécuté.
"""
import datetime as dt
import json
import os
import sys
import traceback

import yaml

from brain import Brain
from etoro_client import EtoroClient, EtoroError, extract_instrument_id, extract_rate
from risk_gate import RiskGate

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path=None):
    """Mini-chargeur .env (pas de dépendance python-dotenv)."""
    path = path or os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def log_jsonl(record):
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    record = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(), **record}
    with open(os.path.join(HERE, "logs", "trades.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def run_cycle():
    load_env()
    with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    gate = RiskGate(config, state_dir=os.path.join(HERE, "state"))
    dry_run = os.environ.get("DRY_RUN", "true").strip().lower() != "false"

    if not gate.acquire_lock():
        print("Verrou actif (cycle récent en cours) → sortie propre.")
        return 0
    try:
        return _cycle(config, gate, dry_run)
    except Exception:
        traceback.print_exc()
        log_jsonl({"event": "cycle_error", "error": traceback.format_exc()[-1500:]})
        return 1
    finally:
        gate.release_lock()  # jamais sauté, même en cas d'exception


def _cycle(config, gate, dry_run):
    if gate.is_halted():
        print(f"HALTE permanente: {gate.halt_info()} — supprimer state/halt.json pour reprendre.")
        log_jsonl({"event": "halted", "info": gate.halt_info()})
        return 0

    client = EtoroClient()
    portfolio = client.get_portfolio() or {}
    pnl = client.get_pnl() or {}
    totals = portfolio.get("accountTotals") or {}
    total_value = float(totals.get("accountTotalValue") or 0)
    cash = float(totals.get("accountAvailableCash") or 0)
    positions = list((pnl.get("clientPortfolio") or {}).get("positions") or [])

    # 1) Garde-fous durs AVANT le cerveau.
    gate.daily_snapshot(total_value)
    if gate.check_hard_floor(total_value):
        for pos in positions:
            if dry_run:
                log_jsonl({"event": "would_close_floor", "position": pos})
            else:
                try:
                    client.close_position(pos["positionID"], pos["instrumentID"])
                    log_jsonl({"event": "closed_floor", "position": pos})
                except EtoroError as exc:
                    log_jsonl({"event": "close_error_floor", "position": pos,
                               "error": str(exc)})
        gate.set_halt(f"Plancher franchi: {total_value:.2f} $ < {gate.hard_floor_usd:.0f} $")
        log_jsonl({"event": "hard_floor_halt", "total_value": total_value,
                   "dry_run": dry_run})
        print("PLANCHER FRANCHI → positions fermées, halte permanente.")
        return 0
    breaker = gate.circuit_breaker_active(total_value)

    # 2) Cerveau (recherche web + décision). Un échec → hold, jamais de crash.
    decision = Brain(config).decide({
        "accountTotals": totals,
        "positions": positions,
        "circuit_breaker_active": breaker,
        "halt_history": gate.halt_info(),
        "dry_run": dry_run,
    })
    log_jsonl({"event": "brain_decision",
               "market_note": decision.get("market_note", ""),
               "n_actions": len(decision.get("actions", []))})

    # 3) Gate déterministe + exécution.
    executed = 0
    for action in decision.get("actions", []):
        if not isinstance(action, dict):
            continue
        entry_rate, instrument = None, None
        if (action.get("type") or "").lower() == "open":
            try:
                instrument = client.search_instrument(
                    action.get("instrument_query") or action.get("symbol") or "")
                entry_rate = extract_rate(instrument)
            except EtoroError as exc:
                log_jsonl({"event": "search_error", "action": action, "error": str(exc)})

        approved, reason = gate.evaluate(action, total_value, cash, positions,
                                         entry_rate=entry_rate, breaker_active=breaker)
        record = {"event": "action", "proposed": action, "approved": approved,
                  "reason": reason, "dry_run": dry_run}
        if approved:
            if approved["type"] == "open":
                # Compter l'ouverture dans le plafond de positions du même cycle.
                positions.append({"positionID": None, "instrumentID": None,
                                  "pending": True})
                cash -= approved["amount_usd"]
                if not dry_run:
                    iid = extract_instrument_id(instrument)
                    if iid is None:
                        record["error"] = "instrument introuvable (pas d'ID)"
                    else:
                        try:
                            record["result"] = client.open_position(
                                iid, approved["is_buy"], approved["leverage"],
                                approved["amount_usd"],
                                stop_loss_rate=approved["stop_loss_rate"],
                                take_profit_rate=approved["take_profit_rate"])
                            executed += 1
                        except EtoroError as exc:  # marché fermé, rejet… on continue
                            record["error"] = str(exc)
            elif approved["type"] == "close" and not dry_run:
                try:
                    record["result"] = client.close_position(
                        approved["position_id"], approved["instrument_id"])
                    executed += 1
                except EtoroError as exc:
                    record["error"] = str(exc)
        log_jsonl(record)

    log_jsonl({"event": "cycle_summary", "total_value": total_value, "cash": cash,
               "open_positions": len(positions), "breaker_active": breaker,
               "executed": executed, "dry_run": dry_run})
    print(f"Cycle OK — valeur {total_value:.2f} $, exécutés: {executed}, dry_run={dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(run_cycle())
