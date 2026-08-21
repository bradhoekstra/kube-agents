import os
import unittest
from unittest.mock import MagicMock, patch

from github_token_refresh import refresh_git_credentials


class GitHubTokenRefreshTest(unittest.TestCase):
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_sandbox_delegates_without_receiving_token(self, urlopen, run):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        urlopen.return_value = response

        with patch.dict(
            os.environ,
            {"CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"},
            clear=False,
        ):
            token = refresh_git_credentials("owner/repository")

        self.assertEqual("", token)
        run.assert_not_called()
        request = urlopen.call_args.args[0]
        self.assertEqual(
            "http://127.0.0.1:8765/v1/github/refresh", request.full_url
        )

    @patch("github_token_refresh.wif_credentials.fetch_identity_token")
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_federated_identity_replaces_gcloud(self, urlopen, run, fetch):
        # The co-located sandbox proxy. gcloud refuses to mint an ID token from
        # an external_account credential, so calling it here is not a fallback
        # that costs a retry -- it is the failure the federated branch exists to
        # avoid, and it must not be reached at all.
        fetch.return_value = "an.id.token"
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"ghs_installation_token"
        urlopen.return_value = response

        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            token = refresh_git_credentials("owner/repository")

        self.assertEqual("ghs_installation_token", token)
        self.assertNotIn(
            "print-identity-token",
            " ".join(str(call.args[0]) for call in run.call_args_list),
        )
        self.assertEqual("an.id.token", urlopen.call_args.args[0].headers["X-oidc-token"])

    @patch("github_token_refresh.wif_credentials.fetch_identity_token")
    @patch("github_token_refresh.subprocess.run")
    @patch("github_token_refresh.urllib.request.urlopen")
    def test_metadata_server_placement_still_asks_gcloud(self, urlopen, run, fetch):
        # Every placement other than the co-located one. fetch_identity_token
        # returns None off a metadata-server identity, and this path has to stay
        # exactly as it was.
        fetch.return_value = None
        run.return_value = MagicMock(stdout="gcloud.id.token\n")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"ghs_installation_token"
        urlopen.return_value = response

        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}, clear=False):
            refresh_git_credentials("owner/repository")

        self.assertIn(
            "print-identity-token",
            " ".join(str(call.args[0]) for call in run.call_args_list),
        )
        self.assertEqual(
            "gcloud.id.token", urlopen.call_args.args[0].headers["X-oidc-token"]
        )


if __name__ == "__main__":
    unittest.main()
