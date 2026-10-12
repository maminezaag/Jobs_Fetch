name: stat_jobs

# Déclenchement :
#   - chaque nuit par crontab.org (repository_dispatch). Le corps JSON de la requête POST doit être EXACTEMENT :
#       { "event_type": "run-stat-jobs" }
#     (job_watcher utilise "run-job-watcher" : les deux workflows ne se déclenchent jamais l'un l'autre)
#   - à la main : Actions -> stat_jobs -> Run workflow
on:
  repository_dispatch:
    types: [run-stat-jobs]
  workflow_dispatch: {}

# Nécessaire pour commiter l'archive (stat_jobs/archive/) dans le dépôt
permissions:
  contents: write

# Jamais deux exécutions de stat_jobs en même temps
concurrency:
  group: stat-jobs
  cancel-in-progress: false

jobs:
  stat-jobs:
    runs-on: ubuntu-latest
    timeout-minutes: 60
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Installer les dépendances de stat_jobs
        run: pip install -r stat_jobs/requirements.txt

      - name: Lancer stat_jobs
        env:
          GCP_SA_KEY: ${{ secrets.GCP_SA_KEY }}
          STAT_JOBS_SHEET_ID: ${{ secrets.STAT_JOBS_SHEET_ID }}
        run: python stat_jobs/stat_jobs.py

      # Exécutée même si le Sheet a échoué : les nouvelles offres archivées ne doivent jamais être perdues.
      # Ne touche QUE stat_jobs/archive/ (jamais seen_jobs.json ni les fichiers de job_watcher).
      - name: Sauvegarder l'archive dans le dépôt
        if: always()
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add stat_jobs/archive || exit 0
          if git diff --staged --quiet; then
            echo "Archive inchangée"
            exit 0
          fi
          git commit -m "stat_jobs : archive du $(date -u +%F)"
          # si job_watcher a commité entre-temps : on se remet à jour et on réessaie
          for i in 1 2 3 4 5; do
            git pull --rebase origin "${{ github.ref_name }}" && git push origin "HEAD:${{ github.ref_name }}" && exit 0
            echo "Push refusé (essai $i/5), nouvelle tentative dans 10 s"
            sleep 10
          done
          exit 1

      # Copie CSV des onglets : secours si l'écriture dans le Sheet échoue
      - name: Sauvegarder les résultats
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: stat-jobs-out
          path: stat_jobs/out/
          if-no-files-found: ignore
