"""Régression hors ligne du correctif français / doublons / priorité métier.

Lancer : python -m unittest -v test_job_watcher_doublons_priorites.py
Aucun appel réseau ; ne lit ni ne modifie l'archive réelle.
"""
import argparse
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import job_watcher as w

CFG = w.load_config(Path(__file__).resolve().parent / 'config.yaml')


def annonce(ref, titre, desc, *, employer='Test GmbH', ville='Bremen', plz='28195',
            region='BREMEN', published='2026-11-01', francais_seul=False):
    gps = (53.075, 8.807) if ville == 'Bremen' else (50.58, 8.50)
    j = w.JobResult(refnr=ref, title=titre, employer=employer, ort=ville,
                    plz=plz, region=region, published=published,
                    latitude=gps[0], longitude=gps[1], description=desc,
                    via_francais_seul=francais_seul,
                    localisations=[{'breite': gps[0], 'laenge': gps[1],
                        'adresse': {'ort': ville, 'plz': plz, 'region': region}}])
    w.evaluate(j, CFG)
    w.classify_source(j, CFG)
    return j


LONG_DESCRIPTION = ('Installation Wartung Linux Windows Server Active Directory VMware ' +
                    'Infrastruktur Backup Netzwerke Benutzerkonten Betrieb von Servern ' +
                    'Monitoring und Administration am Standort. ')*3


class French(unittest.TestCase):
    def test_consultant_sap_fi_exclu_meme_support_description(self):
        j = annonce('F1','Inhouse Consultant SAP S/4 FI (m/w/d)',
                    'Französischkenntnisse. 1st Level Support. SAP FI.',
                    employer='ROSA Experts', ville='Wetzlar', plz='35576')
        self.assertTrue(j.eliminated)
        self.assertIn('métier hors IT', '; '.join(j.matched_elimination))

    def test_consultant_sap_service_exclu(self):
        j = annonce('F2','Inhouse Consultant SAP S/4 HANA Service (m/w/d)',
                    'Französischkenntnisse. IT Support im Unternehmen.',
                    ville='Wetzlar', plz='35576')
        self.assertTrue(j.eliminated)

    def test_francais_it_support_berlin_garde(self):
        j = annonce('F3','IT Support 2nd Level',
                    'Französischkenntnisse. Windows Server Administration.',
                    ville='Berlin', plz='10115', region='BERLIN')
        self.assertFalse(j.eliminated)
        self.assertEqual(j.source, 'francais')

    def test_francais_systemadministrator_wetzlar_garde(self):
        j = annonce('F4','Linux Systemadministrator', 'Französischkenntnisse. Linux.',
                    ville='Wetzlar', plz='35576', region='HESSEN')
        self.assertFalse(j.eliminated)
        self.assertEqual(j.source, 'francais')

    def test_francais_suisse_rothrist_exclu(self):
        j = annonce('F5','IT Field Engineer', 'Französischkenntnisse. IT Services Linux',
                    ville='Rothrist', plz='', region='')
        self.assertTrue(j.eliminated)
        self.assertIn('localisation allemande', '; '.join(j.matched_elimination))

    def test_field_engineer_sans_it_exclu(self):
        j = annonce('F6','Field Engineer','Französischkenntnisse. Flight control radar aviation',
                    ville='Wetzlar', plz='35576')
        self.assertTrue(j.eliminated)

    def test_field_engineer_it_garde(self):
        j = annonce('F7','Field Engineer','Französischkenntnisse. Windows Server VMware',
                    ville='Wetzlar', plz='35576')
        self.assertFalse(j.eliminated)

    def test_francais_cloud_engineer_lien_linux(self):
        j = annonce('F9', 'Cloud Engineer',
                    'Französischkenntnisse Linux Server Azure Administration',
                    ville='Wetzlar', plz='35576')
        self.assertFalse(j.eliminated)

    def test_francais_consultant_it_linux(self):
        j = annonce('F10', 'IT Consultant',
                    'Französischkenntnisse VMware Linux Administration',
                    ville='Wetzlar', plz='35576')
        self.assertFalse(j.eliminated)

    def test_francais_it_application_support(self):
        j = annonce('F11', 'IT Application Support',
                    'Französischkenntnisse und Betreuung von Nutzeranfragen',
                    ville='Wetzlar', plz='35576')
        self.assertFalse(j.eliminated)

    def test_francais_allemand_c1_bloque_toujours(self):
        j = annonce('F8','Linux Systemadministrator','Französischkenntnisse Deutsch C1 Linux',
                    ville='Wetzlar', plz='35576')
        self.assertTrue(j.eliminated)


class Priorites(unittest.TestCase):
    def test_head_of_it_operations(self):
        self.assertEqual(annonce('P1','Head of IT Operations', 'IT-Betrieb').priorite_metier,
                         'Très adapté')
    def test_datacenter(self):
        self.assertEqual(annonce('P2','Plattform & Datacenter Engineer', LONG_DESCRIPTION).priorite_metier,
                         'Très adapté')
    def test_network_linux(self):
        self.assertEqual(annonce('P3','IT Netzwerk Ingenieur Linux', LONG_DESCRIPTION).priorite_metier,
                         'Très adapté')
    def test_fachinformatiker(self):
        self.assertEqual(annonce('P4','Fachinformatiker für Systemintegration', LONG_DESCRIPTION).priorite_metier,
                         'Adapté')
    def test_python_dev_examiner_score_inchange(self):
        j = annonce('P5','Python Developer mit fachlicher Koordination',
                    '100% Remote, Linux Python PowerShell')
        self.assertEqual(j.priorite_metier, 'À examiner')
        self.assertGreaterEqual(j.score, 12)
        self.assertEqual(w._lignes_offre(j)[2], f'  Priorité métier : {j.priorite_metier}')
    def test_ordre_dans_email(self):
        a = annonce('P6','Python Developer', 'Linux Python PowerShell')
        b = annonce('P7','Head of IT Operations', 'Linux')
        s = w.build_email_body([a,b])
        self.assertLess(s.index('Head of IT Operations'), s.index('Python Developer'))


class Dedoublonnage(unittest.TestCase):
    def test_stadt_vechta_deux_refs(self):
        a = annonce('BA-1','Fachinformatiker für Systemintegration (m/w/d)',LONG_DESCRIPTION,
                    employer='Stadt Vechta',ville='Vechta',plz='49377',region='NIEDERSACHSEN',published='2026-10-30')
        b = annonce('BA-2','Fachinformatiker für Systemintegration',LONG_DESCRIPTION,
                    employer='Stadt Vechta',ville='Vechta',plz='49377',region='NIEDERSACHSEN',published='2026-10-29')
        uniques,doublons = w._dedupliquer_qualifies([a,b])
        self.assertEqual((len(uniques),len(doublons)), (1,1))

    def test_deux_metiers_meme_entreprise_non_fusionnes(self):
        a = annonce('BA-3','IT-Systemadministrator Identity & Access Management',LONG_DESCRIPTION,
                    employer='My Humancapital')
        b = annonce('BA-4','IT-Systemadministrator Web & Applikationen',LONG_DESCRIPTION,
                    employer='My Humancapital')
        self.assertEqual(len(w._dedupliquer_qualifies([a,b])[0]), 2)

    def test_deux_annonces_meme_titre_desc_differente(self):
        a = annonce('BA-5','Systemadministrator Linux',LONG_DESCRIPTION)
        b = annonce('BA-6','Systemadministrator Linux',
                    ('Sécurité web design UX graphisme marketing illustrations ' * 4))
        self.assertFalse(w._sont_memes_annonces(a,b))

    def test_munchen_munich(self):
        self.assertEqual(w._norm_ville_doublon('Munich'), w._norm_ville_doublon('München'))

    def test_notifications_uniquement_sur_email_reussi(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp)/'seen_jobs.json'
            f.write_text(json.dumps({'vus': {}}))
            cfg = copy.deepcopy(CFG)
            cfg['etat']={'fichier':str(f)}
            raw = []
            for ref,title in [('BA-1','Fachinformatiker für Systemintegration (m/w/d)'),
                              ('BA-2','Fachinformatiker für Systemintegration')]:
                raw.append({'referenznummer':ref,'stellenangebotsTitel':title,
                            'firma':'Stadt Vechta','datumErsteVeroeffentlichung':'2026-11-01',
                            'stellenlokationen':[{'breite':52.73,'laenge':8.29,
                            'adresse':{'ort':'Vechta','plz':'49377','region':'NIEDERSACHSEN'}}]})
            args = argparse.Namespace(config=Path('config.yaml'),dry_run=False,
                                      reset=False,review_seen=False,review_limit=10)
            with patch.object(w,'parse_args',return_value=args),\
                 patch.object(w,'load_config',return_value=cfg),\
                 patch.object(w,'search_jobs',return_value=raw),\
                 patch.object(w,'fetch_description',return_value=LONG_DESCRIPTION),\
                 patch.object(w,'load_recent_companies',return_value=[]),\
                 patch.object(w,'send_email',return_value=True) as send,\
                 patch.object(w.time,'sleep'):
                w.main()
                self.assertEqual(send.call_count,1)
                self.assertEqual(len(send.call_args.args[1]),1)
                st=json.loads(f.read_text())
                self.assertEqual(len(st['vus']),2)
                self.assertEqual(len(st['notifications']),1)
                w.main()
                self.assertEqual(send.call_count,1)

    def test_smtp_failed_preserves_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'seen_jobs.json'
            f.write_text(json.dumps({'vus': {}}))
            cfg=copy.deepcopy(CFG)
            cfg['etat']={'fichier':str(f)}
            raw=[{'referenznummer':'FAIL','stellenangebotsTitel':'Linux Administrator',
                  'firma':'Company A','datumErsteVeroeffentlichung':'2026-11-01',
                  'stellenlokationen':[{'breite':53.075,'laenge':8.807,
                    'adresse':{'ort':'Bremen','plz':'28195','region':'BREMEN'}}]}]
            args=argparse.Namespace(config=Path('config.yaml'),dry_run=False,
                       reset=False,review_seen=False,review_limit=10)
            with patch.object(w,'parse_args',return_value=args),\
                 patch.object(w,'load_config',return_value=cfg),\
                 patch.object(w,'search_jobs',return_value=raw),\
                 patch.object(w,'fetch_description',return_value=LONG_DESCRIPTION),\
                 patch.object(w,'load_recent_companies',return_value=[]),\
                 patch.object(w,'send_email',return_value=False),\
                 patch.object(w.time,'sleep'):
                w.main()
                self.assertNotIn('FAIL',json.loads(f.read_text())['vus'])
                self.assertNotIn('notifications',json.loads(f.read_text()))

    def test_historique_ancien_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'seen_jobs.json'
            p.write_text('{"vus":{"OLD":"2026-10-31T10:00:00Z"}}')
            self.assertEqual(w.load_state(p)['vus']['OLD'],'2026-10-31T10:00:00Z')

    def test_croisement_notifie_mais_pas_rejete(self):
        j=annonce('NEW','Linux Administrator',LONG_DESCRIPTION,published='2026-11-01')
        archive=[w._empreinte_notification(annonce('OLD','Linux Administrator', LONG_DESCRIPTION,
                                                   published='2026-10-31'))]
        self.assertTrue(w._deja_notifie_autre_reference(j,archive))
        self.assertFalse(w._deja_notifie_autre_reference(j,[]))


if __name__=='__main__':
    unittest.main()
