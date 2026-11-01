"""Tests locaux, sans appel API, SMTP ni modification de seen_jobs.json.
Exécution : python -m unittest -v test_job_watcher_ciblage.py
"""
import argparse
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import job_watcher as w

BASE = Path(__file__).resolve().parent
CONFIG = w.load_config(BASE / 'config.yaml')


def offre(titre, description, *, ville='Oldenburg', plz='26121', lat=53.1435,
          lon=8.2146, francais_seul=False, autres_lieux=None):
    locs = autres_lieux or [{'adresse': {'ort': ville, 'plz': plz}, 'breite': lat, 'laenge': lon}]
    j = w.JobResult(refnr='TEST1', title=titre, employer='Entreprise exemple',
                    ort=ville, plz=plz, region='Niedersachsen', published='2026-11-01',
                    latitude=lat, longitude=lon, description=description,
                    localisations=locs, via_francais_seul=francais_seul)
    w.evaluate(j, CONFIG)
    w.classify_source(j, CONFIG)
    return j


def qualifie(j):
    return not j.eliminated and (j.score >= CONFIG['criteres']['seuil_points']
        or j.francais or (j.metier_cible and j.score >= CONFIG['criteres']['seuil_points_cibles']))


class Langues(unittest.TestCase):
    def test_anglais_c1_allemand_b2(self):
        self.assertTrue(qualifie(offre('Linux Administrator', 'Englisch C1 und Deutsch B2. Linux')))

    def test_anglais_c1_seul(self):
        self.assertTrue(qualifie(offre('Linux Administrator', 'English C1 required. Linux')))

    def test_allemand_c1_anglais_b2(self):
        self.assertTrue(offre('Linux Administrator', 'Deutsch C1 und Englisch B2. Linux').eliminated)

    def test_allemand_c1_direct(self):
        self.assertTrue(offre('Linux Administrator', 'Deutschkenntnisse auf C1-Niveau. Linux').eliminated)

    def test_allemand_c2(self):
        self.assertTrue(offre('Linux Administrator', 'Deutschkenntnisse: C2. Linux').eliminated)

    def test_anglais_c1_phrase_avec_allemand(self):
        self.assertFalse(offre('Linux Administrator', 'Sehr gutes Deutsch. Englischkenntnisse C1. Linux').eliminated)

    def test_muttersprachniveau_allemand(self):
        self.assertTrue(offre('Linux Administrator', 'muttersprachliches Deutsch, Linux').eliminated)

    def test_anglais_natif_allemand_b2(self):
        self.assertFalse(offre('Linux Administrator', 'Native English, Deutsch B2, Linux').eliminated)

    def test_souple_allemand_b2(self):
        self.assertFalse(offre('Linux Administrator', 'Deutsch B2 und verhandlungssicheres Deutsch. Linux').eliminated)

    def test_souple_anglais_b2(self):
        self.assertTrue(offre('Linux Administrator', 'verhandlungssicheres Deutsch, Englisch B2, Linux').eliminated)

    def test_niveau_ambigu_ne_bloque_pas(self):
        self.assertFalse(offre('Linux Administrator', 'mindestens C1 (language not named), Linux').eliminated)

    def test_french_c1_not_german(self):
        self.assertFalse(offre('Linux Administrator', 'Französisch C1 und Deutsch B2. Linux').eliminated)


class Teletravail(unittest.TestCase):
    def test_support_distant_pas_remote(self):
        j=offre('System Engineer', 'Remote-Support, Windows Server, Active Directory', ville='Leipzig', plz='04101', lat=51.3, lon=12.3)
        self.assertEqual(j.statut_teletravail,'aucun')
        self.assertTrue(j.eliminated)

    def test_cent_pour_cent_remote(self):
        j=offre('System Engineer', '100% Remote, Windows Server, Active Directory', ville='Stuttgart', plz='70173', lat=48.8, lon=9.15)
        self.assertEqual(j.source,'remote')
        self.assertTrue(qualifie(j))

    def test_full_remote(self):
        j=offre('Linux Administrator', 'Full Remote, Linux', ville='Berlin', plz='10115', lat=52.5, lon=13.4)
        self.assertEqual(j.source, 'remote')

    def test_remote_support_100_pour_cent(self):
        j=offre('IT Support', '100% Remote-Support telefonisch, Linux', ville='Berlin', plz='10115', lat=52.5, lon=13.4)
        self.assertEqual(j.statut_teletravail,'aucun')
        self.assertTrue(j.eliminated)

    def test_homeoffice_partiel(self):
        j=offre('Windows Administrator','3 Tage Homeoffice pro Woche, Windows Server', ville='Stuttgart', plz='70173', lat=48.8, lon=9.15)
        self.assertEqual(j.statut_teletravail, 'partiel')
        self.assertEqual(j.source, 'hors_zone')

    def test_hybride_lointain(self):
        j=offre('IT-Systemadministrator','hybrides Arbeiten, Linux', ville='München', plz='80331', lat=48.1, lon=11.5)
        self.assertEqual(j.source, 'hors_zone')

    def test_kein_homeoffice(self):
        j=offre('IT-Systemadministrator','Kein Homeoffice. Linux', ville='Köln', plz='50667', lat=50.9, lon=6.95)
        self.assertEqual(j.statut_teletravail,'aucun')

    def test_contradiction_100_remote_presence(self):
        j=offre('IT-Systemadministrator','100% Remote. 2 Tage vor Ort pro Woche, Linux', ville='Stuttgart', plz='70173', lat=48.8, lon=9.15)
        # Contradiction explicitement signalée : ne pas prendre le risque.
        self.assertNotEqual(j.source,'remote')

    def test_francais_derogation_distance(self):
        j=offre('IT Support', 'Französischkenntnisse, Linux', ville='Berlin', plz='10115', lat=52.5, lon=13.4)
        self.assertEqual(j.source, 'francais')
        self.assertTrue(qualifie(j))

    def test_ville_oldenburg_sans_gps(self):
        j=offre('Linux Administrator','Linux',lat=None,lon=None)
        self.assertEqual(j.source,'locale')

    def test_ville_bremen_sans_gps(self):
        j=offre('Linux Administrator','Linux',ville='Bremen',plz='28195',lat=None,lon=None)
        self.assertEqual(j.source,'locale')

    def test_ville_ailleurs_sans_gps(self):
        j=offre('Linux Administrator','Linux',ville='Hamburg',plz='20095',lat=None,lon=None)
        self.assertEqual(j.source,'hors_zone')

    def test_plusieurs_lieux_un_est_local(self):
        l=[{'adresse':{'ort':'München','plz':'80331'},'breite':48.1,'laenge':11.5},
           {'adresse':{'ort':'Bremen','plz':'28195'},'breite':53.075,'laenge':8.80}]
        j=offre('Linux Administrator','Linux',ville='München',plz='80331',lat=48.1,lon=11.5,autres_lieux=l)
        self.assertEqual(j.source,'locale')
        self.assertEqual(j.ort,'Bremen')

    def test_faux_oldenburg_holstein(self):
        j=offre('Linux Administrator','Linux', ville='Oldenburg', plz='23758',lat=None,lon=None)
        self.assertEqual(j.source,'hors_zone')


class Scores(unittest.TestCase):
    def test_linux_seul_cible(self):
        j=offre('Linux Administrator','Linux')
        self.assertTrue(j.metier_cible)
        self.assertTrue(qualifie(j))

    def test_windows_ad(self):
        j=offre('Windows Administrator','Windows Server, Active Directory')
        self.assertTrue(qualifie(j))

    def test_support_second_niveau(self):
        j=offre('IT Support 2nd Level','Windows Server')
        self.assertTrue(qualifie(j))

    def test_m365_new_groups(self):
        j=offre('Microsoft 365 Administrator','Entra ID, Intune, Exchange Online, PowerShell')
        self.assertGreaterEqual(j.score_brut, 12)
        self.assertTrue(qualifie(j))

    def test_m365_unseparated_groups(self):
        j=offre('M365 Administrator','Intune')
        self.assertTrue(qualifie(j))

    def test_it_ops_head(self):
        j=offre('Head of IT Operations','IT-Betrieb und Verantwortung')
        self.assertTrue(qualifie(j))

    def test_architecte_exclu(self):
        j=offre('Cloud Security Architect','Linux, Windows Server, AD, VMware, PowerShell, Microsoft 365')
        self.assertTrue(j.eliminated)
        self.assertFalse(qualifie(j))

    def test_architecture_dans_description_pas_exclu(self):
        j=offre('IT Systemadministrator','Linux, Windows Server, Architektur von Systemen')
        self.assertFalse(j.eliminated)
        self.assertTrue(qualifie(j))

    def test_system_engineer_non_it_ne_gagne_pas_de_bonus(self):
        j=offre('System Engineer','Radar, seeker, avionics, flight control, aerospace')
        self.assertEqual(j.bonus_metier, 0)
        self.assertFalse(qualifie(j))

    def test_system_engineer_it_gagne_bonus(self):
        j=offre('System Engineer','Linux, VMware')
        self.assertTrue(j.metier_cible)
        self.assertTrue(qualifie(j))

    def test_devops_penalite_existante(self):
        j=offre('DevOps Engineer','Erfahrung mit CI/CD Pipelines, Linux, VMware')
        self.assertGreater(j.penalite_metier, 0)

    def test_faux_positif_git(self):
        j=offre('Consultant','digitale Projekte und interessant')
        self.assertFalse(any(m.startswith('Git (') for m in j.matched_positive))
        self.assertFalse(any(m.startswith('Storage (') for m in j.matched_positive))

    def test_queries_additionnelles(self):
        queries = CONFIG['criteres']['recherche']['mots_cles']
        self.assertIn('IT Operations', queries)
        self.assertIn('Windows Administrator', queries)
        self.assertIn('Microsoft 365 Administrator',queries)


class Historique(unittest.TestCase):
    def test_ancien_etat_est_compatible(self):
        original = json.loads((BASE/'seen_jobs.json').read_text())
        self.assertIsInstance(original['vus'],dict)
        self.assertGreater(len(original['vus']),1000)
        # La nouvelle version continue d'accepter les horodatages existants.
        refnr, timestamp = next(iter(original['vus'].items()))
        self.assertIsInstance(timestamp, str)

    def test_reexamen_exige_dry_run(self):
        args = argparse.Namespace(config=BASE/'config.yaml', dry_run=False, reset=False, review_seen=True, review_limit=100)
        with patch.object(w,'parse_args',return_value=args):
            with self.assertRaises(SystemExit):
                w.main()

    def test_reexamen_n_envoie_ni_ne_sauvegarde(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'seen_jobs.json').write_text(json.dumps({'vus': {'TEST1': '2026-10-20T11:00:00Z'}}))
            cfg = copy.deepcopy(CONFIG)
            cfg['etat'] = {'fichier':str(Path(d)/'seen_jobs.json')}
            raw = [{'referenznummer':'TEST1','stellenangebotsTitel':'Linux Administrator',
                    'firma':'Test', 'stellenlokationen':[{'adresse':{'ort':'Oldenburg','plz':'26121'},'breite':53.1435,'laenge':8.2146}]}]
            args=argparse.Namespace(config=BASE/'config.yaml',dry_run=True,reset=False,review_seen=True,review_limit=10)
            before=(Path(d)/'seen_jobs.json').read_bytes()
            with patch.object(w,'parse_args',return_value=args), \
                 patch.object(w,'load_config',return_value=cfg), \
                 patch.object(w,'search_jobs',return_value=raw), \
                 patch.object(w,'fetch_description',return_value='Linux'), \
                 patch.object(w,'send_email') as send, \
                 patch.object(w,'load_recent_companies',return_value=[]):
                w.main()
                send.assert_not_called()
            self.assertEqual(before,(Path(d)/'seen_jobs.json').read_bytes())

    def test_nouvelles_offres_envoyees_une_seule_fois(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d)/'seen_jobs.json'
            f.write_text(json.dumps({'vus': {'OLD': '2026-10-20T11:00:00Z'}}))
            cfg = copy.deepcopy(CONFIG)
            cfg['etat'] = {'fichier':str(f)}
            raw = [
                {'referenznummer':r, 'stellenangebotsTitel':'Linux Administrator',
                 'firma':'Test', 'stellenlokationen':[{'adresse':{'ort':'Oldenburg','plz':'26121'},
                 'breite':53.1435,'laenge':8.2146}]}
                for r in ('OLD','NEW')
            ]
            args=argparse.Namespace(config=BASE/'config.yaml',dry_run=False,reset=False,
                                    review_seen=False,review_limit=10)
            with patch.object(w,'parse_args',return_value=args), \
                 patch.object(w,'load_config',return_value=cfg), \
                 patch.object(w,'search_jobs',return_value=raw), \
                 patch.object(w,'fetch_description',return_value='Linux'), \
                 patch.object(w,'load_recent_companies',return_value=[]), \
                 patch.object(w,'send_email',return_value=True) as send:
                w.main()
                self.assertEqual(send.call_count,1)
                self.assertEqual(len(send.call_args.args[1]),1)
                w.main()
                self.assertEqual(send.call_count,1)
            self.assertEqual(set(json.loads(f.read_text())['vus']), {'OLD','NEW'})

    def test_smtp_echec_pas_d_annonce_perdue(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d)/'seen_jobs.json'
            f.write_text(json.dumps({'vus': {'OLD': '2026-10-20T11:00:00Z'}}))
            cfg = copy.deepcopy(CONFIG)
            cfg['etat'] = {'fichier':str(f)}
            raw = [{'referenznummer':'NEW','stellenangebotsTitel':'Linux Administrator',
                    'firma':'Test', 'stellenlokationen':[{'adresse':{'ort':'Oldenburg','plz':'26121'},
                    'breite':53.1435,'laenge':8.2146}]}]
            args=argparse.Namespace(config=BASE/'config.yaml',dry_run=False,reset=False,
                                    review_seen=False,review_limit=10)
            with patch.object(w,'parse_args',return_value=args), \
                 patch.object(w,'load_config',return_value=cfg), \
                 patch.object(w,'search_jobs',return_value=raw), \
                 patch.object(w,'fetch_description',return_value='Linux'), \
                 patch.object(w,'load_recent_companies',return_value=[]), \
                 patch.object(w,'send_email',return_value=False):
                w.main()
            self.assertEqual(set(json.loads(f.read_text())['vus']), {'OLD'})


if __name__ == '__main__':
    unittest.main()
