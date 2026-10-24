"""Credentials for the collector, and the scrubbing that keeps them out of output.

Only `porchlight collect` loads secrets; the login render never does. Sources:
  * the process environment
  * [general] secrets_files: KEY=VALUE / `export KEY=VALUE` files, parsed in
    memory without a shell (quotes stripped; ~ and $HOME expanded, nothing else)
  * [general] secrets_command: a command that prints KEY=VALUE lines, for
    password managers (`op inject`, `pass`, `doppler secrets download --format env`, ...)

Values never land in a cache file, a log line, or another process's argv.
Error text from sources is scrubbed twice: every secret-looking environment
value is replaced by <redacted:NAME>, then anything token-shaped is masked.
"""
from __future__ import annotations

import os
import re

from . import core

_ENV = None
_SENSITIVE = re.compile(r"(KEY|TOKEN|SECRET|PASS|PWD|AUTH|CREDENTIAL|SID|PRIVATE|COOKIE|SESSION|URI|DSN)", re.I)
# Anchored so a word that merely contains a prefix ("disk-data") is left alone.
_TOKENISH = re.compile(r"(?<![A-Za-z0-9])(xai-|sk-ant-|sk_live_|sk_test_|rk_live_|sk-|gh[pousr]_|glpat-|eyJ)[A-Za-z0-9._\-]{6,}")
_LONG = re.compile(r"[A-Za-z0-9_\-+/=]{40,}")
_URI_CREDS = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@:]+:[^\s/@]+@", re.I)


def load(cfg, refresh=False) -> dict:
    """The merged environment a source sees. Files and the command win over the
    inherited environment: a long-lived tmux server or login session can hold a
    key that has since been rotated in the file."""
    global _ENV
    if _ENV is not None and not refresh:
        return _ENV
    env = dict(os.environ)
    for path in cfg.secrets_files():
        env.update(core.dotenv(path))
    cmd = cfg.secrets_command()
    if cmd:
        argv = cmd if isinstance(cmd, list) else ["/bin/sh", "-c", str(cmd)]
        rc, out, _ = core.run(argv, timeout=20)
        if rc == 0:
            for line in out.splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    k = k.replace("export ", "", 1).strip()
                    if k.replace("_", "").isalnum():
                        env[k] = v.strip().strip("'\"")
    _ENV = env
    return env


# Names that match the pattern but hold paths, not secrets (redacting them only garbles messages).
_STRONG = re.compile(r"(PASSWORD|PASSWD|SECRET|TOKEN|APIKEY|API_KEY|_KEY$|PRIVATE|CREDENTIAL)", re.I)
_NOT_SECRET = {"PWD", "OLDPWD", "SSH_AUTH_SOCK", "XAUTHORITY", "SSH_AGENT_PID", "GPG_AGENT_INFO"}


_PASSWORDY = re.compile(r"(PASSWORD|PASSWD|PASSPHRASE)", re.I)
_PLACEHOLDERS = {"true", "false", "none", "null", "unset", "empty"}  # settings, even under a password name


def masks(name: str, value) -> bool:
    """Whether a variable's value is scrubbed from text. Under a password name any
    value of four or more characters is: a short password is still a password.
    Under another strong name (SECRET, TOKEN, ..._KEY) eight characters are
    needed, since MAX_TOKENS=4096 or SECRET_BACKEND=file is a setting and real
    tokens are long. Under a weaker one, short plain words ('default',
    'subscription') are skipped too: masking them would corrupt output, and real
    secrets are long or contain digits or symbols."""
    if not isinstance(value, str) or not _SENSITIVE.search(name) or name in _NOT_SECRET:
        return False
    if _PASSWORDY.search(name):
        return len(value) >= 4 and value.lower() not in _PLACEHOLDERS
    if _STRONG.search(name):
        return len(value) >= 8
    return len(value) >= 8 and (len(value) >= 20 or bool(re.search(r"[^A-Za-z]", value)))


def sensitive_values(env: dict) -> list:
    """Values of secret-looking variables (see masks), longest first."""
    return sorted({v for k, v in env.items() if masks(k, v)}, key=len, reverse=True)


def child_env(env: dict, keep=()) -> dict:
    """The environment for a child process: this process's own minus anything
    secret-looking, plus the variables under `keep` prefixes from `env`. Service
    bearer tokens (AWS_BEARER_TOKEN_*) never ride along."""
    out = {k: v for k, v in os.environ.items() if not (_SENSITIVE.search(k) and k not in _NOT_SECRET)
           or k.startswith(tuple(keep))}
    out.update({k: str(v) for k, v in env.items() if k.startswith(tuple(keep))})
    return {k: v for k, v in out.items() if not k.startswith("AWS_BEARER_TOKEN")}


def mask(text: str, env=None) -> str:
    """redact() without collapsing whitespace or truncating: for command output
    that is parsed afterwards (JSON, lines) and cached."""
    if not text:
        return text
    env = env if env is not None else (_ENV or {})
    for v in sensitive_values(env):
        if v in text:
            text = text.replace(v, "<redacted>")
    text = _URI_CREDS.sub(r"\1<redacted>@", text)
    return _TOKENISH.sub(lambda m: m.group(1) + "…", text)


def redact(text, env=None, limit: int = 300) -> str:
    """Scrub secret values (by name) and token-shaped strings from error text."""
    if not text:
        return text
    if env is None:
        env = _ENV or {}
    named = [(v, next((k for k, x in env.items() if x == v and masks(k, x)), "secret")) for v in sensitive_values(env)]
    # Values first, on the raw text: collapsing whitespace before would let a secret
    # with tabs, newlines or runs of spaces slip past. Then every value once more,
    # flattened, on the collapsed text: "amber quiet river" printed with a tab and
    # a newline matches only after the collapse.
    text = str(text)
    for v, name in named:
        text = text.replace(v, "<redacted:%s>" % name)
    text = " ".join(text.split())
    for v, name in named:
        flat = " ".join(v.split())
        if flat:
            text = text.replace(flat, "<redacted:%s>" % name)
    text = _URI_CREDS.sub(r"\1<redacted>@", text)
    text = _TOKENISH.sub(lambda m: m.group(1) + "…", text)
    text = _LONG.sub("<redacted>", text)
    return text[:limit]
