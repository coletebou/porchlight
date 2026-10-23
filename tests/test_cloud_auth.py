"""Cloud auth sources with fake CLI replies; no real accounts or network calls."""
import json
import os
import unittest
from unittest import mock

import support  # noqa: F401 — scratch cache/config before importing sources

from porchlight import collect, core, registry
from porchlight.sources import cloud_auth as cloud


GCP_ACCOUNT = '[{"account":"developer@example.com","status":"ACTIVE"}]'
GCP_CONFIG = '{"core":{"project":"example-project"}}'
GCP_PROJECT = '{"projectId":"example-project","projectNumber":"123456789012"}'
GCP_REAUTH = "Reauthentication failed. cannot prompt during non-interactive execution."
SUB_ID = "11111111-2222-3333-4444-555555555555"
TENANT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
AZ_ACCOUNT = {"account": "developer@example.com", "subscription": "Development", "id": SUB_ID, "tenant": TENANT_ID}
LOCATIONS = '["eastus","westus"]'


def ok(text):
    return 0, text, ""


def run_source(name, replies, settings="", env=None):
    h = support.host("[sources.%s]\n%s\n" % (name, settings))
    src = registry.SOURCES[name]
    with mock.patch.object(core, "which", side_effect=lambda name, extra=(): "/fake/" + name), \
            mock.patch.object(core, "run", side_effect=replies) as run:
        data = src.fn(support.ctx(h, src.config(h), env or {}))
    return data, run.call_args_list


class Registration(unittest.TestCase):
    def test_sources_are_opt_in_and_require_their_cli(self):
        for name, binary in (("gcp_auth", "gcloud"), ("azure_auth", "az")):
            with self.subTest(name=name), mock.patch.object(core, "which", return_value="/fake/" + binary):
                src = registry.SOURCES[name]
                self.assertEqual(registry.state(src, support.host()), "opt-in")
                self.assertEqual(registry.state(src, support.host("[sources.%s]\n" % name)), "on")
                self.assertEqual(registry.state(src, support.host("[sources.%s]\nenabled = false\n" % name)), "disabled")
            with mock.patch.object(core, "which", return_value=None):
                self.assertEqual(registry.state(src, support.host("[sources.%s]\n" % name)), "no " + binary)

    def test_missing_cli_is_quiet_even_if_removed_after_gating(self):
        for name in ("gcp_auth", "azure_auth"):
            with self.subTest(name=name), mock.patch.object(core, "which", return_value=None), \
                    mock.patch.object(core, "run") as run:
                data = registry.SOURCES[name].fn(support.ctx(support.host()))
                self.assertFalse(data["cli"])
                self.assertEqual(data["state"], "none")
                self.assertNotIn("alerts", data)
                run.assert_not_called()


class Classification(unittest.TestCase):
    def test_rejected_missing_and_iam_errors_are_distinct(self):
        cases = [
            ("gcp", "invalid_grant: Token has been expired or revoked.", "rejected"),
            ("gcp", "invalid_rapt: reauth related error", "rejected"),
            ("gcp", GCP_REAUTH, "rejected"),
            ("gcp", "You do not currently have an active account selected.", "none"),
            ("gcp", "PERMISSION_DENIED: caller does not have permission", "error"),
            ("gcp", "NOT_FOUND: project missing", "error"),
            ("azure", "AADSTS700082: The refresh token has expired. Please run 'az login'.", "rejected"),
            ("azure", "(InvalidAuthenticationToken) Access token validation failure.", "rejected"),
            ("azure", "Please run 'az login' to setup account.", "none"),
            ("azure", "AuthorizationFailed: cannot perform Microsoft.Resources/subscriptions/locations/read", "error"),
            ("azure", "AADSTS700016: Application was not found", "error"),
        ]
        for provider, error, expected in cases:
            with self.subTest(provider=provider, error=error):
                self.assertEqual(cloud.classify_cli(1, error, provider), expected)

    def test_network_is_not_an_auth_rejection(self):
        for provider in ("gcp", "azure"):
            for rc, error in ((124, ""), (1, "Failed to establish a new connection"),
                              (1, "Temporary failure in name resolution"), (1, "getaddrinfo failed")):
                with self.subTest(provider=provider, error=error):
                    self.assertEqual(cloud.classify_cli(rc, error, provider), "unreachable")


class ChildEnvironment(unittest.TestCase):
    def test_gcp_keeps_its_selectors_and_drops_unrelated_secrets(self):
        inherited = {"HOME": "/scratch", "PATH": "/bin", "CLOUDSDK_CONFIG": "/scratch/gcloud",
                     "AWS_SECRET_ACCESS_KEY": "AWS-CANARY", "OPENAI_API_KEY": "OPENAI-CANARY",
                     "AZURE_CLIENT_SECRET": "AZURE-CANARY", "AWS_BEARER_TOKEN_BEDROCK": "BEDROCK-CANARY"}
        extra = {"CLOUDSDK_ACTIVE_CONFIG_NAME": "work", "CLOUDSDK_CORE_PROJECT": "example-project",
                 "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": "/scratch/gcp.json",
                 "CLOUDSDK_AUTH_ACCESS_TOKEN": "GCP-TOKEN-CANARY", "UNRELATED_TOKEN": "OTHER-CANARY"}
        with mock.patch.dict(os.environ, inherited, clear=True):
            data, calls = run_source("gcp_auth", [ok(GCP_ACCOUNT), ok(GCP_CONFIG), ok(GCP_PROJECT)], env=extra)
        self.assertEqual(data["state"], "ok")
        self.assertEqual(data["account"], "credential override")
        for call in calls:
            env = call.kwargs["env"]
            self.assertEqual(env["CLOUDSDK_CONFIG"], "/scratch/gcloud")
            for name in extra:
                if name.startswith("CLOUDSDK_"):
                    self.assertEqual(env[name], extra[name])
            self.assertEqual(env["CLOUDSDK_CORE_DISABLE_PROMPTS"], "1")
            for key in ("AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "AZURE_CLIENT_SECRET",
                        "AWS_BEARER_TOKEN_BEDROCK", "UNRELATED_TOKEN"):
                self.assertNotIn(key, env)
        self.assertNotIn("CANARY", json.dumps(data))

    def test_azure_keeps_config_dir_without_sdk_or_other_provider_keys(self):
        inherited = {"PATH": "/bin", "HOME": "/scratch", "AZURE_CONFIG_DIR": "/scratch/azure-old",
                     "AZURE_CLIENT_SECRET": "AZURE-CANARY", "AWS_ACCESS_KEY_ID": "AWS-CANARY",
                     "CLOUDSDK_AUTH_ACCESS_TOKEN": "GCP-CANARY", "OPENAI_API_KEY": "OPENAI-CANARY"}
        extra = {"AZURE_CONFIG_DIR": "/scratch/azure-work", "AZURE_CLIENT_SECRET": "OTHER-AZURE-CANARY"}
        with mock.patch.dict(os.environ, inherited, clear=True):
            data, calls = run_source("azure_auth", [ok(json.dumps(AZ_ACCOUNT)), ok(LOCATIONS)], env=extra)
        for call in calls:
            env = call.kwargs["env"]
            self.assertEqual(env["AZURE_CONFIG_DIR"], "/scratch/azure-work")
            self.assertEqual(env["AZURE_EXTENSION_USE_DYNAMIC_INSTALL"], "no")
            self.assertEqual(env["AZURE_CORE_COLLECT_TELEMETRY"], "false")
            for key in ("AZURE_CLIENT_SECRET", "AWS_ACCESS_KEY_ID", "CLOUDSDK_AUTH_ACCESS_TOKEN", "OPENAI_API_KEY"):
                self.assertNotIn(key, env)
        self.assertNotIn("CANARY", json.dumps(data))


class GcpAuth(unittest.TestCase):
    def test_ok_requires_a_project_api_read_with_selected_configuration(self):
        data, calls = run_source("gcp_auth", [ok(GCP_ACCOUNT), ok(GCP_CONFIG), ok(GCP_PROJECT)],
                                 'project = "example-project"\nconfiguration = "work"')
        self.assertEqual(data["state"], "ok")
        self.assertEqual(data["account"], "developer@example.com")
        self.assertEqual(data["project"], "example-project")
        self.assertEqual(data["configuration"], "work")
        self.assertEqual([call.args[0][1:3] for call in calls],
                         [["auth", "list"], ["config", "list"], ["projects", "describe"]])
        for call in calls:
            self.assertIn("--configuration=work", call.args[0])
            self.assertIn("--quiet", call.args[0])
        self.assertNotIn("alerts", data)

    def test_default_project_is_read_without_mutating_configuration(self):
        data, calls = run_source("gcp_auth", [ok(GCP_ACCOUNT), ok(GCP_CONFIG), ok(GCP_PROJECT)])
        self.assertEqual(data["state"], "ok")
        self.assertEqual(calls[1].args[0][1:4], ["config", "list",
                         "--format=json(core.project,auth.access_token_file,auth.credential_file_override,auth.impersonate_service_account)"])

    def test_no_project_is_configured_but_unverified(self):
        for settings in ("{}", "null", '{"core":{"project":"(unset)"}}'):
            with self.subTest(settings=settings):
                data, calls = run_source("gcp_auth", [ok(GCP_ACCOUNT), ok(settings)])
                self.assertEqual(data["state"], "configured")
                self.assertIn("not checked", data["error"])
                self.assertEqual(len(calls), 2)
                self.assertNotIn("alerts", data)

    def test_no_local_credentials_does_not_block_attached_service_account(self):
        data, calls = run_source("gcp_auth", [ok("[]"), ok("null"), ok(GCP_PROJECT)], 'project = "123456789012"')
        self.assertEqual(data["state"], "ok")
        self.assertEqual(data["account"], "")
        self.assertIn("123456789012", calls[2].args[0])

    def test_absent_account_and_project_is_quiet(self):
        data, _ = run_source("gcp_auth", [ok("[]"), ok("null")])
        self.assertEqual(data["state"], "none")
        self.assertNotIn("alerts", data)

    def test_invalid_project_is_not_sent_to_cli(self):
        data, calls = run_source("gcp_auth", [ok(GCP_ACCOUNT), ok(GCP_CONFIG)], 'project = "--flags-file=bad"')
        self.assertEqual(data["state"], "error")
        self.assertEqual(len(calls), 2)

    def test_account_failures_do_not_masquerade_as_no_login(self):
        for rc, error, expected in ((124, "timeout", "unreachable"), (1, "unexpected failure", "error")):
            with self.subTest(expected=expected):
                data, calls = run_source("gcp_auth", [(rc, "", error)])
                self.assertEqual(data["state"], expected)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("alerts", data)

    def test_account_failure_does_not_cache_undiscovered_override_paths(self):
        data, calls = run_source("gcp_auth", [(1, "", "reauthentication is needed for /CANARY/private/credentials.json")])
        self.assertEqual(data["state"], "rejected")
        self.assertNotIn("CANARY", json.dumps(data))
        self.assertEqual(len(calls), 1)

    def test_only_credential_rejection_alerts_and_login_is_never_run(self):
        for error, expected in (("invalid_grant: revoked", "rejected"), (GCP_REAUTH, "rejected"), ("PERMISSION_DENIED", "error"),
                                ("could not connect", "unreachable")):
            with self.subTest(expected=expected):
                data, calls = run_source("gcp_auth", [ok(GCP_ACCOUNT), ok(GCP_CONFIG), (1, "", error)],
                                         'project = "example-project"\nlogin_command = "corp-login gcp"')
                self.assertEqual(data["state"], expected)
                self.assertEqual(bool(data.get("alerts")), expected == "rejected")
                if expected == "rejected":
                    self.assertEqual(data["alerts"][0]["fix"], "corp-login gcp")
                self.assertEqual(len(calls), 3)

    def test_malformed_metadata_and_api_successes_are_errors(self):
        for bad in ("bad json", "null", "{}", '[{"account":7}]', '[{"account":""}]'):
            with self.subTest(account=bad):
                data, _ = run_source("gcp_auth", [ok(bad)])
                self.assertEqual(data["state"], "error")
        for bad in ("", "[]", "{}", '{"projectId":false}'):
            with self.subTest(project=bad):
                data, _ = run_source("gcp_auth", [ok(GCP_ACCOUNT), ok(GCP_CONFIG), ok(bad)], 'project = "example-project"')
                self.assertEqual(data["state"], "error")

    def test_environment_override_never_verifies_the_cached_principal(self):
        names = ("CLOUDSDK_AUTH_ACCESS_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
                 "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT")
        for name in names:
            value = "CANARY-override-identity-or-path"
            with self.subTest(name=name), mock.patch.dict(os.environ, {}, clear=True):
                data, calls = run_source("gcp_auth", [ok(GCP_ACCOUNT), ok(GCP_CONFIG), ok(GCP_PROJECT)],
                                         'project = "example-project"', {name: value})
                self.assertEqual(data["state"], "ok")
                self.assertEqual(data["account"], "credential override")
                self.assertNotIn("developer@example.com", json.dumps(data))
                self.assertNotIn(value, json.dumps(data))
                self.assertEqual(len(calls), 3)

    def test_config_override_never_verifies_or_caches_the_other_identity(self):
        for key in ("access_token_file", "credential_file_override", "impersonate_service_account"):
            settings = {"core": {"project": "example-project"}, "auth": {key: "CANARY-override-identity-or-path"}}
            with self.subTest(key=key), mock.patch.dict(os.environ, {}, clear=True):
                data, _ = run_source("gcp_auth", [ok(GCP_ACCOUNT), ok(json.dumps(settings)), ok(GCP_PROJECT)],
                                     'project = "example-project"')
                self.assertEqual(data["state"], "ok")
                self.assertEqual(data["account"], "credential override")
                self.assertNotIn("developer@example.com", json.dumps(data))
                self.assertNotIn("CANARY", json.dumps(data))

    def test_missing_project_with_only_override_is_configured(self):
        settings = '{"auth":{"access_token_file":"/x"}}'
        data, calls = run_source("gcp_auth", [ok("[]"), ok(settings)])
        self.assertEqual(data["state"], "configured")
        self.assertEqual(data["account"], "credential override")
        self.assertEqual(len(calls), 2)

    def test_config_failures_cannot_verify_a_cached_identity(self):
        for rc, expected in ((1, "error"), (124, "unreachable")):
            with self.subTest(rc=rc):
                data, calls = run_source("gcp_auth", [ok(GCP_ACCOUNT), (rc, "", "config failure in CANARY-override-path")],
                                         'project = "example-project"')
                self.assertEqual(data["state"], expected)
                self.assertEqual(data["account"], "")
                self.assertNotIn("CANARY", json.dumps(data))
                self.assertEqual(len(calls), 2)
        for bad in ("bad json", "[]", '{"core":[]}', '{"core":{"project":42}}', '{"auth":[]}',
                    '{"auth":{"access_token_file":[]}}', '{"auth":{"credential_file_override":false}}',
                    '{"auth":{"impersonate_service_account":{}}}'):
            with self.subTest(config=bad):
                data, calls = run_source("gcp_auth", [ok(GCP_ACCOUNT), ok(bad)], 'project = "example-project"')
                self.assertEqual(data["state"], "error")
                self.assertEqual(data["account"], "")
                self.assertEqual(len(calls), 2)


class AzureAuth(unittest.TestCase):
    def test_ok_requires_live_locations_and_selection_is_per_command(self):
        data, calls = run_source("azure_auth", [ok(json.dumps(AZ_ACCOUNT)), ok(LOCATIONS)],
                                 'subscription = "Development"')
        self.assertEqual(data["state"], "ok")
        self.assertEqual(data["account"], "developer@example.com")
        self.assertEqual(data["subscription"], "Development")
        self.assertEqual(data["subscription_id"], SUB_ID)
        self.assertEqual(data["tenant"], TENANT_ID)
        self.assertEqual([call.args[0][1:3] for call in calls], [["account", "show"], ["account", "list-locations"]])
        self.assertIn("--subscription=Development", calls[0].args[0])
        self.assertIn("--subscription=" + SUB_ID, calls[1].args[0])
        self.assertNotIn("alerts", data)

    def test_missing_or_tenant_level_subscription_is_unverified(self):
        for changes in ({"id": None}, {"subscription": "N/A(tenant level account)", "id": TENANT_ID}):
            with self.subTest(changes=changes):
                account = dict(AZ_ACCOUNT, **changes)
                data, calls = run_source("azure_auth", [ok(json.dumps(account))])
                self.assertEqual(data["state"], "configured")
                self.assertEqual(len(calls), 1)
                self.assertNotIn("alerts", data)

    def test_account_error_classification(self):
        for rc, error, expected in ((1, "Please run 'az login' to setup account.", "none"),
                                    (124, "timeout", "unreachable"), (1, "invalid settings", "error")):
            with self.subTest(expected=expected):
                data, _ = run_source("azure_auth", [(rc, "", error)])
                self.assertEqual(data["state"], expected)
                self.assertNotIn("alerts", data)

    def test_cached_account_does_not_hide_rejected_api_credentials(self):
        data, calls = run_source("azure_auth", [ok(json.dumps(AZ_ACCOUNT)),
                                                (1, "", "AADSTS700082: expired. Please run 'az login'.")])
        self.assertEqual(data["state"], "rejected")
        self.assertEqual(data["alerts"][0]["fix"], "az login")
        self.assertEqual(len(calls), 2)

    def test_subscription_permissions_do_not_produce_auth_alerts(self):
        data, _ = run_source("azure_auth", [ok(json.dumps(AZ_ACCOUNT)), (1, "", "AuthorizationFailed: access denied")])
        self.assertEqual(data["state"], "error")
        self.assertNotIn("alerts", data)

    def test_malformed_metadata_and_api_successes_are_errors(self):
        for bad in ("bad json", "null", "[]", "{}", '{"account":[],"id":"bad"}',
                    json.dumps(dict(AZ_ACCOUNT, id="--subscription=wrong"))):
            with self.subTest(account=bad):
                data, _ = run_source("azure_auth", [ok(bad)])
                self.assertEqual(data["state"], "error")
        for bad in ("", "{}", "null", "[{}]", "[null]"):
            with self.subTest(locations=bad):
                data, _ = run_source("azure_auth", [ok(json.dumps(AZ_ACCOUNT)), ok(bad)])
                self.assertEqual(data["state"], "error")


class CollectorFlow(unittest.TestCase):
    def setUp(self):
        support.clear_cache()

    def test_transitions_replace_old_ok_data_and_alerts_clear_on_recovery(self):
        variants = [
            ("gcp_auth", 'project = "example-project"', [ok(GCP_ACCOUNT), ok(GCP_CONFIG)], ok(GCP_PROJECT),
             GCP_REAUTH, "You do not currently have an active account selected."),
            ("azure_auth", "", [ok(json.dumps(AZ_ACCOUNT))], ok(LOCATIONS),
             "AADSTS700082: expired. Please run 'az login'.", "Please run 'az login' to setup account."),
        ]
        for name, settings, local_replies, api_reply, rejected, missing in variants:
            h = support.host("[sources.%s]\n%s\n" % (name, settings))
            replies = local_replies + [api_reply] + local_replies + [(1, "", rejected), (1, "", missing)] \
                + local_replies + [api_reply]
            with self.subTest(name=name), mock.patch.object(core, "which", return_value="/fake/cli"), \
                    mock.patch.object(core, "run", side_effect=replies):
                for expected in ("ok", "rejected", "none", "ok"):
                    rec = collect.run_one(registry.SOURCES[name], h, {})
                    self.assertTrue(rec["ok"], rec)
                    self.assertEqual(rec["data"]["state"], expected)
                    self.assertEqual(registry.Cache().data(name)["state"], expected)
                    self.assertEqual(bool(rec["data"].get("alerts")), expected == "rejected")
                    self.assertFalse(rec.get("kept"))

    def test_credentials_cannot_escape_in_errors_metadata_or_login_hints(self):
        token = "CANARY-private-token-1234567890"
        env = {"PRIVATE_TOKEN": token}
        variants = [
            ("gcp_auth", 'project = "example-project"',
             [ok(json.dumps([{"account": token, "status": "ACTIVE"}])), ok(GCP_CONFIG)]),
            ("azure_auth", "", [ok(json.dumps(dict(AZ_ACCOUNT, account=token, subscription=token)))]),
        ]
        for name, settings, local_replies in variants:
            settings += '\nlogin_command = "login --secret ' + token + '"'
            h = support.host("[sources.%s]\n%s\n" % (name, settings))
            with self.subTest(name=name), mock.patch.object(core, "which", return_value="/fake/cli"), \
                    mock.patch.object(core, "run", side_effect=local_replies + [(1, "", "invalid_grant: " + token)]):
                rec = collect.run_one(registry.SOURCES[name], h, env)
                self.assertEqual(rec["data"]["state"], "rejected")
                self.assertNotIn(token, json.dumps(rec))
                with open(registry.src_path(name), encoding="utf-8") as f:
                    self.assertNotIn(token, f.read())
                self.assertIn("redacted", rec["data"]["error"])

    def test_config_override_paths_are_not_persisted_even_in_api_errors(self):
        h = support.host('[sources.gcp_auth]\nproject = "example-project"\n')
        for key in ("access_token_file", "credential_file_override", "impersonate_service_account"):
            settings = {"auth": {key: "/x"}}
            replies = [ok(GCP_ACCOUNT), ok(json.dumps(settings)), (1, "", "invalid_grant: cannot use /x")]
            with self.subTest(key=key), mock.patch.object(core, "which", return_value="/fake/cli"), \
                    mock.patch.object(core, "run", side_effect=replies):
                rec = collect.run_one(registry.SOURCES["gcp_auth"], h, {})
                self.assertEqual(rec["data"]["state"], "rejected")
                self.assertEqual(rec["data"]["account"], "credential override")
                self.assertNotIn("/x", json.dumps(rec))
                self.assertNotIn("developer@example.com", json.dumps(rec))
                with open(registry.src_path("gcp_auth"), encoding="utf-8") as f:
                    self.assertNotIn("/x", f.read())


if __name__ == "__main__":
    unittest.main()
