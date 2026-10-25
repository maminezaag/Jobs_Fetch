"""Régressions V6 : pondérations Jobs_Fetch, deux voies, deux alertes, cache."""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from email import message_from_string
from pathlib import Path
from unittest.mock import Mock, patch
import watcher
from watcher import Offre, noter, exclure, analyser_une_offre, emailer, branche_offre, decision_apres_verification

CFG = json.loads((Path(__file__).parent / 'criteres.json').read_text(encoding='utf8'))

class V6ScoringTests(unittest.TestCase):
    def offer(self, title, text='', location='Oldenburg'):
        return Offre('oldenburg',title,'https://uol.de/jobs/demo',location,text)

    def test_it_administrator_and_jobs_fetch_weights(self):
        n=noter(self.offer('IT Administrator Linux VMware Ansible Bash',
                           'TV-L E13, 100%'),CFG)
        self.assertTrue(n['eligible'])
        self.assertEqual(n['priorite'],'PRIORITAIRE')
        self.assertEqual(n['skills'],27)
        self.assertEqual(n['score'],72)
        self.assertEqual(n['bonus_specialite'],4)
        self.assertEqual(dict(n['competences'])['Bash / Shell'],5)
        self.assertEqual(dict(n['competences'])['Ansible'],4)

    def test_linux_synonyms_do_not_stack(self):
        n=noter(self.offer('Linux RHEL Ubuntu SUSE RedHat'),CFG)
        self.assertEqual(dict(n['competences'])['Linux / Red Hat / Ubuntu / SUSE'],5)

    def test_vmware_and_proxmox_max5_not_additive(self):
        n=noter(self.offer('Virtualisierung VMware ESXi Proxmox VE'),CFG)
        self.assertEqual(dict(n['competences'])['VMware / Proxmox'],5)

    def test_vmware_without_proxmox_four(self):
        n=noter(self.offer('VMware vSphere ESXi'),CFG)
        self.assertEqual(dict(n['competences'])['VMware / Proxmox'],4)

    def test_genuine_it_job_at_secondary_level(self):
        n=noter(self.offer('IT-Systemadministrator Linux VMware',
                         'TV-L E13 100%'),CFG)
        self.assertEqual(n['skills'],18)
        self.assertFalse(n['eligible'])
        self.assertTrue(n['alerte'])
        self.assertEqual(n['priorite'],'A_EXAMINER')

    def test_research_bpm_iot_bonus(self):
        n=noter(self.offer('Wissenschaftlicher Mitarbeiter Promotionsstelle BPMN IoT',
                         'BPM, IoT, Python, Workflow Automation, TV-L E13 100%'),CFG)
        self.assertEqual(n['branche'],'RECHERCHE')
        self.assertEqual(n['bonus_specialite'],5)
        self.assertTrue(n['eligible'])
        self.assertEqual(n['skills'],22)

    def test_research_no_free_bonus_for_generic_phd(self):
        n=noter(self.offer('Wissenschaftlicher Mitarbeiter Doktorand Literatur',
                          'Geschichte, Sprachwissenschaft, TV-L E13 Vollzeit'),CFG)
        self.assertEqual(n['bonus_specialite'],0)
        self.assertFalse(n['alerte'])

    def test_neutral_research_topic_needs_technical_match(self):
        n=noter(self.offer('Wissenschaftliche:r Mitarbeiter:in Promotion IoT BPM',
                          'TV-L E13 100%'),CFG)
        self.assertEqual(n['priorite'],'A_EXAMINER')
        self.assertEqual(n['skills'],15)

    def test_not_overreward_IT_admin_with_zero_technical_stack(self):
        n=noter(self.offer('IT Administrator','TV-L E13 100%'),CFG)
        self.assertEqual(n['bonus_specialite'],0)
        self.assertFalse(n['alerte'])

    def test_reasons_and_c1_block_both_tiers(self):
        n=noter(self.offer('IT Administrator Linux VMware Ansible Bash',
              'TV-L E13 100% Deutsche Sprache (mind. CEF-Level C1)'),CFG)
        self.assertFalse(n['eligible'])
        self.assertFalse(n['alerte'])
        self.assertEqual(n['priorite'],'ECARTEE')

    def test_english_c1_does_not_block(self):
        n=noter(self.offer('IT Administrator Linux VMware Ansible Bash',
              'TV-L E13 100% Englisch mindestens C1. Deutsch B2 ausreichend'),CFG)
        self.assertTrue(n['alerte'])

    def test_distant_no_remote_block_both_tiers(self):
        n=noter(self.offer('IT Administrator Linux VMware Ansible Bash',
                    'TV-L E13, 100%', 'München'),CFG)
        self.assertFalse(n['alerte'])
        self.assertEqual(n['priorite'],'ECARTEE')

    def test_inaccessible_detail_blocks_secondary(self):
        o=self.offer('IT-Systemadministrator Linux VMware','TV-L E13 100%')
        n=noter(o,CFG)
        self.assertTrue(n['alerte'])
        decision_apres_verification(o,n)
        self.assertFalse(n['alerte'])
        self.assertEqual(n['priorite'],'ECARTEE')

    def test_certifications_and_unique_monitoring_technologies(self):
        n=noter(self.offer('Checkmk Nagios RHCSA LPIC-2'), CFG)
        matched=dict(n['competences'])
        self.assertEqual(matched['Checkmk / Nagios'],5)
        self.assertEqual(matched['Zertifizierungen (RHCSA/AZ-104/LPIC)'],3)

    def test_changes_v6_signature_and_uses_cache(self):
        o=self.offer('IT-Systemadministrator Linux VMware','TV-L E13 100%')
        col=Mock()
        col.page_text.return_value='Linux VMware TV-L E13, 100%'
        first,entry,_=analyser_une_offre(o,col,CFG['sources']['oldenburg'],None,CFG,date(2026,10,25))
        self.assertTrue(first['alerte'])
        new_o=self.offer('IT-Systemadministrator Linux VMware','TV-L E13 100%')
        updated=dict(CFG)
        updated['bonus_specialite']={'it_administration':7,'recherche_bpm_iot':5}
        second,entry2,status=analyser_une_offre(new_o,col,CFG['sources']['oldenburg'],entry,updated,date(2026,10,26))
        self.assertEqual(status,'nouveaux critères : re-scoring du cache')
        self.assertEqual(col.page_text.call_count,1)
        self.assertNotEqual(first['skills'],second['skills'])

    def test_secondary_email_contains_two_sections(self):
        a=self.offer('IT Administrator Linux VMware Ansible Bash','TV-L E13 100%')
        b=self.offer('IT-Systemadministrator Linux VMware','TV-L E13 100%')
        notes=[(a,noter(a,CFG)),(b,noter(b,CFG))]
        smtp=Mock()
        with patch.object(watcher.smtplib,'SMTP_SSL') as m:
            m.return_value.__enter__.return_value=smtp
            emailer(notes,'to@example.org','from@example.org','fakepassword')
        msg=smtp.send_message.call_args.args[0]
        self.assertIn('1 prioritaires, 1 à examiner',msg['Subject'])
        self.assertIn('PRIORITAIRES',msg.get_content())
        self.assertIn('À EXAMINER',msg.get_content())

    def test_original_job_watcher_unchanged_by_package(self):
        self.assertEqual(CFG['seuil_total'],60)
        self.assertEqual(CFG['minimum_competences'],20)
        self.assertEqual(CFG['alerte_secondaire']['minimum_competences'],10)
        self.assertEqual(CFG['alerte_secondaire']['seuil_total'],55)


class V6SecondTierIntegration(unittest.TestCase):
    def test_secondary_alert_once_despite_cache(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            config=dict(CFG)
            config['sources']={'oldenburg':CFG['sources']['oldenburg']}
            (base/'criteres.json').write_text(json.dumps(config,ensure_ascii=False))
            (base/'seen_university_jobs.json').write_text('[]')
            col=Mock()
            col.collect.side_effect=lambda *args:[Offre('oldenburg','IT-Systemadministrator Linux VMware',
                        'https://uol.de/jobs/it1','Oldenburg','TV-L E13 100%')]
            col.page_text.return_value='Linux VMware TV-L E13 100%'
            with (patch.object(watcher,'BASE',base),patch.object(watcher,'Collecteur',return_value=col),
                  patch.object(watcher,'emailer') as sender,
                  patch.dict(os.environ,{'GMAIL_ADDRESS':'from@example.org',
                                         'GMAIL_APP_PASSWORD':'test','GMAIL_TO':'to@example.org'}),
                  patch('sys.argv',['watcher.py'])):
                self.assertEqual(watcher.main(),0)
                self.assertEqual(sender.call_count,1)
                self.assertEqual(watcher.main(),0)
                self.assertEqual(sender.call_count,1)
                self.assertEqual(col.page_text.call_count,1)
                history=json.loads((base/'seen_university_jobs.json').read_text())
                self.assertEqual(len(history),1)

if __name__=='__main__':unittest.main()
