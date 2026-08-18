#!/usr/bin/env python3
"""Ferme TOUTES les positions ouvertes de l'Agent Portfolio, puis s'arrête.

Écrit pour solder l'expérience (2026-08-14). Ce script ne fait que fermer:
il n'ouvre jamais rien, ne consulte pas le brain, n'écrit aucun état.

Par défaut il ne fait RIEN d'autre que lister les positions. Il faut passer
--yes explicitement pour envoyer les ordres de clôture.

    python3 close_all.py           # liste seulement
    python3 close_all.py --yes     # ferme réellement

Identifiants: ETORO_PUBLIC_KEY et ETORO_PRIVATE_KEY dans l'environnement ou
dans .env (le .env local ne contient QUE la clé OpenAI — les clés eToro vivent
dans les GitHub Actions secrets du dépôt, il faut les remettre ici pour un
usage local).
"""
import os
import sys
import pathlib

from etoro_client import EtoroClient, EtoroError, AmbiguousWriteError


def load_env():
    env = pathlib.Path(__file__).with_name(".env")
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def is_weekend():
    import datetime
    return datetime.date.today().weekday() >= 5


def positions_of(pnl):
    return [p for p in ((pnl.get("clientPortfolio") or {}).get("positions") or [])
            if isinstance(p, dict)]


def main():
    load_env()
    if not os.environ.get("ETORO_PRIVATE_KEY"):
        sys.exit("ETORO_PRIVATE_KEY absente. Récupère-la dans les Actions secrets "
                 "du dépôt (ou régénère un token depuis eToro) et exporte-la:\n"
                 "  export ETORO_PUBLIC_KEY=...\n  export ETORO_PRIVATE_KEY=...")

    do_it = "--yes" in sys.argv
    client = EtoroClient()

    pnl = client.get_pnl()
    positions = positions_of(pnl)
    if not positions:
        print("Aucune position ouverte. Rien à faire.")
        return

    print(f"{len(positions)} position(s) ouverte(s):")
    for p in positions:
        print(f"  #{p.get('positionID')}  instrument {p.get('instrumentID')}  "
              f"investi {p.get('amount')}  P&L {p.get('profit')}")

    if not do_it:
        print("\nListe seulement. Relance avec --yes pour fermer réellement.")
        return

    if is_weekend():
        print("\n⚠️  On est le week-end: les actions US (dont PLTR) ne cotent pas.\n"
              "    eToro rejettera les ordres de clôture jusqu'à lundi.\n"
              "    On tente quand même, mais ne sois pas surpris.\n")

    ok, failed = 0, []
    for i, p in enumerate(positions, 1):
        pid, iid = p.get("positionID"), p.get("instrumentID")
        if pid is None or iid is None:
            failed.append((pid, "positionID/instrumentID manquant"))
            continue
        # 3 s d'espacement imposé entre écritures + jusqu'à 3 essais de 30 s:
        # sans ce message la commande a l'air figée pendant une minute.
        print(f"  [{i}/{len(positions)}] fermeture #{pid}… ", end="", flush=True)
        try:
            client.close_position(pid, iid)
            print("OK", flush=True)
            ok += 1
        except AmbiguousWriteError as exc:
            # L'ordre est peut-être passé: ne JAMAIS rejouer, revérifier après.
            print("AMBIGU", flush=True)
            failed.append((pid, f"ambigu, à vérifier: {exc}"))
        except EtoroError as exc:
            print("ÉCHEC", flush=True)
            failed.append((pid, str(exc)))

    print(f"\n{ok} fermée(s), {len(failed)} en échec.")
    for pid, err in failed:
        print(f"  #{pid}: {err}")
    if failed:
        print("Relance le script SANS --yes pour voir ce qu'il reste réellement ouvert.")


if __name__ == "__main__":
    main()
