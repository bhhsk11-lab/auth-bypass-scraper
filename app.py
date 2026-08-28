steps:
  - name: 'gcr.io/cloud-builders/docker'
    args:
      - 'build'
      - '-t'
      - '${_REGION}-docker.pkg.dev/$PROJECT_ID/cloud-run-source-deploy/auth-bypass-scraper'
      - '--cache-from'
      - '${_REGION}-docker.pkg.dev/$PROJECT_ID/cloud-run-source-deploy/auth-bypass-scraper:latest'
      - '.'

  - name: 'gcr.io/cloud-builders/docker'
    args:
      - 'push'
      - '${_REGION}-docker.pkg.dev/$PROJECT_ID/cloud-run-source-deploy/auth-bypass-scraper'

  - name: 'gcr.io/google.com/cloudsdktool/cloud-sdk'
    entrypoint: gcloud
    args:
      - 'run'
      - 'deploy'
      - 'auth-bypass-scraper'
      - '--image=${_REGION}-docker.pkg.dev/$PROJECT_ID/cloud-run-source-deploy/auth-bypass-scraper'
      - '--region=${_REGION}'
      - '--platform=managed'
      - '--memory=4Gi'
      - '--cpu=2'
      - '--timeout=300'
      - '--concurrency=4'
      - '--min-instances=0'
      - '--max-instances=10'
      - '--allow-unauthenticated'
      - '--set-env-vars=LOG_LEVEL=INFO,PYTHONUNBUFFERED=1'

substitutions:
  _REGION: us-central1

options:
  machineType: 'E2_HIGHCPU_8'
  logging: CLOUD_LOGGING_ONLY

timeout: '1200s'
