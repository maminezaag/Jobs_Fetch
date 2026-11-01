"""Tests de régression du ciblage VMware / Citrix / Storage-Backup.

Commande : python -m unittest -v test_job_watcher_specialites.py
Aucun appel réseau, aucun e-mail, aucune lecture/écriture de seen_jobs.json.
"""
import unittest
from pathlib import Path
import job_watcher as watcher

CFG = watcher.load_config(Path(__file__).resolve().parent / "config.yaml")


def annonce(titre, description='', *, ville='Bremen', latitude=53.075, longitude=8.807,
            postal='28195'):
    j = watcher.JobResult(
        refnr='TEST-NOT-REAL', title=titre, employer='TEST GmbH',
        ort=ville, plz=postal, region='BREMEN', published='2026-11-01',
        description=description, latitude=latitude, longitude=longitude,
        localisations=[{'breite': latitude, 'laenge': longitude,
                        'adresse': {'ort': ville, 'plz': postal, 'region': 'BREMEN'}}],
    )
    watcher.evaluate(j, CFG)
    watcher.classify_source(j, CFG)
    return j


def retenue(j):
    crit = CFG['criteres']
    return not j.eliminated and (j.score >= int(crit['seuil_points']) or j.francais or (
        j.metier_cible and not j.penalite_metier
        and j.score >= int(crit['seuil_points_cibles'])
    ))


class RecuperationSpécialites(unittest.TestCase):
    def test_virtualisation_vmware_nutanix_bremen(self):
        j = annonce('Senior Specialist Virtualisierung - VMware & Nutanix', 'VMware vSphere')
        self.assertEqual(j.score_brut, 5)
        self.assertEqual(j.bonus_metier, 5)
        self.assertTrue(retenue(j))
        self.assertEqual(j.priorite_metier, 'Très adapté')

    def test_storage_backup_lemwerder(self):
        j = annonce('Infrastructure Specialist (m/w/d) - Storage & Backup',
                    'Veeam Backups und Datensicherung',
                    ville='Lemwerder', latitude=53.17, longitude=8.6, postal='27809')
        self.assertEqual(j.score_brut, 4)
        self.assertEqual(j.bonus_metier, 5)
        self.assertTrue(retenue(j))
        self.assertEqual(j.priorite_metier, 'Très adapté')

    def test_citrix_infrastruktur_bremen(self):
        j = annonce('Administrator Technisches Verfahrensmanagement - Citrix-Infrastruktur',
                    'Firewall und Citrix-Systemverwaltung')
        self.assertEqual(j.score_brut, 3)
        self.assertEqual(j.bonus_metier, 5)
        self.assertTrue(retenue(j))
        self.assertEqual(j.priorite_metier, 'Très adapté')

    def test_citrix_admin_titre_seul(self):
        j = annonce('Citrix Administrator')
        self.assertTrue(retenue(j))
        self.assertEqual(j.bonus_metier, 5)

    def test_backup_administrator_titre_seul(self):
        j = annonce('Backup Administrator')
        self.assertTrue(retenue(j))

    def test_specialiste_virtualisation_generique_sans_preuve_non_retenu(self):
        j = annonce('Spezialist Virtualisierung', 'Virtuelle Trainings und Marketing')
        self.assertFalse(j.metier_cible)
        self.assertFalse(retenue(j))

    def test_specialiste_virtualisation_avec_preuve_retenu(self):
        j = annonce('Spezialist Virtualisierung', 'Windows Server Administration')
        self.assertEqual(j.bonus_metier, 5)
        self.assertTrue(retenue(j))

    def test_storage_specialist_sans_preuve_non_retenu(self):
        j = annonce('Storage Specialist', 'Logistik und Lagerung von Lebensmitteln')
        self.assertFalse(j.metier_cible)
        self.assertFalse(retenue(j))

    def test_storage_specialist_avec_preuve_retenu(self):
        j = annonce('Storage Specialist', 'NetApp Speichersysteme')
        self.assertEqual(j.bonus_metier, 5)
        self.assertTrue(retenue(j))

    def test_it_anwendungsbetreuer_sans_technique_non_retenu(self):
        j = annonce('IT-Anwendungsbetreuer:in', 'Benutzerschnittstelle Fachabteilungen')
        self.assertFalse(j.metier_cible)
        self.assertFalse(retenue(j))
        self.assertEqual(j.priorite_metier, 'Adapté')

    def test_it_anwendungsbetreuer_avec_technique_retenu(self):
        j = annonce('IT-Anwendungsbetreuer:in', 'Windows Server Betrieb')
        self.assertEqual(j.bonus_metier, 5)
        self.assertTrue(retenue(j))
        self.assertEqual(j.priorite_metier, 'Adapté')

    def test_it_anwendungsbetreuer_clinique_titre_confirme(self):
        j = annonce('IT-Anwendungsbetreuer:in Schwerpunkt Klinische IT-Systeme', '')
        self.assertEqual(j.bonus_metier, 5)
        self.assertTrue(retenue(j))
        self.assertEqual(j.priorite_metier, 'Adapté')

    def test_architecte_vmware_non_retenu(self):
        j = annonce('VMware Solution Architect', 'VMware ESXi')
        self.assertTrue(j.eliminated)
        self.assertFalse(retenue(j))

    def test_allemand_c1_reste_eliminatoire(self):
        j = annonce('Citrix Administrator', 'Citrix VMware. Deutsch C1 erforderlich')
        self.assertTrue(j.eliminated)
        self.assertFalse(retenue(j))

    def test_anglais_c1_allemand_b2_n_elimine_pas(self):
        j = annonce('Citrix Administrator', 'English C1, Deutsch B2')
        self.assertFalse(j.eliminated)
        self.assertTrue(retenue(j))

    def test_hors_zone_sans_remote_non_retenu(self):
        j = annonce('VMware Administrator', 'VMware Infrastruktur',
                    ville='Stuttgart', latitude=48.77, longitude=9.18, postal='70173')
        self.assertTrue(j.eliminated)
        self.assertFalse(retenue(j))

    def test_hors_zone_hybride_non_retenu(self):
        j = annonce('VMware Administrator', 'VMware, zwei Tage Homeoffice pro Woche',
                    ville='Stuttgart', latitude=48.77, longitude=9.18, postal='70173')
        self.assertTrue(j.eliminated)
        self.assertFalse(retenue(j))

    def test_recherches_specialites_presentes(self):
        ks = CFG['criteres']['recherche']['mots_cles']
        for k in ['VMware Administrator','Citrix Administrator','Storage Backup',
                  'IT-Anwendungsbetreuer']:
            with self.subTest(k=k):self.assertIn(k, ks)

    def test_bare_python_developer_pas_bonus(self):
        j = annonce('Python Developer', 'Linux Python')
        self.assertFalse(j.metier_cible)
        self.assertEqual(j.bonus_metier, 0)


if __name__ == '__main__':
    unittest.main()
