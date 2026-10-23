"""Opt-in GCP and Azure CLI identity checks for the ACCESS panel.

Account listings describe local configuration, not whether credentials still
work. Only a successful read from the provider makes the state `ok`; a local
identity without a project/subscription is `configured` (unverified). Permission
errors and network failures do not mean the credentials have been rejected.

All commands read metadata. We never invoke login, refresh, or token-output
commands; as with the AWS source, the CLI may refresh its own cache while
authenticating a provider request.
"""
from __future__ import annotations

import json
import re

from .. import core, secrets
from ..registry import source


_NETWORK = ("could not connect", "unable to connect", "connection refused", "connection reset",
            "connection aborted", "failed to establish a new connection", "timed out", "connect timeout",
            "read timeout", "name or service not known", "nodename nor servname", "name resolution",
            "network is unreachable", "getaddrinfo failed")
_REJECTED = {
    "gcp": ("invalid_grant", "invalid_rapt", "invalid authentication credentials",
            "token has been expired or revoked", "credentials have expired", "reauthentication is needed",
            "reauthentication failed. cannot prompt during non-interactive execution"),
    "azure": ("invalid_grant", "invalid_client", "interaction_required", "expiredauthenticationtoken",
              "invalidauthenticationtoken", "authenticationfailed", "token has expired", "token is expired",
              "aadsts700082", "aadsts700084", "aadsts70043", "aadsts50173", "aadsts50055",
              "aadsts50057", "aadsts50076", "aadsts50079"),
}
_NONE = {
    "gcp": ("you do not currently have an active account", "no active account selected", "no credentialed accounts"),
    "azure": ("please run 'az login'", 'please run "az login"', "az login to setup account",
              "no subscriptions found"),
}
_PROJECT = re.compile(r"(?:[a-z][a-z0-9-]{4,28}[a-z0-9]|[1-9][0-9]*)\Z")
_SUBSCRIPTION = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_GCP_AUTH_PROPERTIES = ("access_token_file", "credential_file_override", "impersonate_service_account")
_GCP_AUTH_ENV = ("CLOUDSDK_AUTH_ACCESS_TOKEN",) + tuple("CLOUDSDK_AUTH_" + key.upper() for key in _GCP_AUTH_PROPERTIES)
_GCP_CONFIG_FORMAT = "--format=json(core.project,%s)" % ",".join("auth." + key for key in _GCP_AUTH_PROPERTIES)


def classify_cli(rc: int, text: str, provider: str) -> str:
    """Keep authentication rejection distinct from missing login, IAM and network failures."""
    low = " ".join((text or "").split()).lower()
    if rc == 0:
        return "ok"
    if rc == 124 or any(s in low for s in _NETWORK):
        return "unreachable"
    # Expired credentials often come with a suggestion to run `az login` too.
    if any(s in low for s in _REJECTED[provider]):
        return "rejected"
    if any(s in low for s in _NONE[provider]):
        return "none"
    return "error"


def _text(value, env, limit=160) -> str:
    return secrets.redact(value.strip(), env, limit) if isinstance(value, str) else ""


def _result(data, state, **fields):
    # The collector retains old data for {error, status: unknown}. Auth changes
    # are fresh observations, so they must replace a previously valid identity.
    return dict(data, state=state, status=state, **fields)


def _failure(data, provider, rc, out, err, env, private=(), summary=None):
    text = err or out or "command failed (rc %d)" % rc
    state = classify_cli(rc, text, provider)
    text = summary or text
    # Configured override paths/identities are not necessarily token-shaped or
    # long enough for generic secret masking; do not retain them in diagnostics.
    for value in sorted(set(private), key=len, reverse=True):
        if value:
            text = text.replace(value, "<redacted>")
    data = _result(data, state, error=_text(text, env))
    if rc == 127:
        data.update(cli=False, state="none", status="none")
    if data["state"] == "rejected":
        label = "GCP" if provider == "gcp" else "Azure"
        data["alerts"] = [{"sev": "warn", "msg": "%s credentials rejected" % label, "fix": data["login"]}]
    return data


def gcp_env(extra) -> dict:
    """Use gcloud's configuration/credential selectors, without unrelated service secrets."""
    env = secrets.child_env(extra, keep=("CLOUDSDK_",))
    env.update(CLOUDSDK_CORE_DISABLE_PROMPTS="1", CLOUDSDK_CORE_DISABLE_USAGE_REPORTING="true",
               CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK="true", CLOUDSDK_CORE_LOG_HTTP="false",
               CLOUDSDK_CORE_VERBOSITY="error")
    return env


def azure_env(extra) -> dict:
    """az uses its existing login cache; Azure SDK/client secrets are not needed."""
    env = secrets.child_env(extra, keep=("AZURE_CONFIG_DIR",))
    env.update(AZURE_CORE_COLLECT_TELEMETRY="false", AZURE_CORE_DISABLE_CONFIRM_PROMPT="true",
               AZURE_CORE_ONLY_SHOW_ERRORS="true", AZURE_EXTENSION_USE_DYNAMIC_INSTALL="no")
    return env


@source("gcp_auth", ttl=600, timeout=35, binaries=("gcloud",), optin=True, defaults={
    "project": "", "configuration": "", "login_command": "gcloud auth login"})
def gcp_auth(ctx):
    """The active gcloud account/project and a read-only project access check (opt-in)."""
    cfg = ctx.cfg
    configuration = str(cfg.get("configuration") or "")
    project = str(cfg.get("project") or "").strip()
    env = gcp_env(ctx.env)
    overrides = [env[key] for key in _GCP_AUTH_ENV if env.get(key)]
    data = {"cli": False, "state": "none", "status": "none", "account": "credential override" if overrides else "",
            "project": _text(project, ctx.env),
            "configuration": _text(configuration or env.get("CLOUDSDK_ACTIVE_CONFIG_NAME"), ctx.env),
            "login": _text(cfg.get("login_command") or "gcloud auth login", ctx.env)}
    exe = core.which("gcloud", tuple(cfg.get("paths") or ()))
    if not exe:
        return data
    data["cli"] = True
    flags = ["--quiet"] + (["--configuration=" + configuration] if configuration else [])
    rc, out, err = core.run([exe, "auth", "list", "--filter=status:ACTIVE", "--format=json(account,status)"] + flags,
                            8, env=env)
    if rc != 0:
        # Config-file overrides are still unknown, so retain only classification.
        return _failure(data, "gcp", rc, out, err, ctx.env,
                        summary="gcloud account command failed (rc %d)" % rc)
    try:
        accounts = json.loads(out)
    except ValueError:
        accounts = None
    if not isinstance(accounts, list) or len(accounts) > 1 or any(
            not isinstance(a, dict) or not isinstance(a.get("account"), str) or not a["account"].strip()
            for a in accounts):
        return _result(data, "error", error="unparseable gcloud account reply")
    # auth list deliberately excludes credential overrides. Read their presence
    # from the same selected configuration even when a project was supplied.
    rc, out, err = core.run([exe, "config", "list", _GCP_CONFIG_FORMAT] + flags, 6, env=env)
    if rc != 0:
        # Failed config output could include override paths we have not learned.
        return _failure(data, "gcp", rc, out, err, ctx.env,
                        summary="gcloud configuration command failed (rc %d)" % rc)
    try:
        settings = json.loads(out)
    except ValueError:
        return _result(data, "error", error="unparseable gcloud configuration reply")
    # A projection with no configured properties is JSON null, not an error.
    settings = {} if settings is None else settings
    if not isinstance(settings, dict):
        return _result(data, "error", error="unparseable gcloud configuration reply")
    core_cfg, auth_cfg = settings.get("core", {}), settings.get("auth", {})
    core_cfg = {} if core_cfg is None else core_cfg
    auth_cfg = {} if auth_cfg is None else auth_cfg
    if not isinstance(core_cfg, dict) or not isinstance(auth_cfg, dict) or any(
            value is not None and not isinstance(value, str)
            for value in [core_cfg.get("project")] + [auth_cfg.get(key) for key in _GCP_AUTH_PROPERTIES]):
        return _result(data, "error", error="unparseable gcloud configuration reply")
    overrides += [auth_cfg[key] for key in _GCP_AUTH_PROPERTIES if auth_cfg.get(key)]
    if overrides:
        data["account"] = "credential override"
    elif accounts:
        data["account"] = _text(accounts[0]["account"], ctx.env)
    if not project:
        project = (core_cfg.get("project") or "").strip()
        if project == "(unset)":
            project = ""
        data["project"] = _text(project, ctx.env)
    if not project:
        return _result(data, "configured" if data["account"] else "none", error="no project set; access not checked")
    if not _PROJECT.fullmatch(project):
        return _result(data, "error", error="invalid project ID or number")
    # Even without a stored account, gcloud can use an attached service account
    # or credential-file override. Let the provider call determine the state.
    rc, out, err = core.run([exe, "projects", "describe", project, "--format=json(projectId,projectNumber)"] + flags,
                            18, env=env)
    if rc != 0:
        return _failure(data, "gcp", rc, out, err, ctx.env, overrides)
    try:
        reply = json.loads(out)
    except ValueError:
        reply = None
    if not isinstance(reply, dict) or not isinstance(reply.get("projectId"), str) or not reply["projectId"].strip():
        return _result(data, "error", error="unparseable gcloud project reply")
    return _result(data, "ok", project=_text(reply["projectId"], ctx.env))


@source("azure_auth", ttl=600, timeout=30, binaries=("az",), optin=True, defaults={
    "subscription": "", "login_command": "az login"})
def azure_auth(ctx):
    """The selected Azure account/subscription and a read-only location check (opt-in)."""
    cfg = ctx.cfg
    subscription = str(cfg.get("subscription") or "")
    env = azure_env(ctx.env)
    data = {"cli": False, "state": "none", "status": "none", "account": "", "subscription": "",
            "subscription_id": "", "tenant": "",
            "login": _text(cfg.get("login_command") or "az login", ctx.env)}
    exe = core.which("az", tuple(cfg.get("paths") or ()))
    if not exe:
        return data
    data["cli"] = True
    flags = ["--output", "json", "--only-show-errors"]
    selection = ["--subscription=" + subscription] if subscription else []
    query = "{account:user.name,subscription:name,id:id,tenant:tenantId}"
    rc, out, err = core.run([exe, "account", "show", "--query", query] + flags + selection, 8, env=env)
    if rc != 0:
        return _failure(data, "azure", rc, out, err, ctx.env)
    try:
        account = json.loads(out)
    except ValueError:
        account = None
    keys = ("account", "subscription", "id", "tenant")
    if not isinstance(account, dict) or not any(account.get(k) for k in keys) or any(
            account.get(k) is not None and not isinstance(account[k], str) for k in keys):
        return _result(data, "error", error="unparseable Azure account reply")
    data.update(account=_text(account.get("account"), ctx.env), subscription=_text(account.get("subscription"), ctx.env),
                subscription_id=_text(account.get("id"), ctx.env), tenant=_text(account.get("tenant"), ctx.env))
    subscription_id = account.get("id") or ""
    # `az login --allow-no-subscriptions` creates a tenant-level local account.
    if not subscription_id or data["subscription"] == "N/A(tenant level account)":
        return _result(data, "configured", error="no subscription set; access not checked")
    if not _SUBSCRIPTION.fullmatch(subscription_id):
        return _result(data, "error", error="invalid subscription ID")
    # account show is cached; list-locations queries the selected cloud's API.
    rc, out, err = core.run([exe, "account", "list-locations", "--subscription=" + subscription_id,
                             "--query", "[].name"] + flags, 18, env=env)
    if rc != 0:
        return _failure(data, "azure", rc, out, err, ctx.env)
    try:
        locations = json.loads(out)
    except ValueError:
        locations = None
    if not isinstance(locations, list) or any(not isinstance(loc, str) or not loc for loc in locations):
        return _result(data, "error", error="unparseable Azure location reply")
    return _result(data, "ok")
