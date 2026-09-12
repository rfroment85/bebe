#!/usr/bin/env python3
"""
Analyse locale du corpus collecté par coresignal_posts_test.py.

Le tri côté API étant refusé (422 sur la clause `sort`), tout le tri et le
filtrage se font ici, sur le .jsonl déjà payé. Aucun crédit consommé.

Usage :
    python analyse_posts.py                      # synthèse + top 20
    python analyse_posts.py --top 50
    python analyse_posts.py --tri commentaires   # classer par discussion plutôt que par réactions
    python analyse_posts.py --min-reactions 20   # ne garder que les posts relayés
    python analyse_posts.py --contient "rejet"   # filtrer sur le texte
    python analyse_posts.py --csv posts.csv --commentaires commentaires.csv
"""
import argparse, csv, json, os, sys
from collections import Counter


def charger(chemin):
    """Charge le .jsonl en dédoublonnant par id."""
    if not os.path.exists(chemin):
        sys.exit(f"Fichier introuvable : {chemin}")
    posts, vus = [], set()
    with open(chemin, encoding="utf-8") as f:
        for n, ligne in enumerate(f, 1):
            ligne = ligne.strip()
            if not ligne:
                continue
            try:
                p = json.loads(ligne)
            except ValueError:
                print(f"  ligne {n} illisible, ignorée")
                continue
            pid = str(p.get("id"))
            if pid in vus:
                continue
            vus.add(pid)
            posts.append(p)
    return posts


def txt(p):
    return (p.get("article_body") or "").replace("\n", " ").replace("\r", " ")


def synthese(posts):
    if not posts:
        sys.exit("Aucun post à analyser.")
    dates = sorted(d for d in (p.get("date_published") or "" for p in posts) if d)
    reac = sum(p.get("reaction_count") or 0 for p in posts)
    annonces = sum(p.get("comment_count") or 0 for p in posts)
    livres = sum(len(p.get("comments") or []) for p in posts)
    auteurs = Counter(p.get("author_name") or "?" for p in posts)

    print(f"\n{'=' * 70}")
    print(f"{len(posts)} posts uniques | {len(auteurs)} auteurs distincts")
    if dates:
        print(f"Periode : {dates[0][:10]} -> {dates[-1][:10]}")
    print(f"Reactions cumulees : {reac}")
    print(f"Commentaires annonces : {annonces} | reellement livres : {livres}", end="")
    if annonces:
        print(f" ({100.0 * livres / annonces:.0f}%)")
        print(f"  -> {annonces - livres} commentaires existent mais ne sont PAS dans les donnees.")
    else:
        print()
    print(f"{'=' * 70}")

    print("\nAuteurs les plus presents :")
    for nom, n in auteurs.most_common(10):
        print(f"  {n:3d}  {nom}")


def classement(posts, cle, top, libelle):
    print(f"\n--- Top {top} par {libelle} ---")
    for p in sorted(posts, key=lambda x: x.get(cle) or 0, reverse=True)[:top]:
        print(f"  {p.get(cle) or 0:5d} | {(p.get('date_published') or '?')[:10]} | "
              f"{(p.get('author_name') or '?')[:28]:28s} | {txt(p)[:80]}")


def export_posts(posts, chemin):
    colonnes = ["id", "date_published", "author_name", "author_headline", "reaction_count",
                "comment_count", "commentaires_livres", "url", "_query", "article_body"]
    # utf-8-sig : sans le BOM, Excel sous Windows casse les accents.
    with open(chemin, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=colonnes, extrasaction="ignore")
        w.writeheader()
        for p in sorted(posts, key=lambda x: x.get("reaction_count") or 0, reverse=True):
            ligne = {c: p.get(c) for c in colonnes}
            ligne["commentaires_livres"] = len(p.get("comments") or [])
            ligne["article_body"] = txt(p)
            w.writerow(ligne)
    print(f"\n{len(posts)} posts -> {chemin}")


def export_commentaires(posts, chemin):
    """Aplatit les commentaires. Les sous-champs varient : on prend leur union."""
    lignes, cles = [], set()
    for p in posts:
        for c in (p.get("comments") or []):
            if not isinstance(c, dict):
                c = {"valeur": c}
            cles.update(c.keys())
            lignes.append((p, c))
    if not lignes:
        print("\nAucun commentaire dans le corpus.")
        return
    colonnes = ["post_id", "post_auteur", "post_date"] + sorted(cles)
    with open(chemin, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=colonnes, extrasaction="ignore")
        w.writeheader()
        for p, c in lignes:
            ligne = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                     for k, v in c.items()}
            ligne.update({"post_id": p.get("id"), "post_auteur": p.get("author_name"),
                          "post_date": p.get("date_published")})
            w.writerow(ligne)
    print(f"{len(lignes)} commentaires -> {chemin}")
    print(f"  champs disponibles : {', '.join(sorted(cles))}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="entree", default="coresignal_posts_sample.jsonl")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--tri", choices=["reactions", "commentaires"], default="reactions")
    ap.add_argument("--min-reactions", type=int, default=0)
    ap.add_argument("--contient", help="ne garder que les posts contenant ce texte (insensible a la casse)")
    ap.add_argument("--csv", help="exporter les posts en CSV (ouvrable dans Excel)")
    ap.add_argument("--commentaires", help="exporter les commentaires en CSV")
    args = ap.parse_args()

    posts = charger(args.entree)
    total = len(posts)
    if args.min_reactions:
        posts = [p for p in posts if (p.get("reaction_count") or 0) >= args.min_reactions]
    if args.contient:
        motif = args.contient.lower()
        posts = [p for p in posts if motif in txt(p).lower()]
    if len(posts) != total:
        print(f"Filtre : {len(posts)} posts retenus sur {total}.")

    synthese(posts)
    cle, libelle = (("comment_count", "commentaires") if args.tri == "commentaires"
                    else ("reaction_count", "reactions"))
    classement(posts, cle, args.top, libelle)
    if args.csv:
        export_posts(posts, args.csv)
    if args.commentaires:
        export_commentaires(posts, args.commentaires)


if __name__ == "__main__":
    main()
