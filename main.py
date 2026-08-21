"""FormSG webhook that publishes a validated VVIP CSV to GitHub."""

import base64
import csv
import io
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

import requests

from allowed_submitters import ALLOWED_SUBMITTER_EMAILS

try:
    import functions_framework
except ImportError:  # Enables local validation tests without Cloud Run dependencies.
    class _FunctionsFramework:
        @staticmethod
        def http(function):
            return function
    functions_framework = _FunctionsFramework()


LOGGER = logging.getLogger(__name__)
EMAIL_PATTERN = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)
GITHUB_API = "https://api.github.com"
POSTMAN_API = "https://api.postman.gov.sg"
MAX_GITHUB_ATTEMPTS = 3
MAX_POSTMAN_ATTEMPTS = 2


class UpdaterError(Exception):
    """An expected error that is safe to describe to the submitter."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Settings:
    formsg_env: str
    form_secret_key: str
    formsg_webhook_uri: str
    csv_field_id: str
    submitter_email_field_id: str
    github_token: str
    github_owner: str
    github_repo: str
    github_branch: str
    github_file_path: str
    postman_api_token: str
    postman_admin_recipient: str
    postman_base_url: str


def settings_from_environment() -> Settings:
    names = (
        "FORMSG_ENV", "FORM_SECRET_KEY", "FORMSG_WEBHOOK_URI", "CSV_FIELD_ID",
        "SUBMITTER_EMAIL_FIELD_ID", "GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO",
        "GITHUB_BRANCH", "GITHUB_FILE_PATH", "POSTMAN_API_TOKEN", "POSTMAN_ADMIN_RECIPIENT",
    )
    values = {name: os.environ.get(name, "").strip() for name in names}
    if missing := [name for name, value in values.items() if not value]:
        raise UpdaterError("Service configuration is incomplete.", 500)
    environment = values["FORMSG_ENV"].upper()
    if environment not in {"STAGING", "PRODUCTION"}:
        raise UpdaterError("FORMSG_ENV must be STAGING or PRODUCTION.", 500)
    if not EMAIL_PATTERN.fullmatch(values["POSTMAN_ADMIN_RECIPIENT"]):
        raise UpdaterError("POSTMAN_ADMIN_RECIPIENT must be an email address.", 500)
    return Settings(
        formsg_env=environment, form_secret_key=values["FORM_SECRET_KEY"],
        formsg_webhook_uri=values["FORMSG_WEBHOOK_URI"], csv_field_id=values["CSV_FIELD_ID"],
        submitter_email_field_id=values["SUBMITTER_EMAIL_FIELD_ID"],
        github_token=values["GITHUB_TOKEN"], github_owner=values["GITHUB_OWNER"],
        github_repo=values["GITHUB_REPO"], github_branch=values["GITHUB_BRANCH"],
        github_file_path=values["GITHUB_FILE_PATH"], postman_api_token=values["POSTMAN_API_TOKEN"],
        postman_admin_recipient=values["POSTMAN_ADMIN_RECIPIENT"],
        postman_base_url=os.environ.get("POSTMAN_BASE_URL", POSTMAN_API).rstrip("/"),
    )


def normalise_csv(file_content: bytes) -> tuple[bytes, int]:
    try:
        text = file_content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise UpdaterError("The uploaded CSV must use UTF-8 encoding.") from error
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames:
            raise UpdaterError("The uploaded CSV is empty or has no header row.")
        headers = {header.strip().casefold(): header for header in reader.fieldnames if header}
        if not (email_header := headers.get("email")):
            raise UpdaterError('The uploaded CSV must contain an "email" column.')
        emails = set()
        for row in reader:
            email = (row.get(email_header) or "").strip().casefold()
            if not email or not EMAIL_PATTERN.fullmatch(email):
                raise UpdaterError("The uploaded CSV contains an invalid email address.")
            emails.add(email)
    except csv.Error as error:
        raise UpdaterError("Unable to parse the uploaded CSV.") from error
    if not emails:
        raise UpdaterError("The uploaded CSV does not contain any email addresses.")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["email"])
    writer.writerows([[email] for email in sorted(emails)])
    return output.getvalue().encode(), len(emails)


def response_answer(responses: list[Mapping[str, Any]], field_id: str) -> str:
    for response in responses:
        if response.get("_id") == field_id and isinstance(response.get("answer"), str):
            if answer := response["answer"].strip():
                return answer
    raise UpdaterError("The required FormSG email field is missing.")


def log_signature_diagnostics(signature: str, config: Settings) -> None:
    """Log non-sensitive details useful for diagnosing FormSG verification failures."""
    try:
        from formsg.constants import PUBLIC_KEY_PRODUCTION, PUBLIC_KEY_STAGING
        from formsg.util.parser import parse_signature_header
        from formsg.util.webhook import is_signature_valid
        header = parse_signature_header(signature)
        LOGGER.warning("FormSG signature failed: configured_env=%s uri=%s form_id=%s submission_id=%s validates_production=%s validates_staging=%s", config.formsg_env, config.formsg_webhook_uri, header.get("f"), header.get("s"), is_signature_valid(config.formsg_webhook_uri, header, PUBLIC_KEY_PRODUCTION), is_signature_valid(config.formsg_webhook_uri, header, PUBLIC_KEY_STAGING))
    except Exception:
        LOGGER.exception("Unable to inspect failed FormSG signature")


def decrypt_submission(payload: Mapping[str, Any], request_headers: Mapping[str, str], config: Settings):
    try:
        import formsg
        from formsg.exceptions import WebhookAuthenticateException
    except ImportError as error:
        raise UpdaterError("FormSG SDK is unavailable.", 500) from error
    if not (signature := request_headers.get("X-FormSG-Signature")):
        raise UpdaterError("Missing FormSG signature.", 401)
    if not isinstance(payload.get("data"), Mapping):
        raise UpdaterError("Invalid FormSG webhook payload.")
    sdk = formsg.FormSdk(config.formsg_env)
    try:
        sdk.webhooks.authenticate(signature, config.formsg_webhook_uri)
    except WebhookAuthenticateException as error:
        log_signature_diagnostics(signature, config)
        raise UpdaterError("Invalid FormSG signature.", 401) from error
    if not (decrypted := sdk.crypto.decrypt_attachments(config.form_secret_key, payload["data"])):
        raise UpdaterError("Unable to decrypt or download the FormSG submission.")
    return decrypted


def github_update(content: bytes, submission_id: str, submitter_email: str, config: Settings) -> str:
    url = f"{GITHUB_API}/repos/{config.github_owner}/{config.github_repo}/contents/{config.github_file_path}"
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {config.github_token}", "X-GitHub-Api-Version": "2022-11-28"}
    for attempt in range(MAX_GITHUB_ATTEMPTS):
        try:
            current = requests.get(url, headers=headers, params={"ref": config.github_branch}, timeout=20)
            current.raise_for_status()
            update = requests.put(url, headers=headers, json={"message": f"Update vvips.csv from FormSG submission {submission_id} by {submitter_email}", "content": base64.b64encode(content).decode("ascii"), "sha": current.json()["sha"], "branch": config.github_branch}, timeout=20)
            if update.status_code == 409 and attempt + 1 < MAX_GITHUB_ATTEMPTS:
                continue
            update.raise_for_status()
            return update.json()["commit"]["html_url"]
        except (requests.RequestException, KeyError, ValueError) as error:
            LOGGER.exception("GitHub update failed (attempt %s)", attempt + 1)
            if attempt + 1 == MAX_GITHUB_ATTEMPTS:
                raise UpdaterError("Unable to update the database in GitHub.", 502) from error
    raise AssertionError("unreachable")


def notify(recipients: list[str], subject: str, body: str, config: Settings) -> None:
    recipients = list(dict.fromkeys(email for email in recipients if email))
    if not recipients:
        return
    payload = {"subject": subject, "body": body, "recipient": recipients[0], "classification": "FOR_INFO", "tag": "vvip-csv-updater"}
    if len(recipients) > 1:
        payload["cc"] = recipients[1:]
    for attempt in range(MAX_POSTMAN_ATTEMPTS):
        try:
            response = requests.post(f"{config.postman_base_url}/v1/transactional/email/send", headers={"Authorization": f"Bearer {config.postman_api_token}", "Content-Type": "application/json"}, json=payload, timeout=15)
            response.raise_for_status()
            return
        except requests.RequestException:
            if attempt + 1 == MAX_POSTMAN_ATTEMPTS:
                LOGGER.exception("Postman notification failed after retry")


def json_response(payload: Mapping[str, Any], status_code: int):
    return json.dumps(payload), status_code, {"Content-Type": "application/json"}


@functions_framework.http
def formsg_webhook(request):
    """Receive an authenticated FormSG submission and replace vvips.csv in GitHub."""
    if request.method != "POST":
        return json_response({"error": "Method not allowed."}, 405)
    config, submission_id, submitter_email = None, "unknown", None
    try:
        config = settings_from_environment()
        if not isinstance(payload := request.get_json(silent=True), Mapping):
            raise UpdaterError("Invalid JSON webhook payload.")
        submission_id = str(payload.get("submissionId") or "unknown")
        decrypted = decrypt_submission(payload, request.headers, config)
        content = decrypted.get("content", {})
        responses = content.get("responses", []) if isinstance(content, Mapping) else []
        if not isinstance(responses, list):
            raise UpdaterError("Unable to read the decrypted FormSG submission.")
        submitter_email = response_answer(responses, config.submitter_email_field_id).casefold()
        if not EMAIL_PATTERN.fullmatch(submitter_email):
            raise UpdaterError("The FormSG email field is invalid.")
        if submitter_email not in ALLOWED_SUBMITTER_EMAILS:
            raise UpdaterError("This email address is not authorised to update the VVIP database.", 403)
        attachment = decrypted.get("attachments", {}).get(config.csv_field_id, {})
        file_content = attachment.get("content") if isinstance(attachment, Mapping) else None
        if not isinstance(file_content, bytes):
            raise UpdaterError("The required CSV attachment is missing.")
        normalised, count = normalise_csv(file_content)
        commit_url = github_update(normalised, submission_id, submitter_email, config)
        notify([submitter_email, config.postman_admin_recipient], "VVIP database update completed", f"{submitter_email} updated the VVIP database with {count} email addresses. Commit: {commit_url}\n\nPlease allow 5–10 minutes for the database update to be reflected. Contact Ian Chua (chua_cheng_yong_ian@mindef.gov.sg) if you face any issues.", config)
        return json_response({"message": "Database updated.", "submissionId": submission_id}, 200)
    except UpdaterError as error:
        LOGGER.warning("VVIP CSV update failed for submission %s: %s", submission_id, error)
        if config:
            notify(([submitter_email] if submitter_email else []) + [config.postman_admin_recipient], "VVIP database update failed", f"The VVIP database could not be updated: {error}\n\nPlease contact Ian Chua (chua_cheng_yong_ian@mindef.gov.sg) to troubleshoot or for more information.", config)
        if error.status_code in {400, 403}:
            return json_response({"message": "Submission processed with errors.", "submissionId": submission_id}, 200)
        return json_response({"error": str(error), "submissionId": submission_id}, error.status_code)
    except Exception:
        LOGGER.exception("Unexpected VVIP CSV updater failure for submission %s", submission_id)
        return json_response({"error": "Unexpected service error.", "submissionId": submission_id}, 500)
