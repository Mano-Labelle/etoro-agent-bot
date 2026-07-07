"""Suivi de l'expérience — data/*.jsonl (append-only) + PERFORMANCE.md régénéré.

Appelé en FIN de chaque cycle par main.py, dans un try/except: un échec de
suivi ne doit JAMAIS affecter le trading. Aucune dépendance hors stdlib.

- data/equity.jsonl : une ligne par cycle (équité, cash, positions, drapeaux).
- data/trades.jsonl : une ligne par décision/action (approuvée ou non).
- data/targets.json : plancher / objectifs / cliquets (remplis plus tard,
  après calibration Monte Carlo — le dashboard tolère les listes vides).
- PERFORMANCE.md    : rapport lisible régénéré à chaque cycle (en français).
"""
import datetime as dt
import json
import math
import os

T0_ISO = "2026-07-02T16:59Z"
T0 = dt.datetime(2026, 7, 2, 16, 59, tzinfo=dt.timezone.utc)
START_EQUITY = 10000.0
HARD_FLOOR = 3500.0
EUR_ANCHOR = 200.0        # mise réelle en € (le book virtuel de 10 000 $ la reflète)
SPREAD_COST_RATE = 0.001  # proxy de coût: 0,1 % du notionnel par trade

SPARK_CHARS = "▁▂▃▄▅▆▇█"

DEFAULT_TARGETS = {"start_equity": START_EQUITY, "hard_floor": HARD_FLOOR,
                   "eur_anchor": EUR_ANCHOR, "targets": [], "ratchets": []}


def usd_to_eur(usd, start_equity=START_EQUITY, eur_anchor=EUR_ANCHOR):
    """Convertit une valeur du book virtuel ($) en € RÉELS pour l'utilisateur.

    Ancre: book de départ (10 000 $) = mise réelle (200 €). C'est l'échelle
    mentale demandée — on raisonne toujours en euros réels."""
    v = _num(usd)
    if v is None or not start_equity:
        return None
    return v / float(start_equity) * float(eur_anchor)


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _num(v):
    """float fini ou None — les non-finis ne rentrent jamais dans les données."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _parse_iso(s):
    """Datetime UTC depuis un ISO-8601 OU un epoch (s/ms), ou None. Tolérant."""
    if s is None:
        return None
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        try:
            v = float(s)
            if v > 1e12:          # heuristique: millisecondes
                v /= 1000.0
            return dt.datetime.fromtimestamp(v, tz=dt.timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    try:
        txt = str(s).strip().replace("Z", "+00:00")
        t = dt.datetime.fromisoformat(txt)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _pos_get(low, *candidates):
    """Première valeur non-nulle parmi des clés candidates (dict déjà en minuscules)."""
    for c in candidates:
        v = low.get(c.lower())
        if v is not None:
            return v
    return None


# ---- Construction des enregistrements (schéma centralisé ici) ----
def equity_record(equity, cash, used_margin=None, pnl_unrealized=None,
                  n_positions=0, halted=False, breaker_active=False, dry_run=True):
    return {"ts": _now_iso(), "equity": _num(equity), "cash": _num(cash),
            "used_margin": _num(used_margin), "pnl_unrealized": _num(pnl_unrealized),
            "n_positions": int(n_positions or 0), "halted": bool(halted),
            "breaker_active": bool(breaker_active), "dry_run": bool(dry_run)}


def trade_record(cycle_id, a_type, symbol=None, instrument_id=None, is_buy=None,
                 leverage=None, amount_usd=None, sl_rate=None, tp_rate=None,
                 position_id=None, status="rejected", gate_reason="", rationale="",
                 market_note="", pnl_usd=None, error=None):
    """status ∈ {executed, dry_run, rejected, error, hold}.
    pnl_usd (fermetures): PnL de la position au moment de la fermeture,
    estimé depuis /pnl — proxy honnête du PnL réalisé."""
    return {"ts": _now_iso(), "cycle_id": str(cycle_id), "type": str(a_type),
            "symbol": str(symbol) if symbol is not None else None,
            "instrument_id": instrument_id,
            "is_buy": is_buy if isinstance(is_buy, bool) else None,
            "leverage": leverage, "amount_usd": _num(amount_usd),
            "sl_rate": _num(sl_rate), "tp_rate": _num(tp_rate),
            "position_id": position_id, "status": str(status),
            "gate_reason": str(gate_reason)[:300],
            "rationale": str(rationale)[:400],
            "market_note": str(market_note)[:400],
            "pnl_usd": _num(pnl_usd),
            "error": str(error)[:300] if error else None}


# ---- I/O ----
def _append_jsonl(path, record):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _read_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue  # ligne corrompue → ignorée, jamais bloquant
    except OSError:
        pass
    return rows


def load_targets(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    out = dict(DEFAULT_TARGETS)
    if isinstance(data, dict):
        out.update({k: v for k, v in data.items() if v is not None})
    if not isinstance(out.get("targets"), list):
        out["targets"] = []
    if not isinstance(out.get("ratchets"), list):
        out["ratchets"] = []
    return out


# ---- Métriques (mêmes définitions que le dashboard, calcul côté client) ----
def compute_metrics(equity_rows, trades, start_equity=START_EQUITY, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    equities = [start_equity]
    for r in equity_rows:
        v = _num(r.get("equity")) if isinstance(r, dict) else None
        if v is not None:
            equities.append(v)
    current = equities[-1]
    peak, max_dd = equities[0], 0.0
    for v in equities:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100.0)

    done = [t for t in trades if isinstance(t, dict)
            and t.get("status") in ("executed", "dry_run")
            and t.get("type") in ("open", "close")]
    closes = [t for t in done if t.get("type") == "close"
              and _num(t.get("pnl_usd")) is not None]
    wins = [float(t["pnl_usd"]) for t in closes if float(t["pnl_usd"]) > 0]
    losses = [float(t["pnl_usd"]) for t in closes if float(t["pnl_usd"]) <= 0]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = abs(sum(losses) / len(losses)) if losses else None
    costs = sum(abs(t["amount_usd"]) for t in done
                if _num(t.get("amount_usd")) is not None) * SPREAD_COST_RATE
    return {
        "equity": current,
        "return_pct": (current / start_equity - 1.0) * 100.0 if start_equity else 0.0,
        "max_drawdown_pct": max_dd,
        "days_elapsed": max(0.0, (now - T0).total_seconds() / 86400.0),
        "n_trades": len(done),
        "n_closes_pnl": len(closes),
        "hit_rate_pct": (len(wins) / len(closes) * 100.0) if closes else None,
        "avg_win_usd": avg_win,
        "avg_loss_usd": avg_loss,
        "win_loss_ratio": (avg_win / avg_loss) if avg_win and avg_loss else None,
        "expectancy_usd": (sum(wins) + sum(losses)) / len(closes) if closes else None,
        "gross_costs_usd": costs,
    }


def sparkline(values, width=48):
    vals = [v for v in (_num(x) for x in values) if v is not None]
    if not vals:
        return "(pas encore de données)"
    if len(vals) > width:  # sous-échantillonnage régulier
        step = len(vals) / float(width)
        vals = [vals[min(int(i * step), len(vals) - 1)] for i in range(width)]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return SPARK_CHARS[0] * len(vals)
    return "".join(SPARK_CHARS[int((v - lo) / (hi - lo) * (len(SPARK_CHARS) - 1))]
                   for v in vals)


def _fmt(v, suffix="", digits=2):
    return "—" if v is None else f"{v:.{digits}f}{suffix}"


def render_performance(equity_rows, trades, targets, now=None):
    """Rapport PERFORMANCE.md (français), régénéré intégralement à chaque cycle."""
    start_equity = _num(targets.get("start_equity")) or START_EQUITY
    eur_anchor = _num(targets.get("eur_anchor")) or EUR_ANCHOR
    m = compute_metrics(equity_rows, trades, start_equity=start_equity, now=now)

    def eur(usd):
        return usd_to_eur(usd, start_equity, eur_anchor)

    def eur_str(usd, digits=0):
        v = eur(usd)
        return "—" if v is None else f"{v:,.{digits}f} €"

    last = equity_rows[-1] if equity_rows else {}
    flags = []
    if isinstance(last, dict):
        if last.get("halted"):
            flags.append("HALTE")
        if last.get("breaker_active"):
            flags.append("disjoncteur")
        if last.get("dry_run"):
            flags.append("dry-run")
    equities = [r.get("equity") for r in equity_rows if isinstance(r, dict)]

    lines = [
        "# 📈 Expérience eToro — 200 € Bold Bets",
        "",
        "_Régénéré automatiquement à chaque cycle par `tracker.py` — ne pas éditer à la main._",
        "",
        f"- **T0** : {T0_ISO} — mise réelle **{eur_anchor:,.0f} €** "
        f"(book virtuel de {start_equity:,.0f} $ répliqué à ~2 %)",
        f"- **Valeur actuelle** : **{eur_str(m['equity'], 2)}** "
        f"({m['return_pct']:+.2f} %) — _book {m['equity']:,.0f} $_",
        f"- **Drawdown max** : {m['max_drawdown_pct']:.2f} %",
        f"- **Jours écoulés** : {m['days_elapsed']:.1f}",
        f"- **Dernier point** : {last.get('ts', '—')}"
        + (f" ({', '.join(flags)})" if flags else ""),
        "",
        "## Courbe d'équité",
        "",
        "```",
        sparkline(equities),
        "```",
        (f"min {min(v for v in (equities or [start_equity]) if v is not None):,.0f} $ — "
         f"max {max(v for v in (equities or [start_equity]) if v is not None):,.0f} $"
         if any(v is not None for v in equities) else "_En attente du premier cycle._"),
        "",
        "## Métriques",
        "",
        "| Métrique | Valeur |",
        "|---|---|",
        f"| Trades (open + close, exécutés ou dry-run) | {m['n_trades']} |",
        f"| Fermetures avec PnL connu | {m['n_closes_pnl']} |",
        f"| Taux de réussite | {_fmt(m['hit_rate_pct'], ' %', 1)} |",
        f"| Gain moyen | {_fmt(m['avg_win_usd'], ' $')} |",
        f"| Perte moyenne | {_fmt(m['avg_loss_usd'], ' $')} |",
        f"| Ratio gain/perte | {_fmt(m['win_loss_ratio'], '', 2)} |",
        f"| Espérance par trade | {_fmt(m['expectancy_usd'], ' $')} |",
        f"| Coûts bruts estimés (proxy spread 0,1 %) | {m['gross_costs_usd']:.2f} $ |",
        "",
        "## 10 derniers trades",
        "",
    ]
    recent = [t for t in trades if isinstance(t, dict)
              and t.get("type") in ("open", "close")][-10:]
    if recent:
        lines += ["| Date (UTC) | Type | Symbole | Sens | Montant | Levier | Statut | PnL | Rationale |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for t in reversed(recent):
            sens = ("achat" if t.get("is_buy") else "vente") if isinstance(t.get("is_buy"), bool) else "—"
            rationale = str(t.get("rationale") or "").replace("|", "/").replace("\n", " ")[:80]
            lines.append(
                f"| {str(t.get('ts', ''))[:16]} | {t.get('type')} | {t.get('symbol') or '—'} "
                f"| {sens} | {_fmt(_num(t.get('amount_usd')), ' $', 0)} "
                f"| {t.get('leverage') or '—'} | {t.get('status')} "
                f"| {_fmt(_num(t.get('pnl_usd')), ' $')} | {rationale} |")
    else:
        lines.append("_Aucun trade pour l'instant._")

    lines += ["", "## Objectifs (en euros réels)", ""]
    floor_usd = _num(targets.get("hard_floor")) or HARD_FLOOR
    lines.append(f"- 💀 Plancher dur : **{eur_str(floor_usd)}** "
                 f"(_book {floor_usd:,.0f} $_) — halte permanente en dessous, confirmée 2 cycles")
    for tgt in (targets.get("targets") or []):
        if isinstance(tgt, dict):
            usd = _num(tgt.get("usd"))
            lab = str(tgt.get("label") or "Objectif")
            lines.append(f"- 🎯 {lab} : **{eur_str(usd)}** (_book {usd:,.0f} $_)"
                         if usd is not None else f"- 🎯 {lab}")
        else:
            lines.append(f"- 🎯 Objectif : **{eur_str(_num(tgt))}**")
    if not targets.get("targets"):
        lines.append("- 🎯 Objectifs : _à calibrer (Monte Carlo à venir)_")
    for rc in (targets.get("ratchets") or []):
        if isinstance(rc, dict):
            at, fl = _num(rc.get("at")), _num(rc.get("floor"))
            if at is not None and fl is not None:
                lines.append(f"- 🔒 Cliquet : atteindre {eur_str(at)} verrouille "
                             f"un plancher à {eur_str(fl)}")
        else:
            lines.append(f"- 🔒 Cliquet : {eur_str(_num(rc))}")
    lines += ["", "---", "",
              "Suivi web : `dashboard/index.html` (GitHub Pages) — données brutes : "
              "`data/equity.jsonl`, `data/trades.jsonl`. Source temps réel : l'app eToro.",
              ""]
    return "\n".join(lines)


# ---- Point d'entrée appelé par main.py ----
def update(repo_dir, equity_record=None, trade_records=()):
    """Ajoute les lignes JSONL du cycle puis régénère PERFORMANCE.md."""
    data_dir = os.path.join(repo_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    if equity_record:
        _append_jsonl(os.path.join(data_dir, "equity.jsonl"), equity_record)
    for t in trade_records or ():
        _append_jsonl(os.path.join(data_dir, "trades.jsonl"), t)
    equity_rows = _read_jsonl(os.path.join(data_dir, "equity.jsonl"))
    trades = _read_jsonl(os.path.join(data_dir, "trades.jsonl"))
    targets = load_targets(os.path.join(data_dir, "targets.json"))
    md = render_performance(equity_rows, trades, targets)
    with open(os.path.join(repo_dir, "PERFORMANCE.md"), "w", encoding="utf-8") as f:
        f.write(md)


def write_positions(repo_dir, positions, name_map=None, rationale_map=None, now=None):
    """Écrit un instantané des POSITIONS OUVERTES dans data/positions.json (ÉCRASÉ).

    Best-effort et jamais bloquant : toute entrée non-dict ou tout champ manquant
    est toléré. Les montants/PnL restent en $ du book virtuel (le dashboard les
    convertit en € réels, comme pour la courbe d'équité — cohérence avec l'ancre 200 €).

    - name_map     : symbole eToro (ou instrumentID) → nom lisible (watchlist), ex "Bitcoin".
    - rationale_map: symbole eToro → thèse du dernier 'open' de ce symbole.
    Chaque position produit : {symbol, name, amount_usd, entry_rate, pnl_usd, pnl_pct,
    opened_at, days_held, rationale}.
    """
    name_map = name_map or {}
    rationale_map = rationale_map or {}
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for pos in positions or ():
        if not isinstance(pos, dict):
            continue
        low = {str(k).lower(): v for k, v in pos.items()}
        symbol = _pos_get(low, "symbol", "symbolfull", "instrumentname", "instrumentdisplayname")
        iid = _pos_get(low, "instrumentid", "instrument_id")
        sym_str = str(symbol).strip() if symbol is not None else ""
        iid_str = str(iid).strip() if iid is not None else ""
        # Nom lisible : d'abord par symbole, puis par instrumentID, sinon le symbole brut.
        name = None
        for key in (sym_str, iid_str):
            if key and key in name_map:
                name = name_map[key]
                break
        amount = _num(_pos_get(low, "amount", "investedamount", "investamount",
                               "netinvestment", "value"))
        entry = _num(_pos_get(low, "openrate", "entryrate", "openprice", "rate", "avgopenrate"))
        pnl = _num(_pos_get(low, "netprofit", "profit", "unrealizedpnl", "pnl", "totalprofit"))
        pnl_pct = round(pnl / amount * 100.0, 2) if (pnl is not None and amount) else None
        opened_at = _pos_get(low, "opendatetime", "opentimestamp", "opendate",
                             "opendateutc", "created", "createddate")
        ts = _parse_iso(opened_at)
        days_held = round(max(0.0, (now - ts).total_seconds() / 86400.0), 2) if ts else None
        rationale = None
        for key in (sym_str, iid_str):
            if key and key in rationale_map:
                rationale = rationale_map[key]
                break
        out.append({
            "symbol": sym_str or (iid_str or None),
            "name": name or sym_str or (iid_str or None),
            "amount_usd": amount,
            "entry_rate": entry,
            "pnl_usd": pnl,
            "pnl_pct": pnl_pct,
            "opened_at": str(opened_at) if opened_at is not None else None,
            "days_held": days_held,
            "rationale": rationale,
        })
    data_dir = os.path.join(repo_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "positions.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, default=str)
    return out
