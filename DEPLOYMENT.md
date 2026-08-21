# Deploy the FormSG CSV updater

This repository contains a Cloud Run function that receives authenticated FormSG
webhooks and updates `vvips.csv` on GitHub. Cloud Run builds `main.py` with
`requirements.txt`; no Dockerfile is required.

## Runtime configuration

The production deployment uses the following values:

- FormSG webhook URI: `https://vvipchecker-978932763347.europe-west1.run.app/`
- FormSG email field ID: `6a87bf5a951f2495d647c377`
- FormSG CSV attachment field ID: `6a86c71dec6219dca6650efa`
- GitHub target: `icychua/vvipchecker`, branch `main`, file `vvips.csv`

The webhook URI must match the URL configured in FormSG exactly; the FormSG SDK
uses it to validate request signatures.

## Required setup

1. Configure the FormSG production form to post to the webhook URI above.
2. Create a fine-grained GitHub token with **Contents: Read and write** access to
   `icychua/vvipchecker`.
3. Create a Postman legacy v1 API token and select the administrator recipient.
4. Store the FormSG secret key, GitHub token, and Postman token in Secret Manager.
   Grant the Cloud Run service account Secret Manager Secret Accessor on each.
5. Create a Cloud Build trigger for this repository using `cloudbuild.yaml`.
   Set `_POSTMAN_ADMIN_RECIPIENT` to the real administrator email, and change the
   three secret-name substitutions if your Secret Manager names differ.

The service must allow unauthenticated invocations so FormSG can deliver webhooks.
Requests still require a valid FormSG signature before their contents are used.
Do not commit secret values; `.env.example` lists the configuration shape only.
