# Job Watcher — Arbeitsagentur

Surveille la Jobbörse de la Bundesagentur für Arbeit via l'API officielle
"Jobsuche" (pas de scraping HTML), combine une recherche locale (rayon autour
d'Oldenburg) et une recherche remote nationale, évalue chaque **nouvelle**
offre selon tes critères, vérifie si tu as peut-être déjà postulé chez
l'employeur (Google Sheet), et t'envoie un e-mail récapitulatif.

## Fichiers du projet

| Fichier | Rôle |
|---|---|
| `job_watcher.py` | script principal |
| `config.yaml` | configuration **technique** (chemins, e-mail, seuil) |
| `criteres.json` | configuration **métier** (mots-clés, élimination, points) — c'est LUI qu'on édite pour ajuster les critères |
| `seen_jobs.json` | historique des offres déjà notifiées — généré automatiquement, ne pas éditer |
| `.github/workflows/job_watcher.yml` | workflow GitHub Actions |

## 1. Test en local

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # puis remplis GMAIL_ADDRESS / GMAIL_APP_PASSWORD / GMAIL_TO

export $(grep -v '^#' .env | xargs)
python job_watcher.py --config config.yaml --dry-run
```

Le `--dry-run` affiche le résultat dans la console sans envoyer d'e-mail —
utile pour vérifier que la recherche et le scoring fonctionnent avant de
brancher quoi que ce soit sur GitHub.

Pour obtenir un mot de passe d'application Gmail : active la validation en 2
étapes sur ton compte Google, puis crée-en un sur
https://myaccount.google.com/apppasswords — n'utilise jamais ton mot de passe
de connexion normal ici.

## 2. Mise en place du repo GitHub

1. Crée un repo GitHub (public ou privé, les deux fonctionnent) et pousse-y
   tous les fichiers de ce dossier (`job_watcher.py`, `config.yaml`,
   `criteres.json`, `requirements.txt`, `.github/workflows/job_watcher.yml`,
   et un `seen_jobs.json` initial contenant `{"vus": {}}`).
2. **Ne pousse jamais `.env`** (ajoute-le à `.gitignore`) — les secrets
   passent uniquement par GitHub Secrets, pas par un fichier dans le repo.
3. Dans le repo GitHub : **Settings → Secrets and variables → Actions →
   New repository secret**, crée les trois secrets suivants :
   - `GMAIL_ADDRESS`
   - `GMAIL_APP_PASSWORD`
   - `GMAIL_TO`
4. Teste manuellement : onglet **Actions** du repo → workflow "Job Watcher"
   → bouton **Run workflow**. Vérifie les logs, et que `seen_jobs.json` est
   bien recommité automatiquement à la fin.

## 3. Déclenchement planifié via crontab.org (webhook)

Volontairement, ce workflow n'utilise **pas** le planificateur natif GitHub
Actions (`schedule: cron:`), pour éviter les problèmes de fiabilité que tu as
rencontrés avec lui. À la place, crontab.org appelle directement l'API GitHub
pour déclencher le workflow, via un événement `repository_dispatch`.

### 3.1 Créer un token GitHub

Va sur **GitHub → Settings (compte) → Developer settings → Personal access
tokens → Fine-grained tokens → Generate new token** :
- Repository access : limite-le à ce seul repo
- Permissions : **Contents → Read and write**, **Actions → Read and write**
- Copie le token généré (il ne sera plus jamais affiché) — garde-le secret,
  il donne accès à ce repo.

### 3.2 Configurer la tâche sur crontab.org

Crée une nouvelle tâche crontab.org avec :

- **URL** :
  `https://api.github.com/repos/<TON_UTILISATEUR>/<TON_REPO>/dispatches`
- **Méthode** : `POST`
- **En-têtes (headers)** :
  ```
  Accept: application/vnd.github+json
  Authorization: Bearer <TON_TOKEN_GITHUB>
  Content-Type: application/json
  ```
- **Corps (body)** :
  ```json
  {"event_type": "run-job-watcher"}
  ```
- **Planification** : selon ta préférence (ex. tous les jours à 7h)

⚠️ Le `event_type` dans le corps JSON doit correspondre **exactement** à la
valeur `types:` dans `.github/workflows/job_watcher.yml` (`run-job-watcher`).

### 3.3 Vérifier

Après le prochain déclenchement crontab.org, va dans l'onglet **Actions** du
repo GitHub : un nouveau run "Job Watcher" doit apparaître, déclenché par
`repository_dispatch`.

## Personnaliser les critères

Tout ce qui définit **quoi chercher** et **comment noter** les offres est
dans `criteres.json` — mots-clés de recherche, expressions qui éliminent une
offre (niveau d'allemand, etc.), compétences pondérées. Chaque section du
fichier a sa propre clé `_aide` qui explique comment ajouter ou supprimer un
élément, avec des exemples.

`config.yaml` ne contient que la technique (seuil de points, e-mail, chemin
du Google Sheet de suivi de candidatures) — pas besoin d'y toucher pour
ajuster les critères de recherche.

## Notes

- L'API utilisée est publique et documentée par le projet open-source
  `bundesAPI/jobsuche-api` (https://jobsuche.api.bund.dev/), avec les mêmes
  paramètres que l'URL de recherche du site arbeitsagentur.de.
- Le endpoint de détail d'offre (`/pc/v4/jobdetails/{hashId}`) est documenté
  par la communauté plutôt que dans la spec officielle — à surveiller lors du
  premier run réel si jamais il ne répondait pas comme attendu.
- La vérification anti-doublon lit un export CSV public du Google Sheet
  (pas d'authentification requise, le fichier est partagé en lecture libre).
  Un employeur dans les 30 dernières lignes du suivi n'empêche PAS la
  notification — il ajoute juste un avertissement `⚠️ eventuell bereits
  beworben` dans l'e-mail.
- Le scoring est volontairement transparent (somme de points par groupe de
  compétence trouvé) plutôt qu'une IA boîte noire — plus facile à ajuster et
  à expliquer pourquoi une offre est retenue ou non.
