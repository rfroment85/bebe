#!/usr/bin/env python3
"""
Test de couverture Coresignal — Employee Posts API (REST)

Objectif : savoir si l'index de posts Coresignal couvre ce qui s'est dit sur
LinkedIn à propos des plateformes agréées (facturation électronique FR)
depuis le 1er septembre 2026.

Usage :
    export CORESIGNAL_API_KEY="ta_clé"        # (Windows : set CORESIGNAL_API_KEY=ta_clé)
    pip install requests

    python coresignal_posts_test.py --dry-run   # couverture seule (~5 crédits, rien de collecté)
    python coresignal_posts_test.py             # couverture + 5 posts/requête (~5 + 25 crédits)
    python coresignal_posts_test.py --collect 20

Coûts (doc Coresignal) : 1 crédit par requête de recherche réussie,
1 crédit par post collecté. Rien n'est facturé sur une réponse d'erreur.

Commence TOUJOURS par --dry-run : les deux requêtes B0/B1 disent si un zéro
ailleurs signifie « le sujet est absent » ou « l'index n'est pas à jour ».
Sans elles, un résultat nul n'est pas interprétable.
"""
import argparse, json, os, sys, time
import requests

API_KEY = os.environ.get("CORESIGNAL_API_KEY")
if not API_KEY:
    sys.exit("Clé manquante : définis la variable d'environnement CORESIGNAL_API_KEY.")

BASE = "https://api.coresignal.com/cdapi/v2"
# Chemin réel documenté : /v2/post_employee/... — on garde l'autre en repli.
PATHS = ["post_employee", "employee_post"]
HEADERS = {"apikey": API_KEY, "accept": "application/json", "Content-Type": "application/json"}
SINCE = "2026-09-01"

# Chemin qui a fonctionné, mémorisé pour ne pas re-sonder à chaque requête.
_WORKING_PATH = None


def phrases_should(field, phrases):
    return [{"match_phrase": {field: p}} for p in phrases]


def any_of(field, phrases):
    """Bloc bool : au moins une des phrases présente dans `field`."""
    return {"bool": {"should": phrases_should(field, phrases), "minimum_should_match": 1}}


def since_range(since):
    return {"range": {"date_published": {"gte": since}}}


def q(must, sort="date_published"):
    """sort : 'date_published', 'reaction_count', ou None pour ne pas trier."""
    body = {"query": {"bool": {"must": must}}}
    if sort:
        body["sort"] = [{sort: {"order": "desc"}}]
    return body


# Vocabulaire réellement employé sur LinkedIn FR. "plateforme agréée" seule
# rate l'essentiel du corpus : le sigle PDP domine largement.
AGREEES = [
    "plateforme agréée", "plateformes agréées", "plateforme agreee",
    "plateforme de dématérialisation partenaire", "PDP immatriculée",
    "immatriculation PDP",
]
EFACTURE = [
    "facture électronique", "facturation électronique", "e-invoicing",
    "factur-x", "peppol", "Chorus Pro", "portail public de facturation",
]
# Q2 croise le sujet avec des mots de friction génériques ("migration",
# "erreur"...). Avec "e-invoicing" dans la liste, les annonces d'emploi SAP
# anglophones ("e-invoicing" + "Data Migration") satisfont les deux clauses.
# Sur Q2 uniquement, on exige donc un ancrage lexical français.
EFACTURE_FR = [
    "facture électronique", "facturation électronique", "factur-x",
    "Chorus Pro", "portail public de facturation",
]
EFACTURE_CORE = ["facture électronique", "facturation électronique", "e-invoicing"]
FRICTION = ["rejet", "rejetée", "annuaire", "migration", "bug", "bloqué", "erreur", "panne"]
TEMOINS = ["Anne Richer", "Grégoire Leclercq", "Adil Cherkaoui", "Cyrille Sautereau", "Christophe Viry"]

def build_queries(since, tri="date_published"):
    """Construit le jeu de requêtes pour une date plancher et un tri donnés."""
    return {
        # --- Baselines : à lire AVANT tout le reste ---------------------------
        # B0 : l'index contient-il quoi que ce soit depuis `since` ? Si total ≈ 0,
        # l'index n'est pas à jour et tous les zéros suivants ne prouvent rien.
        "B0_fraicheur_index": q([since_range(since)], sort=None),
        # B1 : le sujet existe-t-il dans l'index, toutes dates confondues ?
        # Sépare « sujet absent » de « fenêtre trop récente ».
        "B1_sujet_sans_date": q([any_of("article_body", EFACTURE_CORE)], sort=None),

        # B2 : volume du sujet DANS la fenêtre. B1 ignore les dates et Q1 est
        # restreint à une locution étroite : sans B2, aucun chiffre ne dit
        # l'ampleur réelle de la conversation depuis `since`.
        # Préfixe B = comptage seul, jamais collecté : --all sur ce volume
        # coûterait des milliers de crédits.
        "B2_volume_sujet_fenetre": q([since_range(since), any_of("article_body", EFACTURE)],
                                     sort=None),

        # --- Questions de fond ------------------------------------------------
        "Q1_plateforme_agreee": q([since_range(since), any_of("article_body", AGREEES)], sort=tri),
        "Q2_friction": q([
            since_range(since),
            any_of("article_body", EFACTURE_FR),
            {"bool": {"should": [{"match": {"article_body": w}} for w in FRICTION],
                      "minimum_should_match": 1}},
        ], sort=tri),
        # Les témoins remontent un peu plus haut : on veut leurs posts d'amorce.
        # Garde thématique obligatoire : `author_name` seul ne distingue pas
        # deux personnes homonymes (le run du 12/09 a ramené six posts d'un
        # universitaire casablancais portant le même nom qu'un expert du sujet).
        "Q3_auteurs_temoins": q([
            since_range("2026-08-15"),
            any_of("author_name", TEMOINS),
            any_of("article_body", AGREEES + EFACTURE),
        ], sort=tri),
    }


TRANSIENT = (500, 502, 503, 504)


def _post_once(path, body, attempts=4):
    """POST en réessayant les erreurs transitoires (503 = requête trop lourde côté serveur).

    Backoff 2s, 4s, 8s. Une réponse d'erreur n'étant pas facturée, réessayer ne coûte rien.
    """
    r = None
    for i in range(attempts):
        r = requests.post(f"{BASE}/{path}/search/es_dsl", headers=HEADERS, json=body, timeout=90)
        if r.status_code not in TRANSIENT:
            return r
        if i < attempts - 1:
            delay = 2 ** (i + 1)
            print(f"   {r.status_code} transitoire — nouvelle tentative dans {delay}s…")
            time.sleep(delay)
    return r


def post(payload):
    """POST avec repli sur l'autre chemin et sans tri si l'API refuse ou sature.

    Le repli « sans tri » change le SENS du résultat (on n'obtient plus les
    posts les plus X, mais l'ordre par défaut de l'API) : il est donc annoncé
    bruyamment et remonté à l'appelant, jamais avalé en silence.
    """
    global _WORKING_PATH
    last = None
    champ_tri = list(payload["sort"][0])[0] if payload.get("sort") else None
    paths = [_WORKING_PATH] if _WORKING_PATH else PATHS
    for p in paths:
        variantes = [(payload, False)]
        if champ_tri:
            variantes.append(({k: v for k, v in payload.items() if k != "sort"}, True))
        for body, sans_tri in variantes:
            r = _post_once(p, body)
            if r.status_code == 200:
                _WORKING_PATH = p
                if sans_tri:
                    print(f"   /!\\ tri sur '{champ_tri}' REFUSE par l'API "
                          f"({getattr(last, 'status_code', '?')}) — ordre par defaut, "
                          f"l'echantillon n'est PAS trie par {champ_tri}")
                return p, r, sans_tri
            last = r
            # 400/422 = corps refusé → retenter sans le tri.
            # 404 = mauvais chemin → passer au chemin suivant.
            # 401/403/429 = clé ou quota → inutile d'insister.
            if r.status_code in (401, 403, 429):
                return None, r, False
            if r.status_code not in (400, 422) + TRANSIENT:
                break
    return None, last, False

def search(name, payload):
    path, r, sans_tri = post(payload)
    if r is None or r.status_code != 200:
        print(f"[{name}] ERREUR {getattr(r, 'status_code', None)} : {getattr(r, 'text', '')[:300]}")
        return None, [], None, False
    try:
        ids = r.json()
    except ValueError:
        print(f"[{name}] réponse non-JSON : {r.text[:200]}")
        return path, [], None, False
    # Selon les endpoints la réponse est une liste d'ids ou un objet l'encapsulant.
    if isinstance(ids, dict):
        ids = ids.get("data") or ids.get("ids") or ids.get("results") or []
    if not isinstance(ids, list):
        print(f"[{name}] forme de réponse inattendue : {type(ids).__name__}")
        return path, [], None, False
    total = r.headers.get("x-total-results") or r.headers.get("X-Total-Results")
    print(f"[{name}] total annoncé : {total or '?'} — ids reçus : {len(ids)} (chemin {path})")
    return path, ids, total, sans_tri


def collect(path, pid, attempts=3):
    """GET un post, en réessayant les erreurs transitoires.

    Sans retry, un 503 passager fait perdre le post — et à 1 crédit l'unité
    sur une collecte de plusieurs centaines, ces trous coûtent cher à combler.
    """
    for i in range(attempts):
        r = requests.get(f"{BASE}/{path}/collect/{pid}", headers=HEADERS, timeout=60)
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                return None
        if r.status_code not in TRANSIENT or i == attempts - 1:
            print(f"   collect {pid} → {r.status_code}")
            return None
        time.sleep(2 ** (i + 1))
    return None


def deja_collectes(chemin):
    """Ids déjà présents dans le .jsonl, pour ne jamais repayer un post.

    Un post collecté coûte 1 crédit : reprendre une collecte interrompue doit
    repartir d'où elle s'est arrêtée, pas du début.
    """
    ids = set()
    if not os.path.exists(chemin):
        return ids
    with open(chemin, encoding="utf-8") as f:
        for ligne in f:
            ligne = ligne.strip()
            if not ligne:
                continue
            try:
                ids.add(str(json.loads(ligne).get("id")))
            except ValueError:
                continue
    return ids


def summarize(p):
    body = (p.get("article_body") or "").replace("\n", " ")
    date = (p.get("date_published") or "?")[:10]
    return (f"   {date} | {p.get('author_name') or '?'} | "
            f"{p.get('reaction_count', 0)} réac. / {p.get('comment_count', 0)} com. "
            f"(commentaires livrés : {len(p.get('comments') or [])}) | {body[:110]}…")


def verdict(totals, since):
    """Lecture des baselines : dit si les zéros sont interprétables."""
    def n(name):
        v = totals.get(name)
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    print("\n--- Lecture ---")
    QS = ("Q1_plateforme_agreee", "Q2_friction", "Q3_auteurs_temoins")
    fresh, topic = n("B0_fraicheur_index"), n("B1_sujet_sans_date")

    for name in QS:
        print(f"{name} : {totals.get(name, 'non exécutée')}")

    # Une baseline ne sert qu'à interpréter un zéro. Si toutes les questions
    # ont ramené des résultats, elles sont surnuméraires.
    zeros = [name for name in QS if n(name) == 0]
    errs = [name for name in QS if n(name) is None]
    manquantes = [b for b, v in (("B0", fresh), ("B1", topic)) if v is None]

    if fresh == 0:
        print(f"\nB0 = 0 : aucun post indexé depuis {since} → l'index n'est pas à jour sur cette")
        print("fenêtre. Les zéros ci-dessus ne prouvent RIEN sur ce qui s'est dit sur LinkedIn.")
    elif fresh is not None:
        print(f"\nB0 = {fresh} : l'index est alimenté depuis {since}, un zéro ci-dessus serait")
        print("donc une absence réelle et pas un défaut de fraîcheur.")
    fenetre = n("B2_volume_sujet_fenetre")
    if fenetre is not None:
        q1 = n("Q1_plateforme_agreee")
        print(f"B2 = {fenetre} : posts sur le sujet depuis {since}, toutes formulations.")
        if q1:
            print(f"     Q1 ({q1}) n'en represente que {100.0 * q1 / max(fenetre, 1):.1f}% : le reste")
            print("     parle du sujet sans employer le vocabulaire 'plateforme agreee'.")
    if topic == 0:
        print("B1 = 0 : le sujet facturation électronique FR est absent de l'index toutes dates")
        print("confondues → couverture thématique nulle, pas un problème de fenêtre temporelle.")
    elif topic is not None:
        print(f"B1 = {topic} : le sujet est présent dans l'index (toutes dates).")

    if manquantes and (zeros or errs):
        print(f"\nBaseline(s) {', '.join(manquantes)} en erreur ET {', '.join(zeros + errs)} sans")
        print("résultat exploitable : relance avant de conclure, ce zéro n'est pas interprétable.")
    elif manquantes:
        print(f"\nBaseline(s) {', '.join(manquantes)} en erreur, mais toutes les questions ont ramené")
        print("des résultats : elles n'étaient là que pour interpréter un zéro. Sans zéro, non bloquant.")

    nonzero = [name for name in QS if (n(name) or 0) > 0]
    if nonzero:
        print(f"\nCouverture confirmée : {', '.join(nonzero)} rendent des posts. Relance sans")
        print("--dry-run (1 crédit/post) pour lire le contenu.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", type=int, default=5, help="posts à collecter par requête (défaut 5)")
    ap.add_argument("--dry-run", action="store_true",
                    help="recherches seules : compte les résultats sans collecter (1 crédit/requête)")
    ap.add_argument("--since", default=SINCE, help=f"date plancher (défaut {SINCE})")
    ap.add_argument("--tri", choices=["date", "reactions"], default="date",
                    help="ordre de collecte : 'date' (plus récents) ou 'reactions' "
                         "(plus relayés — échantillon plus représentatif)")
    ap.add_argument("--all", action="store_true",
                    help="collecter TOUS les résultats de chaque question "
                         "(1 crédit/post — vérifie le total avec --dry-run avant)")
    ap.add_argument("--out", default="coresignal_posts_sample.jsonl")
    args = ap.parse_args()

    tri = "reaction_count" if args.tri == "reactions" else "date_published"
    queries = build_queries(args.since, tri)

    totals, n_written, tris_ignores, reacs = {}, 0, [], {}
    # Reprise : on repart du fichier existant plutôt que de l'écraser, et on
    # ouvre en ajout. Un run interrompu ne coûte donc jamais deux fois.
    seen = set() if args.dry_run else deja_collectes(args.out)
    if seen:
        print(f"Reprise : {len(seen)} posts déjà dans {args.out}, ils ne seront pas repayés.\n")
    out = None if args.dry_run else open(args.out, "a", encoding="utf-8")
    try:
        for name, payload in queries.items():
            path, ids, total, sans_tri = search(name, payload)
            if sans_tri:
                tris_ignores.append(name)
            totals[name] = total if total is not None else ("erreur" if path is None else len(ids))
            if args.dry_run or not ids or name.startswith("B"):
                continue  # les baselines servent à compter, pas à collecter
            lot = ids if args.all else ids[: args.collect]
            a_payer = [i for i in lot if str(i) not in seen]
            print(f"   → {len(lot)} posts visés, {len(a_payer)} à collecter "
                  f"({len(lot) - len(a_payer)} déjà en base)")
            for rang, pid in enumerate(a_payer, 1):
                seen.add(str(pid))
                p = collect(path, pid)
                if not p:
                    continue
                p["_query"] = name
                out.write(json.dumps(p, ensure_ascii=False) + "\n")
                out.flush()  # crash-safe : chaque post payé est sur le disque
                reacs.setdefault(name, []).append(p.get("reaction_count") or 0)
                n_written += 1
                print(f"   [{rang}/{len(a_payer)}]" + summarize(p)[3:])
                time.sleep(0.05)
    finally:
        if out:
            out.close()

    verdict(totals, args.since)

    if tris_ignores:
        print(f"\nATTENTION — tri non applique sur : {', '.join(tris_ignores)}.")
        print("L'API a refuse le critere demande et renvoye son ordre par defaut.")
        print("Les posts collectes ne sont donc PAS les plus pertinents selon --tri.")
    # Contrôle a posteriori : un 200 ne garantit pas que le tri a été honoré.
    if args.tri == "reactions" and reacs:
        desordre = [k for k, v in reacs.items() if v != sorted(v, reverse=True)]
        if desordre:
            print(f"\nATTENTION — ordre incoherent avec --tri reactions sur : "
                  f"{', '.join(desordre)}.")
            print("Les reactions collectees ne decroissent pas : l'API a ignore le tri")
            print("sans le signaler. Traite cet echantillon comme non representatif.")
    if args.dry_run:
        print("\nDry-run : aucun post collecté. Relance sans --dry-run pour récupérer le contenu.")
    else:
        print(f"\n{n_written} posts écrits dans {args.out}. Colle la sortie console dans la conversation.")


if __name__ == "__main__":
    main()
