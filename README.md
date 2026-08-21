# VVIP Correspondence Screener

A client-side web application designed to identify VVIP email addresses in free text to ensure Premium Service Standards are met. Built adhering to the organization's Design Language System (DLS).

## Files
- index.html: Main application logic and interface.
- styles.css: DLS-compliant styling.
- vvips.csv: The database of VVIP email addresses (one per line).

## Deployment
Host these three files on a static platform like GitHub Pages.

## Self-service database updates

`main.py` is a Python Cloud Run function that receives authenticated FormSG CSV
submissions and replaces `vvips.csv` in GitHub. See [DEPLOYMENT.md](DEPLOYMENT.md)
for the FormSG, GitHub, Postman, and Google Cloud setup.
