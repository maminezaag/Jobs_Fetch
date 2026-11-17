# Jobs Fetch — un e-mail avec deux sections (17 novembre 2026)

## Installation dans le dépôt GitHub `Jobs_Fetch`

Décompresser le ZIP puis, **à la racine du dépôt**, remplacer :

1. `job_watcher.py` — une seule notification Gmail, regroupant les offres utiles.
2. `criteres.json` — règles *très ciblées* pour ne pas notifier les métiers manifestement hors profil.
3. `test_job_watcher_compatibilite.py` — remplacer ce test si l'ancienne version « deux e-mails » est déjà installée.
4. `test_job_watcher_doublons_priorites.py` — mettre à jour ce test ancien (son ancien contrôle supposait encore que Python Developer soit notifié).

Commit changes sur `main`, puis GitHub **Actions → Job Watcher → Run workflow**.

**NE PAS modifier/supprimer/réinitialiser `seen_jobs.json`.** Ne pas remplacer `config.yaml`, `.github/workflows/job_watcher.yml`, `job_watcher_bochra.py`, `stat_jobs/`, `job_watcher_V2.0/`, les modules universitaires ou de taux de change.

## Nouveau comportement

- Le **scoring et la collecte restent inchangés** : seuil normal 12, seuil métier ciblé 5, règles allemand C1/B2, distance, télétravail et dédoublonnage conservés.
- Le script envoie **UN SEUL E-MAIL PAR EXÉCUTION** (s'il y a des offres à notifier), avec deux sections :
  - `OFFRES PRIORITAIRES` : systèmes, Linux/Windows, VMware, AD, Citrix, sauvegardes, IT Operations, 2nd/3rd Level.
  - `OFFRES À EXAMINER` : proches mais demandant une vérification (DevOps généraliste, M365/Intune, autres rôles voisins).
- Des postes manifestement hors domaine (par ex. **Senior Cloud Native Storage Engineer – Ceph**, **DevOps Software Engineer**, développement Python pur) **ne sont plus notifiés**. Certains postes DevOps ou réseau senior ne sont écartés **que si des expertises avancées sont explicitement obligatoires** ; une compétence simplement souhaitable ne suffit pas.
- Tous les postes examinés restent dans l'historique `vus`, même lorsqu'ils ne sont pas notifiés. L'audit manuel permet de les réexaminer plus tard.
- Si SMTP échoue, toutes les offres destinées à cet e-mail restent retentables au passage suivant. Seul un envoi SMTP réussi ajoute les empreintes au registre `notifications`.
- Les offres déjà vues dans `seen_jobs.json` ne sont **pas renvoyées** du seul fait de ce correctif.

## Tester localement (facultatif)

```bash
python -m unittest discover -p 'test_job_watcher*.py' -q
```

103 tests réussis sur la base fournie, sans appel au site Arbeitsagentur ni à Gmail, et sans modification de l'historique réel.

## Fichiers du ZIP

- `job_watcher.py` (modifié)
- `criteres.json` (modifié)
- `test_job_watcher_compatibilite.py` (mis à jour)
- `test_job_watcher_doublons_priorites.py` (mis à jour)
- `README_INSTALLATION_1_EMAIL.md` (ces instructions)

**Limite :** il s'agit d'un filtrage par règles, pas d'une compréhension parfaite de toutes les offres. Un intitulé ambigu reste à examiner plutôt qu'à exclure. Les résultats réels seront à confirmer avec les prochaines annonces.
