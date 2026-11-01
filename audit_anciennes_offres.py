#!/usr/bin/env python3
"""Audit EN LECTURE SEULE des annonces de seen_jobs.json encore visibles dans l'API.

N'envoie jamais d'email, ne modifie jamais seen_jobs.json, ne pousse rien sur GitHub.
Le statut historique 'déjà notifiée' n'est fiable que pour le registre notifications,
introduit récemment dans le collecteur principal.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZipFile, ZIP_DEFLATED

import requests

import job_watcher as jw

LOG = logging.getLogger("audit_anciennes_offres")
HEADERS = [
    "Titre", "Entreprise", "Ville", "Code postal", "Région", "Publication",
    "Score", "Priorité métier", "Source", "Télétravail", "Français",
    "Compétences détectées", "Notification antérieure", "Décision",
    "Motifs d'exclusion", "Lien Arbeitsagentur", "Référence BA",
]
COL_WIDTHS = [52, 35, 23, 15, 25, 18, 10, 19, 13, 14, 13, 52, 30, 22, 54, 67, 33]


def _clean_xml(value: object) -> str:
    s = str(value) if value is not None else ""
    # XML 1.0 n'accepte pas certains caractères de contrôle.
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return escape(s, {'"': '&quot;'})


def _column_letter(index: int) -> str:
    result = ""
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


def _sheet_xml(headers: list[str], rows: list[list[object]], widths: list[int]) -> bytes:
    n = len(headers)
    last = f"{_column_letter(n)}{len(rows) + 1}"
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
             '<sheetFormatPr defaultRowHeight="16"/>', '<cols>']
    for i, width in enumerate(widths, 1):
        parts.append(f'<col min="{i}" max="{i}" width="{max(9, min(75, width))}" customWidth="1"/>')
    parts += ['</cols>', '<sheetData>']
    for ri, row in enumerate([headers, *rows], 1):
        parts.append(f'<row r="{ri}"' + (' ht="28" customHeight="1"' if ri == 1 else '') + '>')
        for ci, val in enumerate(row, 1):
            address = f'{_column_letter(ci)}{ri}'
            style = ' s="1"' if ri == 1 else ''
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                parts.append(f'<c r="{address}"{style}><v>{val}</v></c>')
            else:
                parts.append(f'<c r="{address}"{style} t="inlineStr"><is><t xml:space="preserve">{_clean_xml(val)}</t></is></c>')
        parts.append('</row>')
    parts.append('</sheetData>')
    parts.append(f'<autoFilter ref="A1:{last}"/>')
    parts.append('</worksheet>')
    return ''.join(parts).encode('utf-8')


def write_xlsx(path: Path, sheets: list[tuple[str, list[str], list[list[object]], list[int]]]) -> None:
    """Génère un XLSX basique avec la bibliothèque standard Python uniquement."""
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    relns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    workbook = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<workbook xmlns="{ns}" xmlns:r="{relns}"><sheets>'
                + ''.join(f'<sheet name="{_clean_xml(name)}" sheetId="{i}" r:id="rId{i}"/>'
                          for i, (name, _, _, _) in enumerate(sheets, 1))
                + '</sheets></workbook>')
    relations = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 + ''.join(f'<Relationship Id="rId{i}" Type="{relns}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                           for i in range(1, len(sheets) + 1))
                 + f'<Relationship Id="rId{len(sheets) + 1}" Type="{relns}/styles" Target="styles.xml"/>'
                 + '</Relationships>')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                     '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                     + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                               for i in range(1, len(sheets) + 1)) + '</Types>')
    root_rel = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rId1" Type="{relns}/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>')
    # Titre sur fond bleu-vert, table claire et filtrable.
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              f'<styleSheet xmlns="{ns}"><fonts count="2">'
              '<font><sz val="10"/><name val="Calibri"/></font>'
              '<font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
              '</fonts><fills count="3"><fill><patternFill patternType="none"/></fill>'
              '<fill><patternFill patternType="gray125"/></fill>'
              '<fill><patternFill patternType="solid"><fgColor rgb="FF2C6E91"/><bgColor indexed="64"/></patternFill></fill>'
              '</fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
              '</styleSheet>')
    with ZipFile(path, 'w', ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', content_types)
        z.writestr('_rels/.rels', root_rel)
        z.writestr('xl/workbook.xml', workbook)
        z.writestr('xl/_rels/workbook.xml.rels', relations)
        z.writestr('xl/styles.xml', styles)
        for i, (_, headers, rows, widths) in enumerate(sheets, 1):
            z.writestr(f'xl/worksheets/sheet{i}.xml', _sheet_xml(headers, rows, widths))


def offer_row(job: jw.JobResult, notification: str, decision: str, motifs: str = '') -> list[object]:
    return [
        job.title, job.employer, job.ort, job.plz, job.region, job.published[:10],
        job.score, job.priorite_metier, job.source, job.statut_teletravail,
        'Oui' if job.francais else 'Non', ' ; '.join(job.matched_positive),
        notification, decision, motifs, job.url, job.refnr,
    ]


def audit(config: Path, limit: int, output: Path, delay: float = .4) -> dict[str, int]:
    cfg = jw.load_config(config)
    state_path = jw.resolve_path(config, cfg['etat'].get('fichier', 'seen_jobs.json'))
    if not state_path.is_file():
        raise FileNotFoundError(f'Historique introuvable : {state_path}; audit annulé, rien à réinitialiser.')
    state = json.loads(state_path.read_text(encoding='utf-8'))
    seen = state.get('vus', {})
    if not isinstance(seen, dict) or not seen:
        raise ValueError('Historique vide ou invalide : audit annulé sans aucune écriture.')
    notifications = state.get('notifications', [])
    if not isinstance(notifications, list):
        notifications = []
    notifications = [n for n in notifications if isinstance(n, dict)]
    output.mkdir(parents=True, exist_ok=True)

    with requests.Session() as session:
        all_offers = jw.search_jobs(cfg, session)
        candidates = [o for o in all_offers if o.get('referenznummer') in seen]
        # Priorité aux offres les plus récemment observées ; les autres restent dans l'archive.
        candidates.sort(key=lambda o: str(seen.get(o.get('referenznummer'), '')), reverse=True)
        selected = candidates[:limit]
        LOG.info('Historique : %s | encore retrouvées : %s | sélectionnées : %s',
                 len(seen), len(candidates), len(selected))
        results = []
        missing = []
        for index, o in enumerate(selected, 1):
            loc = o.get('stellenlokationen') or [{}]
            first_loc = loc[0] or {}
            a = first_loc.get('adresse') or {}
            job = jw.JobResult(
                refnr=o.get('referenznummer', ''),
                title=o.get('stellenangebotsTitel') or o.get('hauptberuf', ''),
                employer=o.get('firma', ''), ort=a.get('ort', ''), plz=str(a.get('plz', '')),
                region=a.get('region', ''),
                published=o.get('datumErsteVeroeffentlichung') or
                          (o.get('veroeffentlichungszeitraum') or {}).get('von', ''),
                latitude=first_loc.get('breite'), longitude=first_loc.get('laenge'),
                via_francais_seul=bool(o.get('_via_francais_seul')),
                localisations=loc,
            )
            job.description = jw.fetch_description(session, job.refnr)
            if job.description.strip():
                jw.evaluate(job, cfg)
                jw.classify_source(job, cfg)
                results.append(job)
            else:
                missing.append(job)
            if index % 100 == 0:
                LOG.info('Audit : %s/%s offres consultées', index, len(selected))
            time.sleep(delay)

    normal = int(cfg['criteres'].get('seuil_points', 12))
    target = int(cfg['criteres'].get('seuil_points_cibles', 5))
    potential = [j for j in results if not j.eliminated and (
        j.score >= normal or j.francais or (j.metier_cible and not j.penalite_metier and j.score >= target)
    )]
    unique, duplicates = jw._dedupliquer_qualifies(potential)
    seen_notifications = {n.get('reference') for n in notifications if isinstance(n, dict)}
    def status(j: jw.JobResult) -> str:
        if j.refnr in seen_notifications or jw._deja_notifie_autre_reference(j, notifications):
            return 'Déjà notifiée (registre récent)'
        return 'Inconnu : ancien historique'

    not_sent = sorted((j for j in unique if not status(j).startswith('Déjà')),
                      key=lambda j: (['Très adapté', 'Adapté', 'À examiner'].index(j.priorite_metier), -j.score))
    sent = [j for j in unique if status(j).startswith('Déjà')]
    filtered = [j for j in results if j not in potential]
    already = [offer_row(j, status(j), 'À vérifier') for j in not_sent]
    notified = [offer_row(j, status(j), 'Notifiée') for j in sent]
    rejected = [offer_row(j, status(j), 'Éliminée' if j.eliminated else 'Sous le seuil',
                          ', '.join(j.matched_elimination)) for j in filtered]
    rejected += [offer_row(j, status(j), 'Doublon autre référence') for j in duplicates]
    rejected += [offer_row(j, 'Inconnu', 'Description indisponible') for j in missing]
    summary = [
        ['Historique total (identifiants)', len(seen)],
        ['Offres encore visibles dans les recherches actuelles', len(candidates)],
        ['Offres sélectionnées pour audit', len(selected)],
        ['Descriptions disponibles et réévaluées', len(results)],
        ['Qualifiées à vérifier (notification ancienne inconnue)', len(not_sent)],
        ['Déjà notifiées selon registre récent', len(sent)],
        ['Écartées / sous le seuil / doublons / détails manquants', len(rejected)],
        ['Seuil normal (points)', normal],
        ['Seuil métiers ciblés (points)', target],
        ['Date UTC du rapport', datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')],
        ['AVERTISSEMENT', 'Une offre dans « À vérifier » peut avoir déjà été envoyée avant la création du registre notifications.'],
        ['LIMITATION', 'L’API ne fournit ici que les annonces encore accessibles dans la fenêtre actuelle de recherche.'],
    ]
    sheets = [
        ('Synthese', ['Indicateur', 'Valeur'], summary, [65, 95]),
        ('A verifier', HEADERS, already, COL_WIDTHS),
        ('Deja notifiees', HEADERS, notified, COL_WIDTHS),
        ('Autres resultats', HEADERS, rejected, COL_WIDTHS),
    ]
    write_xlsx(output / 'audit_anciennes_offres.xlsx', sheets)
    for name, rows in [('a_verifier.csv', already), ('deja_notifiees.csv', notified), ('autres_resultats.csv', rejected)]:
        with (output / name).open('w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(HEADERS)
            writer.writerows(rows)
    (output / 'resume.json').write_text(json.dumps(dict(summary[:9]), ensure_ascii=False, indent=2), encoding='utf-8')
    LOG.info('Rapport créé : %s (À vérifier=%d, Déjà notifiées=%d, autres=%d)',
             output / 'audit_anciennes_offres.xlsx', len(not_sent), len(sent), len(rejected))
    return {'recherche': len(all_offers), 'archive': len(seen), 'eligible': len(candidates),
            'auditees': len(results), 'a_verifier': len(not_sent), 'deja_notifiees': len(sent)}


def main() -> None:
    p = argparse.ArgumentParser(description='Audit des offres déjà vues, sans email et sans modification de l’historique.')
    p.add_argument('--config', type=Path, default=Path('config.yaml'))
    p.add_argument('--limit', type=int, default=300)
    p.add_argument('--output-dir', type=Path, default=Path('audit-resultats'))
    a = p.parse_args()
    if not 1 <= a.limit <= 2000:
        p.error('--limit doit être entre 1 et 2000')
    summary = audit(a.config, a.limit, a.output_dir)
    print('Audit terminé en lecture seule :', summary)


if __name__ == '__main__':
    main()
