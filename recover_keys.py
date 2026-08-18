#!/usr/bin/env python3
"""Récupère les clés eToro depuis les anciens journaux de session Claude.

Les clés ne vivent nulle part ailleurs sur cette machine: pas dans .env, pas
dans le trousseau, pas dans le profil shell. Seules copies locales = les
transcripts .jsonl des sessions Claude du projet Money.

Ce script les retrouve et les écrit dans .env, à côté de OPENAI_API_KEY.
Il n'affiche JAMAIS la valeur en clair — seulement un aperçu masqué.

    python3 recover_keys.py            # cherche et montre ce qu'il a trouvé (masqué)
    python3 recover_keys.py --write    # écrit réellement dans .env
"""
import pathlib
import re
import sys

TRANSCRIPTS = pathlib.Path.home() / ".claude" / "projects"
ENV = pathlib.Path(__file__).with_name(".env")

# Un même nom peut apparaître sous plusieurs formes selon qu'il a été collé
# en shell (KEY=valeur), en JSON ({"KEY": "valeur"}) ou en en-tête HTTP.
PATTERNS = {
    "ETORO_PUBLIC_KEY": [
        r'ETORO_PUBLIC_KEY["\']?\s*[:=]\s*["\']?([A-Za-z0-9_\-.]{8,})',
        r'x-api-key["\']?\s*[:=]\s*["\']?([A-Za-z0-9_\-.]{8,})',
    ],
    "ETORO_PRIVATE_KEY": [
        # Le jeton agent-portfolio eToro est un JSON base64url qui commence
        # TOUJOURS par eyJjaSI6 ({"ci":…) et fait ~260 caractères. Les
        # transcripts contiennent des dizaines d'autres jetons eyJ… (Google,
        # Notion, Supabase…) — sans ce préfixe on en attrape un au hasard et
        # l'API répond Unauthorized.
        r'\b(eyJjaSI6[A-Za-z0-9_\-.]{60,})',
        r'ETORO_PRIVATE_KEY["\']?\s*[:=]\s*["\']?(eyJjaSI6[A-Za-z0-9_\-.]{60,})',
        r'x-user-key["\']?\s*[:=]\s*["\']?(eyJjaSI6[A-Za-z0-9_\-.]{60,})',
    ],
}
# À motif égal, garder le jeton le PLUS LONG (les transcripts contiennent des
# versions tronquées « eyJjaSI6…" » dans les logs d'erreur).
PREFER_LONGEST = {"ETORO_PRIVATE_KEY"}
# Faux positifs à écarter: noms de variables, placeholders, exemples.
REJECT = re.compile(r"^(ETORO|YOUR|EXEMPLE|EXAMPLE|xxx|\.\.\.|<)", re.I)


def mask(v):
    return f"{v[:6]}…{v[-4:]} ({len(v)} car.)"


def scan():
    found = {}
    files = sorted(TRANSCRIPTS.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        sys.exit(f"Aucun transcript trouvé sous {TRANSCRIPTS}")
    for path in files:
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for name, patterns in PATTERNS.items():
            if name in found:
                continue
            hits = []
            for pat in patterns:
                hits += [m.group(1) for m in re.finditer(pat, text)
                         if not REJECT.match(m.group(1))]
            if not hits:
                continue
            val = max(hits, key=len) if name in PREFER_LONGEST else hits[0]
            found[name] = val
            print(f"{name}: trouvé dans {path.name} → {mask(val)}")
        if len(found) == len(PATTERNS):
            break
    return found


def write(found):
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    kept = [l for l in lines if not any(l.startswith(k + "=") for k in found)]
    kept += [f"{k}={v}" for k, v in found.items()]
    ENV.write_text("\n".join(kept) + "\n")
    ENV.chmod(0o600)
    print(f"\nÉcrit dans {ENV} (chmod 600). close_all.py peut maintenant tourner.")


if __name__ == "__main__":
    got = scan()
    missing = [k for k in PATTERNS if k not in got]
    if missing:
        print(f"\nIntrouvable: {', '.join(missing)}.")
        print("Régénère un token depuis eToro (Agent Portfolio → paramètres) "
              "et ajoute-le à la main dans .env.")
    if got and "--write" in sys.argv:
        write(got)
    elif got:
        print("\nRelance avec --write pour les écrire dans .env.")
