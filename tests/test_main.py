import unittest
from unittest.mock import Mock, patch

import requests

from main import Settings, UpdaterError, formsg_webhook, github_update, normalise_csv, notify


SETTINGS = Settings("PRODUCTION", "secret", "https://example.test", "csv-field", "email-field", "token", "owner", "repo", "main", "vvips.csv", "postman-token", "admin@example.com", "https://postman.example.test")


class FakeRequest:
    method = "POST"
    headers = {"X-FormSG-Signature": "signature"}

    def __init__(self, payload):
        self.payload = payload

    def get_json(self, silent=False):
        return self.payload


class NormaliseCsvTests(unittest.TestCase):
    def test_normalises_deduplicates_and_sorts(self):
        content, count = normalise_csv(b"Name,EMAIL\nA,Bravo@Example.com\nB,alpha@example.com\nC,bravo@example.com\n")
        self.assertEqual((content, count), (b"email\nalpha@example.com\nbravo@example.com\n", 2))

    def test_rejects_invalid_or_missing_email_column(self):
        with self.assertRaisesRegex(UpdaterError, 'must contain an "email" column'):
            normalise_csv(b"address\nuser@example.com\n")
        with self.assertRaisesRegex(UpdaterError, "invalid email"):
            normalise_csv(b"email\nnot-an-email\n")


class WebhookTests(unittest.TestCase):
    @patch("main.notify")
    @patch("main.github_update", return_value="https://github.com/owner/repo/commit/abc")
    @patch("main.settings_from_environment", return_value=SETTINGS)
    @patch("main.decrypt_submission")
    def test_success_updates_github_and_notifies(self, decrypt, _settings, update, notify):
        decrypt.return_value = {"content": {"responses": [{"_id": "email-field", "answer": "ian_chua_cheng_yong@defence.gov.sg"}]}, "attachments": {"csv-field": {"content": b"email\nVVIP@example.com\n"}}}
        _body, status, _headers = formsg_webhook(FakeRequest({"submissionId": "submission-1", "data": {}}))
        self.assertEqual(status, 200)
        update.assert_called_once()
        self.assertEqual(notify.call_args.args[0], ["ian_chua_cheng_yong@defence.gov.sg", "admin@example.com"])

    @patch("main.notify")
    @patch("main.github_update")
    @patch("main.settings_from_environment", return_value=SETTINGS)
    @patch("main.decrypt_submission")
    def test_unlisted_submitter_is_not_published(self, decrypt, _settings, update, notify):
        decrypt.return_value = {"content": {"responses": [{"_id": "email-field", "answer": "unlisted@example.com"}]}, "attachments": {"csv-field": {"content": b"email\nvvip@example.com\n"}}}
        _body, status, _headers = formsg_webhook(FakeRequest({"submissionId": "submission-2", "data": {}}))
        self.assertEqual(status, 200)
        update.assert_not_called()
        self.assertEqual(notify.call_args.args[0], ["unlisted@example.com", "admin@example.com"])


class IntegrationHelperTests(unittest.TestCase):
    @patch("main.requests.put")
    @patch("main.requests.get")
    def test_github_conflict_is_retried(self, get, put):
        current = Mock(); current.json.return_value = {"sha": "current-sha"}; current.raise_for_status.return_value = None
        get.return_value = current
        conflict = Mock(status_code=409)
        success = Mock(status_code=200); success.json.return_value = {"commit": {"html_url": "https://github.com/owner/repo/commit/abc"}}
        put.side_effect = [conflict, success]
        self.assertEqual(github_update(b"email\nuser@example.com\n", "submission-3", "user@example.com", SETTINGS), "https://github.com/owner/repo/commit/abc")
        self.assertEqual(put.call_count, 2)

    @patch("main.requests.post")
    def test_postman_notification_is_retried(self, post):
        failed = Mock(); failed.raise_for_status.side_effect = requests.RequestException("temporary failure")
        success = Mock(); success.raise_for_status.return_value = None
        post.side_effect = [failed, success]
        notify(["user@example.com"], "subject", "body", SETTINGS)
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
