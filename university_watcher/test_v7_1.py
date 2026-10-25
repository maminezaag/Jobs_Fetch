"""Tests V7.1 : jamais de priorité sans domaine principal identifié."""
import json
import unittest
from pathlib import Path
from adequation import evaluer_adequation
from watcher import Offre, noter
CFG = json.loads((Path(__file__).parent / 'criteres.json').read_text(encoding='utf-8'))

class CasIncertain(unittest.TestCase):
    def test_unknown_domain_is_review(self):
        a = evaluer_adequation('Advanced Data Specialist',
            'Linux VMware Puppet Ansible Bash Docker Python. TV-L E13, 100%.', CFG['adequation_v7'])
        self.assertEqual(a['domaine'], 'INCERTAIN')
        self.assertEqual(a['action'], 'EXAMEN')

    def test_unknown_domain_high_score_is_secondary(self):
        o = Offre('oldenburg','Advanced Data Specialist', 'https://uol.de/job-unclassified',
                  'Oldenburg','Linux VMware Puppet Ansible Bash Docker Python. TV-L E13, 100%.')
        n = noter(o, CFG)
        self.assertGreaterEqual(n['score'],60)
        self.assertGreaterEqual(n['skills'],20)
        self.assertEqual(n['adequation']['domaine'],'INCERTAIN')
        self.assertEqual(n['priorite'],'A_EXAMINER')
        self.assertFalse(n['eligible'])
        self.assertTrue(n['alerte'])

    def test_unknown_low_score_stays_excluded(self):
        o = Offre('oldenburg','Mitarbeit Verwaltung', 'https://uol.de/job-other', 'Oldenburg','E11 75%')
        n = noter(o, CFG)
        self.assertEqual(n['adequation']['action'],'EXAMEN')
        self.assertEqual(n['priorite'],'ECARTEE')
        self.assertFalse(n['alerte'])

    def test_known_it_stays_priority(self):
        t = ('Your tasks: Administration of Linux servers and server infrastructure; '
             'infrastructure automation with Puppet and Ansible; monitoring and IT support. '
             'Your profile: Required qualifications: Linux system administration, Ansible, '
             'Bash, Puppet, VMware. We offer E13 TV-L 100%.')
        o = Offre('oldenburg','IT Administrator Linux','https://uol.de/job-it','Oldenburg',t)
        n = noter(o, CFG)
        self.assertEqual(n['adequation']['domaine'],'ADMINISTRATION_IT')
        self.assertEqual(n['priorite'],'PRIORITAIRE')

if __name__=='__main__': unittest.main()
