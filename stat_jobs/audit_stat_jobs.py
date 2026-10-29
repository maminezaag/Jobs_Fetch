#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit comparatif, en lecture seule, de 4 annonces Arbeitsagentur.

Remplacer stat_jobs/audit_stat_jobs.py par ce fichier dans le dépôt.
Conserver le workflow GitHub Actions déjà installé : options --pages / --complet.

N'écrit PAS dans Google Sheets et ne modifie NI stat_jobs.py NI config.json
NI l'archive. Écrit uniquement stat_jobs/audit_stat_jobs_resultat.json.
"""
import argparse
import base64
import glob
import json
import os
import time
from datetime import date

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
URL = 'https://rest.arbeitsagentur.de/jobboerse/jobsuche-service'
ENTETES = {'X-API-Key': 'jobboerse-jobsuche', 'User-Agent': 'stat_jobs-audit-comparaison/3.0'}
TAILLE_PAGE = 100

ANNONCES = {
    'event_absente': {
        'ref': '10001-1003579803-S',
        'entreprise': 'event it AG',
        'titre': 'Linux-Systemadministrator (m/w/d)',
        'publication': '2026-08-21',
    },
    'ferchau_pdf_c1': {
        'ref': '12265-469948_JB5262478-S',
        'entreprise': 'FERCHAU GmbH Niederlassung Köln',
        'titre': 'Linux-Systemadministrator (m/w/d)',
        'publication': '2026-10-02',
    },
    'ferchau_recente': {
        'ref': '12265-469948_JB5283281-S',
        'entreprise': 'FERCHAU GmbH Niederlassung Köln',
        'titre': 'Linux-Systemadministrator (m/w/d)',
        'publication': '2026-10-27',
    },
    'hays_pdf_b2': {
        'ref': '13319-899515/1_5536CS-S',
        'entreprise': 'Hays Professional Solutions GmbH Standort Frankfurt',
        'titre': 'Linux Admin (m/w/d)',
        'publication': '2026-10-26',
    },
}


def lire_json(fichier):
    with open(fichier, encoding='utf-8') as f:
        return json.load(f)


def etat_archive():
    """Recherche aussi dans les archives des autres mois, si elles existent."""
    par_ref = {}
    for fichier in sorted(glob.glob(os.path.join(BASE, 'archive', '*.jsonl'))):
        with open(fichier, encoding='utf-8') as f:
            for n, ligne in enumerate(f, 1):
                if not ligne.strip():
                    continue
                try:
                    o = json.loads(ligne)
                except ValueError:
                    print(f'Archive corrompue {fichier}:{n} (ligne ignorée)')
                    continue
                ref = o.get('refnr')
                if ref:
                    par_ref[ref] = {
                        'fichier': os.path.basename(fichier),
                        'statut': o.get('statut'),
                        'titre': o.get('titel'),
                        'publication': o.get('datum'),
                        'premiere_vue': o.get('premiere_vue'),
                        'recherches': o.get('recherches'),
                    }
    return {
        nom: {'presente': meta['ref'] in par_ref, 'fiche': par_ref.get(meta['ref'])}
        for nom, meta in ANNONCES.items()
    }


def requete(session, chemin, params=None):
    erreur = None
    for tentative in range(3):
        try:
            reponse = session.get(URL + chemin, params=params, headers=ENTETES, timeout=40)
            if reponse.status_code in (404, 410):
                return None, reponse.status_code, None
            reponse.raise_for_status()
            return reponse.json(), reponse.status_code, None
        except (requests.RequestException, ValueError) as e:
            erreur = str(e)
            if tentative < 2:
                time.sleep((2, 6)[tentative])
    return None, None, erreur


def chercher(session, nom, params, pages_max):
    fiches = {}
    total = None
    erreur = None
    nb_pages = 0
    complet = False
    for page in range(1, pages_max + 1):
        p = dict(params, page=page, size=TAILLE_PAGE)
        data, statut, err = requete(session, '/pc/v6/jobs', p)
        if data is None:
            erreur = err or ('HTTP ' + str(statut))
            break
        liste = data.get('ergebnisliste') or data.get('stellenangebote') or []
        total = data.get('maxErgebnisse', total)
        nb_pages += 1
        for offre in liste:
            ref = offre.get('referenznummer') or offre.get('refnr')
            if ref:
                fiches[ref] = {
                    'titre': offre.get('stellenangebotsTitel') or offre.get('hauptberuf') or '',
                    'entreprise': offre.get('firma', ''),
                    'publication': offre.get('datumErsteVeroeffentlichung', ''),
                }
        if len(liste) < TAILLE_PAGE or (total is not None and page * TAILLE_PAGE >= int(total)):
            complet = True
            break
        time.sleep(0.15)

    recensement = {nom: {
        'trouvee': meta['ref'] in fiches,
        'details_liste': fiches.get(meta['ref'])
    } for nom, meta in ANNONCES.items()}
    resume = {nom: ('OUI' if v['trouvee'] else ('NON' if complet else 'INCONNU (pagination partielle)'))
              for nom, v in recensement.items()}
    print(f"\n{nom}: {len(fiches)} résultats, total API {total}, {nb_pages} page(s), complet={complet}")
    print('  ' + '; '.join(f'{k}={v}' for k, v in resume.items()))
    if erreur:
        print('  Erreur:', erreur)
    return {
        'test': nom, 'params': params, 'total_api': total,
        'total_uniques_lus': len(fiches), 'pages_lues': nb_pages,
        'complet': complet, 'erreur': erreur, 'annonces': recensement,
    }


def obtenir_detail(session, nom, meta):
    ref = meta['ref']
    b64 = base64.b64encode(ref.encode('utf-8')).decode('ascii')
    data, statut, erreur = requete(session, '/pc/v4/jobdetails/' + b64)
    if data is None:
        print(f'DETAIL {nom}: HTTP {statut}, erreur={erreur}')
        return {'http': statut, 'accessible': False, 'erreur': erreur}
    titre = data.get('stellenangebotsTitel') or data.get('titel')
    texte = data.get('stellenangebotsBeschreibung') or data.get('stellenbeschreibung') or ''
    resultat = {
        'http': statut, 'accessible': True,
        'titre_api': titre, 'publication_api': data.get('datumErsteVeroeffentlichung'),
        'derniere_modification': data.get('datumLetzteAenderung'),
        'longueur_description': len(texte),
        'champs_disponibles': sorted(data.keys()),
    }
    print(f'DETAIL {nom}: HTTP {statut}, titre={titre!r}, publication={resultat["publication_api"]}')
    return resultat


def main():
    ap = argparse.ArgumentParser(description='Comparer une offre absente à trois offres archivées')
    ap.add_argument('--pages', type=int, default=5)
    ap.add_argument('--complet', action='store_true')
    ap.add_argument('--sans-detail', action='store_true')
    args = ap.parse_args()
    if not 1 <= args.pages <= 60:
        ap.error('--pages doit être compris entre 1 et 60')
    limite = 60 if args.complet else args.pages
    cfg = lire_json(os.path.join(BASE, 'config.json'))
    bf = cfg.get('berufsfeld')
    jours = cfg.get('recherche_jours', 100)
    base = {'angebotsart': cfg.get('angebotsart', 1),
            'zeitarbeit': 'true' if cfg.get('zeitarbeit', True) else 'false'}
    titres = [
        ('01 - même titre, paramètres du script', {'was': 'Linux-Systemadministrator',
                                                   'berufsfeld': bf, 'veroeffentlichtseit': jours}),
        ('02 - même titre, sans champ', {'was': 'Linux-Systemadministrator',
                                        'veroeffentlichtseit': jours}),
        ('03 - même titre, sans limite date', {'was': 'Linux-Systemadministrator'}),
        ('04 - même titre, sans champ, zeitarbeit=false', {'was': 'Linux-Systemadministrator',
                                                          'veroeffentlichtseit': jours, 'zeitarbeit': 'false'}),
        ('05 - même titre, sans champ, zeitarbeit omis', {'was': 'Linux-Systemadministrator',
                                                        'veroeffentlichtseit': jours, 'zeitarbeit': None}),
        ('06 - mot large, paramètres script', {'was': 'Systemadministrator', 'berufsfeld': bf,
                                              'veroeffentlichtseit': jours}),
        ('07 - contrôle B2 Hays, mot large', {'was': 'Linux Admin',
                                            'veroeffentlichtseit': jours}),
    ]
    resultats = {
        'version': 3, 'date_audit': date.today().isoformat(),
        'objectif': 'Comparer une annonce absente et trois offres archivées, y compris deux titres identiques',
        'annonces_testees': ANNONCES,
        'archive': etat_archive(), 'recherches': [], 'details': {},
    }
    print('=== CONTRÔLE ARCHIVE ===')
    for nom, info in resultats['archive'].items():
        print(nom, ':', 'PRESENTE' if info['presente'] else 'ABSENTE')
    with requests.Session() as s:
        for nom, variante in titres:
            p = dict(base)
            p.update(variante)
            p = {k: v for k, v in p.items() if v is not None}
            resultats['recherches'].append(chercher(s, nom, p, limite))
        if not args.sans_detail:
            for nom, meta in ANNONCES.items():
                resultats['details'][nom] = obtenir_detail(s, nom, meta)
    sortie = os.path.join(BASE, 'audit_stat_jobs_resultat.json')
    with open(sortie, 'w', encoding='utf-8') as f:
        json.dump(resultats, f, indent=2, ensure_ascii=False)
    print('\nRapport écrit:', sortie)
    print('Aucun fichier historique, fichier de configuration ou Sheet n’a été modifié.')


if __name__ == '__main__':
    main()
