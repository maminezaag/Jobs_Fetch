"""V7.2 : aucune PRIORITÉ sur la seule foi d'un titre IT ou BPMN/IoT."""
import json
import unittest
from pathlib import Path
from adequation import evaluer_adequation
from watcher import Offre, noter

CFG = json.loads((Path(__file__).parent / 'criteres.json').read_text(encoding='utf-8'))

class VerificationMetier(unittest.TestCase):
    def test_it_titre_sans_missions_nest_pas_prioritaire(self):
        offre = Offre('oldenburg', 'IT Administrator', 'https://example.org/poste-it', 'Oldenburg',
            'Linux VMware Puppet Ansible Bash Docker TV-L E13 100%')
        note = noter(offre, CFG)
        self.assertGreaterEqual(note['score'], 60)
        self.assertEqual(note['adequation']['domaine'], 'IT_PRESUMEE')
        self.assertEqual(note['adequation']['action'], 'EXAMEN')
        self.assertEqual(note['priorite'], 'A_EXAMINER')
        self.assertFalse(note['eligible'])

    def test_bpm_titre_sans_missions_nest_pas_prioritaire(self):
        offre = Offre('oldenburg', 'Research Assistant BPMN et IoT', 'https://example.org/poste-bpm', 'Oldenburg',
            'BPMN IoT Python Docker Linux Ansible VMware Puppet TV-L E13 100%')
        note = noter(offre, CFG)
        self.assertGreaterEqual(note['score'], 60)
        self.assertEqual(note['adequation']['domaine'], 'RECHERCHE_CIBLE_PRESUMEE')
        self.assertEqual(note['adequation']['action'], 'EXAMEN')
        self.assertEqual(note['priorite'], 'A_EXAMINER')

    def test_it_missions_confirmees_reste_prioritaire(self):
        txt = ('Your tasks Administration of Linux servers and server infrastructure. '
               'IT support and infrastructure automation. Your profile Required qualifications: '
               'Linux, Bash, Ansible, Puppet, VMware. We offer TV-L E13 100%.')
        note = noter(Offre('oldenburg','IT Administrator','https://example.org/it','Oldenburg',txt),CFG)
        self.assertEqual(note['adequation']['domaine'], 'ADMINISTRATION_IT')
        self.assertEqual(note['adequation']['action'], 'OK')
        self.assertEqual(note['priorite'], 'PRIORITAIRE')

    def test_bpm_missions_confirmees_reste_prioritaire(self):
        txt = ('Your tasks Design BPMN workflows for business process management and IoT sensor networks. '
               'Process automation and information systems research. '
               'Your profile Required qualifications: Master and experience in BPMN and IoT, '
               'Python and MySQL. We offer TV-L E13 100%.')
        note = noter(Offre('oldenburg','Research Assistant BPMN IoT','https://example.org/bpm','Oldenburg',txt),CFG)
        self.assertEqual(note['adequation']['domaine'], 'RECHERCHE_BPM_IOT')
        self.assertEqual(note['adequation']['action'], 'OK')
        self.assertEqual(note['priorite'], 'PRIORITAIRE')

if __name__ == '__main__':
    unittest.main()
