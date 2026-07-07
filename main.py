"""Un cycle de trading par invocation — le cron (GitHub Actions) planifie.

Ordre: verrou → lectures (/pnl + portefeuille) → halte? (fermetures SEULEMENT,
jamais de retour anticipé sans avoir tenté de fermer) → validité de l'équité
(illisible = fin de cycle, JAMAIS une liquidation) → plancher confirmé sur
2 cycles / disjoncteur (AVANT le cerveau) → cerveau → gate → exécution →
suivi (tracker). DRY_RUN=true (défaut): tout est loggé, rien n'est exécuté.
"""
import datetime as dt
import json
import math
import os
import sys
import traceback

import yaml

import brain as brain_module
import marketdata
import tracker
from etoro_client import (AmbiguousWriteError, EtoroClient, EtoroError,
                          extract_instrument_id, extract_rate)
from risk_gate import RiskGate, _coerce_int

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path=None):
    """Mini-chargeur .env (pas de dépendance python-dotenv).

    Gère `export KEY=...` et les commentaires en fin de ligne sur les
    valeurs non guillemetées (`KEY=val # commentaire`).
    """
    path = path or os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if value[:1] in ("'", '"'):
                value = value.strip("'\"")
            else:
                value = value.split(" #", 1)[0].strip()
            if key:
                os.environ.setdefault(key, value)


def log_jsonl(record):
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    record = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(), **record}
    with open(os.path.join(HERE, "logs", "trades.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _safe_hold(note):
    hold = json.loads(json.dumps(brain_module.SAFE_HOLD))
    hold["market_note"] = str(note)[:400]
    return hold


def _read_equity(portfolio):
    """(total_value|None, cash, totals). Validité STRICTE: accountTotals présent
    et accountTotalValue float fini > 0 — sinon total_value=None (= illisible,
    jamais 0: un zéro fantôme déclencherait la liquidation plancher)."""
    totals = portfolio.get("accountTotals") if isinstance(portfolio, dict) else None
    if not isinstance(totals, dict):
        return None, 0.0, {}
    try:
        total_value = float(totals.get("accountTotalValue"))
    except (TypeError, ValueError):
        return None, 0.0, totals
    if not math.isfinite(total_value) or total_value <= 0:
        return None, 0.0, totals
    try:
        cash = float(totals.get("accountAvailableCash"))
    except (TypeError, ValueError):
        cash = 0.0
    if not math.isfinite(cash) or cash < 0:
        cash = 0.0  # cash illisible → 0: bloque les ouvertures, jamais les fermetures
    return total_value, cash, totals


def _totals_field(totals, candidates):
    if not isinstance(totals, dict):
        return None
    low = {str(k).lower(): v for k, v in totals.items()}
    for c in candidates:
        try:
            f = float(low.get(c.lower()))
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            return f
    return None


def _position_pnl(pos):
    """PnL courant d'une entrée /pnl (proxy du PnL réalisé à la fermeture)."""
    if not isinstance(pos, dict):
        return None
    low = {str(k).lower(): v for k, v in pos.items()}
    for k in ("netprofit", "profit", "unrealizedpnl", "pnl", "totalprofit"):
        try:
            f = float(low.get(k))
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            return f
    return None


def _sweep_close(client, positions, dry_run, event, cycle_id, trade_records):
    """Ferme toutes les positions restantes (halte ou plancher).

    Une entrée malformée ou une erreur d'API sur UNE position n'interrompt
    JAMAIS le balayage des suivantes (try/except Exception + pos.get)."""
    closed = 0
    for pos in positions or []:
        status, err, pid, iid = None, None, None, None
        try:
            if not isinstance(pos, dict):
                log_jsonl({"event": f"close_skip_{event}", "position": str(pos)[:200]})
                continue
            pid, iid = pos.get("positionID"), pos.get("instrumentID")
            if pid is None or iid is None:
                log_jsonl({"event": f"close_skip_{event}", "position": pos})
                continue
            if dry_run:
                log_jsonl({"event": f"would_close_{event}", "position": pos})
                status = "dry_run"
            else:
                client.close_position(pid, iid)
                log_jsonl({"event": f"closed_{event}", "position": pos})
                status, closed = "executed", closed + 1
        except AmbiguousWriteError as exc:
            # L'ordre a PEUT-ÊTRE été exécuté: pas de rejeu, le /pnl du
            # prochain cycle est la réconciliation.
            status, err = "error", f"écriture ambiguë: {exc}"
            log_jsonl({"event": f"ambiguous_close_{event}",
                       "position": str(pos)[:300], "error": str(exc)})
        except Exception as exc:  # EtoroError, KeyError, TypeError…
            status, err = "error", str(exc)
            log_jsonl({"event": f"close_error_{event}",
                       "position": str(pos)[:300], "error": str(exc)})
        if status:
            trade_records.append(tracker.trade_record(
                cycle_id=cycle_id, a_type="close", position_id=pid, instrument_id=iid,
                status=status, gate_reason=f"sweep_{event}",
                rationale="fermeture automatique (halte/plancher)",
                pnl_usd=_position_pnl(pos), error=err))
    return closed


def _name_map():
    """Symbole eToro → nom lisible (ex "Bitcoin"/"Nvidia"), depuis la watchlist réelle."""
    return {str(sym): query for sym, _yahoo, _cls, query in marketdata.WATCHLIST}


def _rationale_map(repo_dir, trade_records=()):
    """Symbole → rationale du DERNIER 'open' de ce symbole (data/trades.jsonl +
    trades du cycle courant). Best-effort, jamais bloquant."""
    out = {}
    try:
        rows = tracker._read_jsonl(os.path.join(repo_dir, "data", "trades.jsonl"))
    except Exception:
        rows = []
    for r in list(rows) + list(trade_records or ()):
        if (isinstance(r, dict) and r.get("type") == "open"
                and r.get("symbol") and r.get("rationale")):
            out[str(r.get("symbol"))] = r.get("rationale")  # le dernier écrase
    return out


def _track(track_dir, totals, total_value, cash, positions, halted, breaker,
           dry_run, trade_records):
    """Suivi de l'expérience — un échec ici ne touche JAMAIS au trading."""
    try:
        eq = None
        if total_value is not None:
            eq = tracker.equity_record(
                equity=total_value, cash=cash,
                used_margin=_totals_field(totals, ("totalUsedMargin", "usedMargin",
                                                   "accountUsedMargin",
                                                   "accountInvestedAmount",
                                                   "investedAmount")),
                pnl_unrealized=_totals_field(totals, ("totalUnrealizedPnL",
                                                      "unrealizedPnL",
                                                      "accountUnrealizedPnL",
                                                      "netProfit")),
                n_positions=len([p for p in positions
                                 if isinstance(p, dict) and not p.get("pending")]),
                halted=halted, breaker_active=breaker, dry_run=dry_run)
        tracker.update(track_dir, equity_record=eq, trade_records=trade_records)
        # Instantané des positions ouvertes pour le dashboard (nom lisible + thèse IA).
        # Les positions réelles (non 'pending' intra-cycle) uniquement.
        real_positions = [p for p in positions
                          if isinstance(p, dict) and not p.get("pending")]
        tracker.write_positions(track_dir, real_positions, _name_map(),
                                _rationale_map(track_dir, trade_records))
    except Exception:
        log_jsonl({"event": "tracker_error", "error": traceback.format_exc()[-600:]})


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


def _cycle(config, gate, dry_run, client=None, brain_factory=None, track_dir=None):
    track_dir = track_dir or HERE
    cycle_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trade_records = []

    client = client or EtoroClient()

    # Lectures. Les positions (/pnl) servent à la halte COMME au plancher.
    read_errors = []
    try:
        pnl = client.get_pnl() or {}
    except EtoroError as exc:
        pnl = {}
        read_errors.append(f"pnl: {exc}")
    try:
        portfolio = client.get_portfolio() or {}
    except EtoroError as exc:
        portfolio = {}
        read_errors.append(f"portfolio: {exc}")
    positions = [p for p in ((pnl.get("clientPortfolio") or {}).get("positions") or [])
                 if isinstance(p, dict)]
    total_value, cash, totals = _read_equity(portfolio)

    # 0) Halte permanente: PAS de retour anticipé sans agir — chaque cycle
    #    retente de fermer les positions restantes (fermer est toujours permis;
    #    seuls cerveau et ouvertures sont sautés).
    if gate.is_halted():
        log_jsonl({"event": "halted", "info": gate.halt_info(),
                   "n_positions": len(positions)})
        print(f"HALTE permanente ({gate.halt_info()}) — "
              f"fermetures restantes tentées: {len(positions)}. "
              "Supprimer state/halt.json pour reprendre.")
        _sweep_close(client, positions, dry_run, "halted", cycle_id, trade_records)
        _track(track_dir, totals, total_value, cash, positions,
               halted=True, breaker=False, dry_run=dry_run, trade_records=trade_records)
        return 0

    # 1) Équité illisible = fin de cycle. JAMAIS de liquidation, de halte ni de
    #    snapshot sur une lecture invalide (un 0 fantôme ≠ plancher franchi).
    if total_value is None:
        log_jsonl({"event": "read_error", "errors": read_errors,
                   "account_totals": str(totals)[:300]})
        print("Lecture d'équité invalide → cycle terminé sans aucune action.")
        return 0

    # 2) Garde-fous durs AVANT le cerveau.
    gate.daily_snapshot(total_value)
    if gate.check_hard_floor(total_value):
        if not gate.floor_breach_pending():
            # 1er cycle sous le plancher: on ARME seulement. La liquidation
            # exige une confirmation au cycle suivant (données transitoirement
            # fausses ≠ vraie casse). Pas de cerveau, pas d'ouvertures.
            gate.set_floor_breach_pending(total_value)
            log_jsonl({"event": "floor_breach_pending", "total_value": total_value})
            print(f"Plancher franchi ({total_value:.2f} $) — confirmation requise "
                  "au prochain cycle avant liquidation.")
            _track(track_dir, totals, total_value, cash, positions,
                   halted=False, breaker=False, dry_run=dry_run,
                   trade_records=trade_records)
            return 0
        # Confirmé sur 2 cycles consécutifs: halte D'ABORD (elle ne doit jamais
        # dépendre du succès des fermetures), PUIS balayage des fermetures.
        gate.set_halt(f"Plancher franchi (confirmé 2 cycles): "
                      f"{total_value:.2f} $ < {gate.hard_floor_usd:.0f} $")
        _sweep_close(client, positions, dry_run, "floor", cycle_id, trade_records)
        log_jsonl({"event": "hard_floor_halt", "total_value": total_value,
                   "dry_run": dry_run})
        print("PLANCHER FRANCHI (confirmé) → halte permanente + fermetures.")
        _track(track_dir, totals, total_value, cash, positions,
               halted=True, breaker=False, dry_run=dry_run,
               trade_records=trade_records)
        return 0
    gate.clear_floor_breach_pending()  # lecture saine → le compteur repart
    breaker = gate.circuit_breaker_active(total_value)

    # 3) Cerveau (recherche web + décision). Échec de CONSTRUCTION ou d'appel
    #    → hold, jamais de crash de cycle.
    # Données de marché CALCULÉES (momentum + volatilité réelles) — best-effort,
    # jamais bloquant. Ancre la direction/le sizing dans des chiffres, pas du texte.
    try:
        market_data = marketdata.market_snapshot(state_dir=os.path.join(HERE, "state"))
    except Exception:
        market_data = []
        log_jsonl({"event": "marketdata_error", "error": traceback.format_exc()[-400:]})
    try:
        b = (brain_factory() if brain_factory is not None
             else brain_module.Brain(config, state_dir=os.path.join(HERE, "state")))
        decision = b.decide({
            "accountTotals": totals,
            "positions": positions,
            "circuit_breaker_active": breaker,
            "halt_history": gate.halt_info(),
            # Table de momentum/volatilité CALCULÉS par instrument (vérité chiffrée).
            "market_data": market_data,
            # Mémoire courte (self-learning étage 1): le cerveau voit ses trades
            # récemment fermés (thèse vs résultat) pour apprendre in-context.
            "recent_closed_trades": _recent_closed_trades(track_dir),
            "dry_run": dry_run,
        })
    except Exception as exc:
        decision = _safe_hold(f"cerveau indisponible: {exc}")
    if not isinstance(decision, dict):
        decision = _safe_hold("décision illisible")
    market_note = str(decision.get("market_note") or "")[:400]
    actions = decision.get("actions") if isinstance(decision.get("actions"), list) else []
    log_jsonl({"event": "brain_decision", "market_note": market_note,
               "n_actions": len(actions)})

    # 4) Gate déterministe + exécution. Chaque action est isolée: une action
    #    pourrie ne bloque JAMAIS les fermetures en file derrière elle.
    ctx = {"total_value": total_value, "cash": cash, "positions": positions,
           "breaker": breaker, "dry_run": dry_run, "cycle_id": cycle_id,
           "market_note": market_note, "trade_records": trade_records}
    executed = 0
    for action in actions:
        if not isinstance(action, dict):
            continue
        try:
            executed += _handle_action(client, gate, action, ctx)
        except Exception:
            log_jsonl({"event": "action_error", "action": str(action)[:400],
                       "error": traceback.format_exc()[-800:]})

    # 5) Suivi de l'expérience (jamais bloquant).
    _track(track_dir, totals, total_value, ctx["cash"], positions,
           halted=False, breaker=breaker, dry_run=dry_run,
           trade_records=trade_records)
    log_jsonl({"event": "cycle_summary", "total_value": total_value,
               "cash": ctx["cash"], "open_positions": len(positions),
               "breaker_active": breaker, "executed": executed, "dry_run": dry_run})
    print(f"Cycle OK — valeur {total_value:.2f} $, exécutés: {executed}, dry_run={dry_run}")
    return 0


def _handle_action(client, gate, action, ctx):
    """Une action du cerveau: résolution d'instrument → gate → exécution.

    Retourne 1 si une écriture réelle a été exécutée, sinon 0."""
    a_type = str(action.get("type") or "hold").strip().lower()
    symbol = str(action.get("symbol") or "").strip()
    instrument, entry_rate = None, None

    if a_type == "open":
        # Résolution AVANT le gate: identité vérifiée par le gate lui-même.
        query = str(action.get("instrument_query") or symbol or "").strip()
        if query:  # requête vide → instrument None → rejet par le gate
            try:
                instrument = client.search_instrument(query)
            except Exception as exc:
                log_jsonl({"event": "search_error", "query": query, "error": str(exc)})
        is_buy = action.get("is_buy")
        # Prix vivant du bon côté du carnet (achat → ask, vente → bid).
        entry_rate = extract_rate(instrument, is_buy if isinstance(is_buy, bool) else None)
    elif a_type == "close":
        # Repli fermeture-par-symbole: si le position_id ne matche rien, on
        # résout l'instrument pour que le gate puisse fermer tout le symbole.
        pid = _coerce_int(action.get("position_id"))
        known = pid is not None and any(
            _coerce_int(p.get("positionID")) == pid
            for p in ctx["positions"] if isinstance(p, dict))
        if not known and symbol:
            try:
                instrument = client.search_instrument(symbol)
            except Exception as exc:
                log_jsonl({"event": "search_error", "query": symbol, "error": str(exc)})

    approved, reason = gate.evaluate(
        action, ctx["total_value"], ctx["cash"], ctx["positions"],
        entry_rate=entry_rate, breaker_active=ctx["breaker"], instrument=instrument)

    record = {"event": "action", "proposed": action, "approved": approved,
              "reason": reason, "dry_run": ctx["dry_run"]}
    status = "hold" if reason == "hold" else "rejected"
    error, executed, iid, pnl_usd = None, 0, None, None

    if approved and approved["type"] == "open":
        iid = extract_instrument_id(instrument)
        # Comptabilité intra-cycle: plafond de positions + cash suivis localement.
        ctx["positions"].append({"positionID": None, "instrumentID": iid,
                                 "pending": True})
        ctx["cash"] -= approved["amount_usd"]
        # Anti-churn: compte aussi en dry-run (budget réaliste en simulation).
        gate.record_open(approved["symbol"], instrument_id=iid)
        if iid is None:
            error, status = "instrument introuvable (pas d'ID)", "error"
        elif ctx["dry_run"]:
            status = "dry_run"
        else:
            try:
                record["result"] = client.open_position(
                    iid, approved["is_buy"], approved["leverage"],
                    approved["amount_usd"],
                    stop_loss_rate=approved["stop_loss_rate"],
                    take_profit_rate=approved["take_profit_rate"])
                status, executed = "executed", 1
            except AmbiguousWriteError as exc:
                # PAS de rejeu: l'ordre a peut-être été exécuté. Le /pnl du
                # prochain cycle est la réconciliation.
                error, status = f"écriture ambiguë: {exc}", "error"
                log_jsonl({"event": "ambiguous_write", "action": approved,
                           "error": str(exc)})
            except EtoroError as exc:  # marché fermé, rejet… on continue
                error, status = str(exc), "error"

    elif approved and approved["type"] == "close":
        closes = approved.get("closes") or [{"position_id": approved.get("position_id"),
                                             "instrument_id": approved.get("instrument_id")}]
        pnl_usd = _closes_pnl(closes, ctx["positions"])
        if ctx["dry_run"]:
            status = "dry_run"
        else:
            ok = 0
            for c in closes:
                try:
                    record.setdefault("results", []).append(
                        client.close_position(c["position_id"], c["instrument_id"]))
                    ok += 1
                except AmbiguousWriteError as exc:
                    error = f"écriture ambiguë: {exc}"
                    log_jsonl({"event": "ambiguous_write", "action": approved,
                               "error": str(exc)})
                except EtoroError as exc:
                    error = str(exc)
            status = "executed" if ok else "error"
            executed = 1 if ok else 0

    log_jsonl(record)
    ctx["trade_records"].append(tracker.trade_record(
        cycle_id=ctx["cycle_id"], a_type=a_type, symbol=symbol or None,
        instrument_id=iid if a_type == "open" else (approved or {}).get("instrument_id"),
        is_buy=(approved or {}).get("is_buy", action.get("is_buy")),
        leverage=(approved or {}).get("leverage"),
        amount_usd=(approved or {}).get("amount_usd"),
        sl_rate=(approved or {}).get("stop_loss_rate"),
        tp_rate=(approved or {}).get("take_profit_rate"),
        position_id=(approved or {}).get("position_id", action.get("position_id")),
        status=status, gate_reason=reason,
        rationale=str(action.get("rationale") or "")[:400],
        market_note=ctx["market_note"], pnl_usd=pnl_usd, error=error))
    return executed


def _recent_closed_trades(repo_dir, n=8):
    """Derniers trades fermés (thèse + résultat PnL) pour la mémoire courte du
    cerveau. Lecture best-effort de data/trades.jsonl; jamais bloquant."""
    try:
        rows = tracker._read_jsonl(os.path.join(repo_dir, "data", "trades.jsonl"))
    except Exception:
        return []
    closed = [r for r in rows if isinstance(r, dict) and r.get("type") == "close"
              and r.get("status") in ("executed", "dry_run")]
    out = []
    for r in closed[-n:]:
        out.append({"ts": r.get("ts"), "symbol": r.get("symbol"),
                    "pnl_usd": r.get("pnl_usd"), "rationale": r.get("rationale")})
    return out


def _closes_pnl(closes, positions):
    """Somme des PnL courants des positions fermées (proxy du PnL réalisé)."""
    total, found = 0.0, False
    for c in closes:
        pid = _coerce_int(c.get("position_id"))
        for p in positions:
            if isinstance(p, dict) and _coerce_int(p.get("positionID")) == pid:
                v = _position_pnl(p)
                if v is not None:
                    total, found = total + v, True
    return round(total, 2) if found else None


if __name__ == "__main__":
    sys.exit(run_cycle())
