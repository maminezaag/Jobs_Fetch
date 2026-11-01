# Jobs_Fetch — correctif ciblage / scoring (01.11.2026)

## Installation (branche `main`)

Remplacer **uniquement** ces trois fichiers à la racine du dépôt :

- `job_watcher.py`
- `criteres.json`
- `config.yaml`

Ajouter, facultativement, le quatrième fichier à la racine :

- `test_job_watcher_ciblage.py` (tests hors ligne)

**Ne PAS toucher** à `seen_jobs.json`, `job_watcher_bochra.py`, `job_watcher_V2.0/`, `stat_jobs/`, `taux_change/`, `university_watcher/` ni aux workflows GitHub.
Ne jamais exécuter `--reset` pour appliquer ce correctif.

Ensuite, **Commit changes** puis **Actions > Job Watcher > Run workflow**. Le workflow exécutera toujours le collecteur IT puis celui de Bochra, comme avant.

## Nouvelles règles

- **Métiers IT très ciblés** : bonus de 5 points au titre, et notification possible dès **5 points** totaux, même si l'annonce comporte peu de mots-clés. Titres génériques (« System Engineer »...) : bonus limité à 3 points **uniquement** si au moins 4 points de vraies compétences IT ont été détectés.
- **Autres offres** : seuil historique de **12 points** maintenu. Pénalité CI/CD DevOps maintenue, sans contournement par le seuil métier réduit.
- **Langue** : les exigences C1/C2/natif sont éliminatoires uniquement lorsqu'elles se rapportent à l'allemand. Le B2 anglais ne neutralise pas un allemand « verhandlungssicher ». En cas d'exigence C1 ambiguë sans langue indiquée, le script ne rejette pas automatiquement.
- **Localisation** : toutes les implantations listées par l'annonce sont examinées. Sans GPS, un repli prudent existe pour les villes proches d'Oldenburg avec code postal cohérent.
- **Télétravail** : au-delà de 70 km, seuls les cas clairement **100% télétravail** passent, sauf la dérogation historique des annonces demandant le français. « Remote-Support », travail hybride, 2–3 jours de Homeoffice et « kein Homeoffice » ne rendent plus un poste éloigné admissible.
- **Métiers d'architecte** : un titre comportant « Architect/Architekt » est écarté, mais une description contenant « architecture » ne l'est pas.
- **Découverte** : mots-clés supplémentaires pour administration Windows, Microsoft 365, IT Operations, Azure, IT-Systembetreuer, etc., et points pour Microsoft 365, Entra ID, Intune, Exchange, PowerShell, Azure.

Les nouveaux scores affichent séparément points techniques, bonus métier et éventuelle pénalité. Les annonces exclues par la distance ne sont pas envoyées, même si le score est élevé (sauf exception française).

## Historique `seen_jobs.json`

Le fichier reste **100% compatible** et n'est PAS livré dans ce ZIP. Les anciennes annonces analysées ne sont pas renvoyées automatiquement, car l'historique précédent ne distingue pas les annonces notifiées des annonces rejetées.

Un **réexamen sécurisé** est possible, sur une machine disposant de l'accès à l'API :

```bash
python job_watcher.py --config config.yaml --dry-run --review-seen --review-limit 150
```

Ce mode n'envoie **aucun email** et ne modifie **aucune archive**. Il réexamine seulement les annonces précédemment vues qui sont encore retournées par l'API Jobsuche (période de recherche actuellement 7 jours). Un résultat positif peut déjà avoir été envoyé dans le passé : l'ancienne archive ne contient pas cette information.

## Validation

```bash
python -m unittest -q test_job_watcher_ciblage.py
python -m py_compile job_watcher.py
python -m json.tool criteres.json > /dev/null
```

**44 tests automatisés hors ligne réussis**, y compris envoi réussi/échoué (SMTP simulé), historique, langue, télétravail, scoring et métier. L'API de production et les e-mails réels ne sont pas testés par cette suite. Aucun collecteur ne peut garantir 100% des offres publiées par une API tierce.
