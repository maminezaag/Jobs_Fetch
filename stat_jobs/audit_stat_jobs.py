#!/usr/bin/env python3
# coding: utf-8
"""Audit NON DESTRUCTIF de stat_jobs : recherche API, archivage et limites de pagination.

Copier ce fichier à côté de stat_jobs.py / config.json dans stat_jobs/.
Installer les dépendances avec `pip install requests`.

Exécution rapide : python audit_stat_jobs.py
Exécution exhaustive pour reproduire la recherche actuelle : python audit_stat_jobs.py --complet
Le rapport JSON est écrit dans audit_stat_jobs_resultat.json, sans toucher au Sheet ni à l'archive.
"""
import argparse
import base64
import glob
import json
import os
import time
import urllib.parse
from datetime import date, timedelta

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
API_SEARCH = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
API_DETAIL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{}"
HEADERS = {"X-API-Key": "jobboerse-jobsuche", "User-Agent": "stat_jobs-audit/1.0"}
SIZE = 100
TARGETS = {
    "event it AG / Linux-Systemadministrator": ["10001-1003579803-S"],
    "ANG GmbH / Administrator Linux (exemples connus)": [
        "10001-1003810414-S", "10001-1003810365-S", "10001-1003859776-S",
    ],
}


def request_json(session, url, **kwargs):
    last = None
    for n in range(3):
        try:
            response = session.get(url, headers=HEADERS, timeout=40, **kwargs)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            if n < 2:
                time.sleep((2, 6)[n])
    raise RuntimeError(f"API inaccessible : {last}")


def archive_stats(config):
    dossier = os.path.join(BASE, config.get("archive_dossier", "archive"))
    sources = {}
    statuts = {}
    for file in sorted(glob.glob(os.path.join(dossier, "*.jsonl"))):
        with open(file, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                o = json.loads(line)
                ref = o.get("refnr")
                if not ref:
                    continue
                sources[ref] = {"titre": o.get("titel"), "entreprise": o.get("firma"),
                                "datum": o.get("datum"),
                                "statut": o.get("statut"), "fichier": os.path.basename(file),
                                "sources": o.get("recherches", [])}
                statuts[o.get("statut", "(absent)")] = statuts.get(o.get("statut", "(absent)"), 0) + 1
    return sources, statuts


def rechercher(session, cfg, terme, champ, max_pages, etape, jours=None):
    """Reproduit exactement les paramètres de lancer_recherche() de stat_jobs.py."""
    page = 1
    rows = []
    total = None
    exhausted = False
    last_count = 0
    while page <= max_pages:
        params = {
            "was": terme, "size": SIZE, "page": page,
            "veroeffentlichtseit": min(int(jours if jours is not None else cfg.get("recherche_jours", 100)), 100),
            "angebotsart": cfg.get("angebotsart", 1),
            "zeitarbeit": str(bool(cfg.get("zeitarbeit", True))).lower(),
        }
        if champ:
            params["berufsfeld"] = champ
        data = request_json(session, API_SEARCH, params=params)
        liste = data.get("ergebnisliste") or data.get("stellenangebote") or []
        total = data.get("maxErgebnisse")
        last_count = len(liste)
        rows.extend(liste)
        if len(liste) < SIZE:
            exhausted = True
            break
        try:
            if total is not None and len(rows) >= int(total):
                exhausted = True
                break
        except (ValueError, TypeError):
            pass
        page += 1
        time.sleep(cfg.get("pause_s", 0.15))
    refs = {}
    entreprises = []
    for o in rows:
        ref = o.get("referenznummer") or o.get("refnr") or ""
        titre = o.get("stellenangebotsTitel") or o.get("hauptberuf") or ""
        firme = o.get("firma") or ""
        if ref:
            refs[ref] = {"titre": titre, "entreprise": firme}
        if "ang gmbh" in firme.lower() or "event it" in firme.lower():
            entreprises.append({"refnr": ref, "titre": titre, "entreprise": firme})
    attendus = {r for arr in TARGETS.values() for r in arr}
    trouves = {k: refs[k] for k in attendus if k in refs}
    info = {
        "etape": etape, "mot_cle": terme, "berufsfeld": champ,
        "veroeffentlichtseit": int(jours if jours is not None else cfg.get("recherche_jours", 100)),
        "pages_consultees": min(page, max_pages),
        "total_annonce_api": total, "offres_retournees": len(rows), "offres_uniques": len(refs),
        "pagination_complete": exhausted, "limite_atteinte": not exhausted and last_count == SIZE,
        "references_cibles_trouvees": trouves, "annonces_entreprises_cibles": entreprises[:30],
        "nb_annonces_entreprises_cibles": len(entreprises),
    }
    print(f"[{etape:10s}] {terme:29s} | champ={str(champ):42.42s} | "
          f"{len(rows):5d} annonces, {min(page,max_pages):2d} page(s), "
          f"cibles={len(trouves)} refs / {len(entreprises)} entreprises, "
          f"{'COMPLET' if exhausted else 'PARTIEL - LIMITE'}")
    return info


def obtenir_detail(session, refnr):
    url = API_DETAIL.format(urllib.parse.quote(base64.b64encode(refnr.encode()).decode()))
    try:
        data = request_json(session, url)
    except Exception as e:
        return {"reference": refnr, "detail_accessible": False, "erreur": str(e)}
    texte = data.get("stellenangebotsBeschreibung") or data.get("stellenbeschreibung") or ""
    return {"reference": refnr, "detail_accessible": True,
            "longueur_description": len(texte), "texte_c1_present": "C1" in texte,
            "titre": data.get("titel") or data.get("stellenangebotsTitel")}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--complet", action="store_true", help="rejoue aussi TOUS les mots-clés actuels sur 60 pages")
    p.add_argument("--pages", type=int, default=5, help="pages par recherche ciblée (défaut 5, maximum 60)")
    p.add_argument("--sans-detail", action="store_true", help="ne teste pas l'API détail")
    args = p.parse_args(argv)
    if args.pages < 1 or args.pages > 60:
        p.error("--pages doit être compris entre 1 et 60")
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    archive, statuts = archive_stats(cfg)
    cutoff = (date.today() - timedelta(days=100)).isoformat()
    anciennes = sum(1 for o in archive.values() if o.get("datum", o.get("publication", "")) and
                    o.get("datum", o.get("publication", "")) < cutoff)
    print("\nARCHIVE :", len(archive), "offres", statuts)
    print(f"ARCHIVE AVEC DATE DE PUBLICATION AVANT {cutoff} (>100j) :", anciennes)
    for libelle, refs in TARGETS.items():
        print("  -", libelle, ":", {r: archive.get(r, "ABSENTE") for r in refs})

    # Isoler l'effet du MOT-CLÉ, puis l'effet du filtre BERUFSFELD (mêmes paramètres API).
    variations = [
        ("Administrator Linux", cfg.get("berufsfeld"), "mot+champ"),
        ("Administrator Linux", None, "mot sans champ"),
        ("Administrator Linux", "Informatik", "autre champ"),
        ("Linux-Systemadministrator", cfg.get("berufsfeld"), "mot+champ"),
        ("Linux-Systemadministrator", None, "mot sans champ"),
        ("Systemadministrator", cfg.get("berufsfeld"), "mot existant"),
    ]
    if args.complet:
        variations += [(t, cfg.get("berufsfeld"), "domaine prod") for t in cfg.get("mots_cles", [])]
        variations += [(t, cfg.get("berufsfeld_marche"), "marche prod") for t in cfg.get("mots_cles_marche", [])]
    rapport = {"audit_date": date.today().isoformat(), "recherche_jours": cfg.get("recherche_jours"),
               "archive_total": len(archive), "archive_plus_de_100_jours": anciennes, "statuts_archive": statuts,
               "cibles_archivees": {r: r in archive for refs in TARGETS.values() for r in refs},
               "recherches": [], "details": []}
    with requests.Session() as session:
        for mot, champ, etape in dict.fromkeys(variations):
            try:
                info = rechercher(session, cfg, mot, champ, 60 if args.complet else args.pages, etape)
                rapport["recherches"].append(info)
            except Exception as e:
                print("ERREUR RECHERCHE", mot, champ, e)
                rapport["recherches"].append({"mot_cle": mot, "berufsfeld": champ, "erreur": str(e)})
        # Test A/B du nombre de jours : comparer 28 (valeur fréquente côté BA) et 100 (configuration).
        for jours in (28, 100):
            try:
                info = rechercher(session, cfg, "Systemadministrator", cfg.get("berufsfeld"), 1,
                                  f"test dates {jours}j", jours=jours)
                rapport["recherches"].append(info)
            except Exception as e:
                print("ERREUR TEST DATES", jours, e)
        if not args.sans_detail:
            for r in dict.fromkeys(r for refs in TARGETS.values() for r in refs):
                d = obtenir_detail(session, r)
                rapport["details"].append(d)
                print("DÉTAIL", r, d)
    chemin = os.path.join(BASE, "audit_stat_jobs_resultat.json")
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(rapport, f, ensure_ascii=False, indent=2)
    print("\nRapport enregistré :", chemin)
    print("Note : une recherche PARTIELLE ne prouve jamais l'absence d'une annonce de l'API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
