#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostic additionnel en lecture seule pour les offres absentes de stat_jobs.

Remplace l'ancien audit_stat_jobs.py; utilise le MEME workflow GitHub Actions.
Ne change ni l'archive, ni la configuration, ni Google Sheets.

Tests : recherche v6 / v4-app, variantes de paramètres, références ciblées,
        détails v4 / v3. Rapport : audit_stat_jobs_resultat.json.
"""
import argparse
import base64
import json
import os
import time
from datetime import date
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = 'https://rest.arbeitsagentur.de/jobboerse/jobsuche-service'
HEADERS = {'X-API-Key': 'jobboerse-jobsuche', 'User-Agent': 'stat_jobs-audit/2.0'}
TARGETS = {
    'event it AG': ['10001-1003579803-S'],
    'ANG GmbH': ['10001-1003810414-S', '10001-1003810365-S',
                 '10001-1003859776-S', '10001-1003859757-S'],
}
TARGET_REFS = set(sum(TARGETS.values(), []))
SIZE = 100


def get_json(session, path, params=None):
    err = None
    for attempt in range(3):
        try:
            response = session.get(ROOT + path, params=params, headers=HEADERS, timeout=40)
            if response.status_code in (404, 410):
                return None, response.status_code
            response.raise_for_status()
            return response.json(), response.status_code
        except (requests.RequestException, ValueError) as e:
            err = e
            if attempt < 2:
                time.sleep((2, 6)[attempt])
    return {'erreur': str(err)}, None


def list_offres(session, name, path, base_params, max_pages):
    records, api_total, err = {}, None, None
    pages, complete = 0, False
    keys = []
    for page in range(1, max_pages + 1):
        params = dict(base_params, page=page, size=SIZE)
        data, code = get_json(session, path, params)
        if data is None:
            err = f'HTTP {code}'
            break
        if data.get('erreur'):
            err = data['erreur']
            break
        if page == 1:
            keys = list(data.keys())
        liste = data.get('ergebnisliste') or data.get('stellenangebote') or []
        api_total = data.get('maxErgebnisse', api_total)
        pages += 1
        for offer in liste:
            ref = offer.get('referenznummer') or offer.get('refnr') or ''
            if ref:
                records[ref] = {
                    'titre': offer.get('stellenangebotsTitel') or offer.get('hauptberuf') or '',
                    'entreprise': offer.get('firma') or '',
                    'publication': offer.get('datumErsteVeroeffentlichung') or '',
                }
        if len(liste) < SIZE or (api_total is not None and len(records) >= int(api_total)):
            complete = True
            break
        time.sleep(.12)
    refs = {ref: info for ref, info in records.items() if ref in TARGET_REFS}
    companies = {ref: info for ref, info in records.items()
                 if 'ang gmbh' in info['entreprise'].lower() or 'event it' in info['entreprise'].lower()}
    result = dict(test=name, endpoint=path, params=base_params, pages=pages,
                  api_total=api_total, unique_results=len(records), complete=complete,
                  found_targets=refs, found_companies=companies, error=err,
                  response_keys=keys)
    print(f'{name:29} | {len(records):5} offre(s) | {len(refs)} refs | {len(companies)} entreprises | '
          f'{"complet" if complete else "partiel/erreur"}')
    return result


def detail(session, ref):
    encoded = base64.b64encode(ref.encode()).decode()
    output = {'reference': ref}
    for version in ('v4', 'v3'):
        data, status = get_json(session, '/pc/' + version + '/jobdetails/' + encoded)
        if data is None:
            output[version] = {'status': status, 'accessible': False}
        elif data.get('erreur'):
            output[version] = {'status': status, 'accessible': False, 'erreur': data['erreur']}
        else:
            texte = data.get('stellenangebotsBeschreibung') or data.get('stellenbeschreibung') or ''
            output[version] = {
                'status': status, 'accessible': True,
                'titre': data.get('stellenangebotsTitel') or data.get('titel'),
                'publication': data.get('datumErsteVeroeffentlichung'),
                'modification': data.get('datumLetzteAenderung'),
                'longueur_description': len(texte),
                'mention_c1': 'c1' in texte.lower(),
            }
    print('DETAIL', ref, 'v4=', output['v4']['status'], 'v3=', output['v3']['status'])
    return output


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pages', type=int, default=3)
    ap.add_argument('--complet', action='store_true', help='Utilise jusqu\'à 60 pages par test')
    ap.add_argument('--sans-detail', action='store_true')
    args = ap.parse_args()
    if not 1 <= args.pages <= 60:
        ap.error('--pages doit être compris entre 1 et 60')
    cfg = json.load(open(os.path.join(BASE, 'config.json'), encoding='utf8'))
    jours = cfg.get('recherche_jours', 100)
    common = {'angebotsart': cfg.get('angebotsart', 1),
              'zeitarbeit': str(bool(cfg.get('zeitarbeit', True))).lower()}
    root_tests = [
        ('ANG, titre, 100j', 'v6', {'was': 'Administrator Linux', 'veroeffentlichtseit': jours}),
        ('ANG, titre, sans date', 'v6', {'was': 'Administrator Linux'}),
        ('ANG, employeur, 100j', 'v6', {'arbeitgeber': 'ANG GmbH', 'veroeffentlichtseit': jours}),
        ('ANG, employeur, sans date', 'v6', {'arbeitgeber': 'ANG GmbH'}),
        ('ANG, Bochum', 'v6', {'was': 'Administrator Linux', 'wo': 'Bochum', 'veroeffentlichtseit': jours}),
        ('ANG, titre, pav=true', 'v6', {'was': 'Administrator Linux', 'pav': 'true', 'veroeffentlichtseit': jours}),
        ('event, titre, 100j', 'v6', {'was': 'Linux-Systemadministrator', 'veroeffentlichtseit': jours}),
        ('event, titre, sans date', 'v6', {'was': 'Linux-Systemadministrator'}),
        ('event, employeur, 100j', 'v6', {'arbeitgeber': 'event it AG', 'veroeffentlichtseit': jours}),
        ('event, employeur, sans date', 'v6', {'arbeitgeber': 'event it AG'}),
        ('event, Hannover', 'v6', {'was': 'Linux-Systemadministrator', 'wo': 'Hannover', 'veroeffentlichtseit': jours}),
        ('event, titre, pav=true', 'v6', {'was': 'Linux-Systemadministrator', 'pav': 'true', 'veroeffentlichtseit': jours}),
        ('ANG, app v4', 'v4app', {'was': 'Administrator Linux', 'veroeffentlichtseit': jours}),
        ('event, app v4', 'v4app', {'was': 'Linux-Systemadministrator', 'veroeffentlichtseit': jours}),
    ]
    endpoint = {'v6': '/pc/v6/jobs', 'v4app': '/pc/v4/app/jobs'}
    results = {'audit_date': date.today().isoformat(), 'version': 2,
               'note': 'Recherches avec pagination limitée : complete=false signifie résultat non exhaustif.',
               'targets': TARGETS, 'searches': [], 'details': []}
    with requests.Session() as s:
        for name, api, extras in root_tests:
            p = dict(common, **extras)
            results['searches'].append(list_offres(s, name, endpoint[api], p,
                                                   60 if args.complet else args.pages))
        if not args.sans_detail:
            for refs in TARGETS.values():
                for ref in refs:
                    results['details'].append(detail(s, ref))
    output = os.path.join(BASE, 'audit_stat_jobs_resultat.json')
    with open(output, 'w', encoding='utf8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print('Rapport enregistré :', output)


if __name__ == '__main__':
    main()
