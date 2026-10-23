"""The rules that read a worker's Hermes transcript back out of its session store.

Two readers walk the same transcript: the session-kv feed
(`session_kv_server.py`, `GET /v1/tasks/{id}` and `GET /v1/sessions/{id}/tasks`)
serves a card's live worker steps to the chat surfaces, and the bench's in-pod
trajectory read (`bench/kube_agents_bench/worker_trajectory.py`) appends the
worker's tool calls to a run record. Each scrubs the text with the install's
`AuditRedactor` and then with a supplement for the shapes the redactor leaves:
block scalars under `data:`/`stringData:`, the JSON `data` object, URL userinfo,
`NAME: value` and `NAME=value` lines, the CLI credential flags (`-u user:pass`,
`-p pass`, `--password pass`), the bearer-scheme pair, and the secret-read
command detector that withholds a result instead of scrubbing it. Each reader
used to keep its own copy of the supplement and of the card-to-session mapping
(`task_runs.metadata.worker_session_id`, the dispatcher's prompt), and a fix
landed in one of them: `curl -u admin:hunter2` was masked in the feed and kept
in a bench trajectory. This module is the one copy, as `credential_shapes.py`
is for the credential proxy and the feedback broker. A new shape lands here
and nowhere else.

The module is stdlib only and never loads the redactor itself: every rule
takes it as an argument, so the server passes the one it loaded lazily and
the bench's in-pod script passes the one it loaded by path. The agent image
copies the scripts tree whole, so it ships at
`/opt/defaults/scripts/worker_transcript.py`; the server imports it as a
sibling module and the in-pod script loads it from that path.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

# The markers the redactor leaves, spelled once so a reader grepping a
# transcript finds one marker whichever rule masked the value.
REDACTED_SECRET_MARKER = "[REDACTED_SECRET]"
REDACTED_PRIVATE_KEY_MARKER = "[REDACTED_PRIVATE_KEY]"
# Every marker the redactor and this module write starts with this; a block
# whose every line already carries one is not masked again.
REDACTED_MARKER_PREFIX = "[REDACTED_"
# The step kinds whose text takes a rule of its own: a `tool_call` carries a
# shell command the continuation fold applies to, a `tool_result` the
# inline-object rule.
STEP_KIND_TOOL_CALL = "tool_call"
STEP_KIND_TOOL_RESULT = "tool_result"
# The first character of a string that may be a JSON document.
JSON_STRUCTURE_OPENERS = "{["
# The Kubernetes keys and kind whose mapping is blanked wholesale.
SECRET_DATA_KEY = "data"
SECRET_STRING_DATA_KEY = "stringData"
KIND_KEY = "kind"
SECRET_KIND = "Secret"
ENV_NAME_KEY = "name"
ENV_VALUE_KEY = "value"
# The redactor's own list of key suffixes that name no credential (`name`,
# `path`, ...), read from the module it was loaded from under this attribute;
# a redactor that predates the list falls back to the same words.
REDACTOR_NON_CREDENTIAL_SUFFIXES_ATTR = "NON_CREDENTIAL_KEY_SUFFIXES"
NON_CREDENTIAL_KEY_SUFFIXES_FALLBACK = frozenset({"name", "names", "path", "file", "mode", "ref", "type", "kind"})
# A flat base64 map (a Secret's `data` with the envelope cut off) is masked
# when one value is at least this long or sits under a credential-named key.
FLAT_BASE64_LONG_VALUE_MIN_CHARS = 16
BASE64_SECRET_VALUE_MIN_CHARS = 4
BASE64_QUANTUM_CHARS = 4
BASE64_PADDING_CHAR = "="
BASE64_DECODED_TEXT_WHITESPACE = "\n\r\t"
# A CLI value that starts with this is the next flag, not a credential.
CLI_FLAG_LEAD_CHAR = "-"
CLI_FLAG_WORD_SEPARATOR = "_"
CLI_FLAG_NORMALISED_SEPARATOR = "-"
# The user prompt the kanban dispatcher starts a worker session with
# (`hermes -p <profile> chat -q "work kanban task <id>"`). Both readers map a
# card to its worker session through it when `task_runs.metadata` does not
# record `worker_session_id`.
WORKER_PROMPT_PREFIX = "work kanban task "
# The id ends where the prompt's does: a card whose id merely starts with
# another's is not that card.
WORKER_PROMPT_TASK_ID_RE = r"([\w-]+)"
WORKER_PROMPT_TASK_ID_BOUNDARY_RE = r"(?![\w-])"
WORKER_PROMPT_TASK_RE = re.compile(re.escape(WORKER_PROMPT_PREFIX) + WORKER_PROMPT_TASK_ID_RE + WORKER_PROMPT_TASK_ID_BOUNDARY_RE)
# Where a profile's Hermes session store sits under the data root: the
# default profile's in the root itself, every other profile's under
# `profiles/<name>/`.
DEFAULT_PROFILE = "default"
PROFILES_DIR = "profiles"
HERMES_STATE_DB_FILENAME = "state.db"
# The `task_runs` columns the stamped-session walk reads, and the metadata
# key Hermes writes when the worker ran with `HERMES_SESSION_ID` set.
TASK_RUN_METADATA_FIELD = "metadata"
TASK_RUN_PROFILE_FIELD = "profile"
WORKER_SESSION_ID_METADATA_KEY = "worker_session_id"
# The block rules below run only on text that can hold a `data:` block;
# `stringData` carries its only "data" capitalised.
SECRET_BLOCK_FAST_PATH_WORDS = ("data", "Data")
JSON_STRING_DATA_KEY_QUOTED = '"' + SECRET_STRING_DATA_KEY + '"'
JSON_OBJECT_OPEN = "{"
JSON_OBJECT_CLOSE = "}"
YAML_SECRET_BLOCK_RE = re.compile(r"^(\s*)(data|stringData)\s*:\s*$")
YAML_PAIR_RE = re.compile(r"^(\s*)([^\s:]+)\s*:\s*(.*)$")
JSON_SECRET_BLOCK_RE = re.compile(r'("(?:data|stringData)"\s*:\s*\{)([^{}]*)(\}|\Z)')
JSON_PAIR_RE = re.compile(r'("(?:[^"\\]|\\.)*"\s*:\s*")((?:[^"\\]|\\.)*)("|\Z)')
URL_USERINFO_RE = re.compile(r"(://[^\s:/?#@\[\]\"'<>{}\\|^`,;]*:)[^\s/?#\[\]\"'<>{}\\|^`,;]+(?=@)")
UNTERMINATED_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+|PGP\s+)?PRIVATE\s+KEY(?:\s+BLOCK)?-----[ \t]*\r?\n[A-Za-z0-9+/=\s]*\Z",
    re.IGNORECASE,
)
CLI_LONG_FLAG_RE = re.compile(
    r"(?P<prefix>(?:^|\s)--)(?P<flag>[A-Za-z0-9][A-Za-z0-9_-]*)(?P<sep>[ \t=]+)"
    r"(?P<val>\"(?:[^\"\\\r\n]|\\.)*\"|'[^'\r\n]*'|\S+)"
)
CLI_EXTRA_SECRET_FLAGS = frozenset(
    {
        "docker-password",
        "admin-password",
        "auth-token",
        "authtoken",
        "bearer-token",
        "passphrase",
    }
)
CLI_NON_SECRET_FLAGS = frozenset({"with-token"})
CLI_USERPASS_FLAG_RE = re.compile(
    r"\b(?:curl|wget|mysql|mysqldump|mariadb|mysqladmin|(?:docker|helm(?:\s+registry)?)\s+login)\b[^\r\n;|&]*"
)
CLI_USERPASS_ARG_RE = re.compile(
    r"(?P<prefix>\s(?:-u|--user)[ \t=]*)"
    r"(?:(?P<quote>[\"'])(?P<quser>[^:\s\"']+:)(?P<qval>(?:(?!(?P=quote))[^\r\n])+)(?P=quote)"
    r"|(?P<user>[^:\s\"']+:)(?P<val>\"(?:[^\"\\\r\n]|\\.)*\"|'[^'\r\n]*'|[^\s\"']+))"
)
CLI_SHORT_P_RE = re.compile(
    r"(?P<prefix>\b(?:mysql|mysqldump|mariadb|mysqladmin|sshpass|mongosh|mongo|redis-cli|az\s+login|(?:docker|helm(?:\s+registry)?)\s+login)\b"
    r"[^\r\n;|&]{0,512}?\s-(?:p|a)[ \t=]*)"
    r"(?P<val>\"(?:[^\"\\\r\n]|\\.)*\"|'[^'\r\n]*'|[^\s\"']+)"
)
# The two rules above end a command's segment at `;`, `|` or `&`, but a
# shell operator inside a quoted argument (`?query=x&type=dash-db`,
# `-e "select 1 | 2"`) is data. `sub_shell_segments` therefore finds the
# segment on a copy of the text whose quoted interiors are blanked to spaces
# (same length, so every offset holds) and reads the flag and its value out
# of the original at that span. A quote that does not close on its own line
# is left as written, which is how a shell would reject it.
SHELL_QUOTE_CHARS = ("'", '"')
SHELL_QUOTE_BLANK_CHAR = " "
SHELL_QUOTED_SPAN_RE = re.compile(r"'[^'\r\n]*'|\"(?:[^\"\\\r\n]|\\[^\r\n])*\"")
# A scheme-led value (`Bearer <token>`) after any credential-named key, with
# `:` or `=` as the separator: the redactor's own bearer rule needs twelve
# token characters from its alphabet, so a short or `~`-bearing token after
# `auth:`, `token:` or `AUTH_HEADER=` would otherwise outlive the marker.
# The redactor runs first and, for a key its own key rule knows (`token:`),
# masks the scheme word as the value; the marker it leaves there counts as
# the scheme so the token after it is still taken.
AUTH_SCHEME_HEADER_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?\b(?P<key>[\w.\-]+)[\"']?[ \t]*[:=][ \t]*"
    r"(?:bearer|basic|token|apikey|api-key|digest|negotiate|aws4-hmac-sha256|mutual|"
    + re.escape(REDACTED_SECRET_MARKER)
    + r")[ \t]+)"
    r"(?P<val>[^\s\"',}{\[\]\\]+)"
)
TEXT_COLON_KV_RE = re.compile(
    r"(?m)^(?P<prefix>[ \t]*(?:-[ \t]+)?[\"']?)(?P<key>[\w.\-]+)(?P<sep>[\"']?[ \t]*:[ \t]*)"
    r"(?P<val>\"(?:[^\"\\\r\n]|\\.)*(?:\"|\Z)|'[^'\r\n]*(?:'|\Z)|[^\s\"',}{\[\]]+)"
)
QUOTED_COLON_KV_RE = re.compile(
    r"(?P<prefix>[\"'])(?P<key>[\w.\-]+)(?P<sep>(?P=prefix)[ \t]*:[ \t]*)"
    r"(?P<val>\"(?:[^\"\\\r\n]|\\.)*(?:\"|\Z)|'[^'\r\n]*(?:'|\Z)|[^\s\"',}{\[\]]+)"
)
TEXT_EQUALS_KV_RE = re.compile(
    r"(?<![\w.\-/\"])(?P<prefix>)(?P<key>[\w.\-]+)(?P<sep>[ \t]*=[ \t]*)"
    r"(?P<val>\"(?:[^\"\\\r\n]|\\.)*\"|'[^'\r\n]*'|[^\s\"',}{\[\]]+)"
)
SUPPLEMENT_BLOCK_SCALAR_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?:-[ \t]+)?[\"']?(?P<key>[\w.\-]+)[\"']?[ \t]*:[ \t]*[|>][+-]?[0-9]?[+-]?[ \t]*\r?\n"
    r"(?P<body>(?:(?:[ \t]*\r?\n)*(?P=indent)[ \t]+[^\r\n]*(?:\r?\n|$))+)"
)
SUPPLEMENT_ENV_BLOCK_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?:-[ \t]+)?name:[ \t]*(?P<nq>[\"']?)(?P<name>[\w.\-]+)(?P=nq)"
    r"[ \t]*\r?\n(?P=indent)(?P<keyindent>[ \t]*)value:[ \t]*[|>][+-]?[0-9]?[+-]?[ \t]*\r?\n"
    r"(?P<body>(?:(?:[ \t]*\r?\n)*(?P=indent)(?P=keyindent)[ \t]+[^\r\n]*(?:\r?\n|$))+)"
)
SUPPLEMENT_ENV_YAML_PATTERNS = (
    re.compile(
        r"(?m)^(?P<indent>[ \t]*)(?:-[ \t]+)?name:[ \t]*(?P<nq>[\"']?)(?P<name>[\w.\-]+)(?P=nq)"
        r"[ \t]*\r?\n(?P=indent)[ \t]*value:[ \t]*(?P<value>(?:\S[^\r\n]*)?)(?=\r?$)"
    ),
    re.compile(
        r"(?m)^(?P<indent>[ \t]*)(?:-[ \t]+)?value:[ \t]*(?P<value>(?:\S[^\r\n]*)?)\r?\n"
        r"(?P=indent)[ \t]*name:[ \t]*(?P<nq>[\"']?)(?P<name>[\w.\-]+)(?P=nq)[ \t]*(?=\r?$)"
    ),
)
KEY_DATA_WORDS = ({"key", "data"}, {"certificate", "data"})
# A key that is a file name (`tls.crt`, `cert.pem`) opens ordinary diagnostic
# lines: `kubectl describe secret` rows (`tls.crt:  1679 bytes`), `openssl
# verify` (`cert.pem: OK`), `file` (`cert.pem: PEM certificate`), `grep -n`
# (`cert.pem:1:-----BEGIN`, `cert.pem:28:-----END`). The value there is a
# byte count followed by the word `bytes`, a line number with at most a PEM
# marker after it, or one of the words those tools print, never key
# material, so the name alone does not mask it. A `grep -n` body line
# (`tls.key:2:MIIEvQ...`), a bare number (`api.key: 1234567890123456`) and a
# describe row of a non-file key (`token:  1234 bytes`) stay masked: the
# first two can be a credential, the third is over-masking that is safe.
FILE_NAME_KEY_SUFFIXES = (".key", ".pem", ".crt", ".p12", ".pfx")
FILE_KEY_LINE_NUMBER_RE = re.compile(r"^\d+:(?:-----[A-Za-z]*)?$")
FILE_KEY_BYTE_COUNT_RE = re.compile(r"^\d+$")
FILE_KEY_BYTES_WORD_RE = re.compile(r"^[ \t]+bytes\b")
FILE_KEY_BENIGN_WORDS = frozenset(
    {"ok", "pem", "openssh", "data", "ascii", "certificate", "verification", "error", "cannot", "unable", "bytes"}
)
SENSITIVE_KEY_SUFFIXES = (
    *FILE_NAME_KEY_SUFFIXES,
    ".dockercfg",
    ".dockerconfigjson",
    "privatekey",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
)
BASE64_SECRET_VALUE_RE = re.compile(r"^[A-Za-z0-9+/]{4,}={0,2}$")
INLINE_JSON_OBJECT_RE = re.compile(r"\{[^{}]+\}")
# A `kubectl get` of a secret whose output is shaped (`-o`/`--output`/
# `--template`, in any spelling: `-o json`, `-o=go-template=...`,
# `--output=yaml`) or piped anywhere (`| jq`, `| yq`, `| base64`) can print a
# value; the formatter is not enumerated because the agent writes the command
# and a form the list does not name would land as the value alone, which no
# key rule recognises. kubectl takes its flags in any position, so the two
# conditions are lookaheads from `kubectl`, not a token order: `get` with
# `secret` somewhere after it on the same unpiped segment, and a flag or a
# pipe anywhere on the line. A whitespace-led `-o` counts whatever follows
# it (`-ojson`, `-o'go-template=...'`, `-o"..."`, `-o$FMT`): kubectl reads
# the attached text as the format, and a quote or a variable is the shell's
# to resolve. A bare list (`kubectl get secrets -n ns`) prints names and ages
# only; a ConfigMap whose name contains `secret` is withheld too, which is
# the safe direction. gcloud and aws take global flags and a release track
# before the subcommand (`gcloud --project=p beta secrets versions access`,
# `aws --region r secretsmanager get-secret-value`), so those two match the
# subcommand anywhere on the same unpiped segment after the binary.
# A shell joins a backslash-newline before it parses, so the detector folds
# the continuation (and the indentation after it) to one space first; every
# alternative below is line-bounded and would otherwise see two commands.
SHELL_CONTINUATION_RE = re.compile(r"\\\r?\n[ \t]*")
# The tool-call argument keys that carry a shell command, in the order
# `is_secret_read_tool_call` and `scrub_step_text` read them.
COMMAND_ARG_KEYS = ("command", "cmd")
SECRET_READ_COMMAND_RE = re.compile(
    r"(?i)(?:"
    r"\bkubectl\b(?=[^\r\n]*\bget\b[^\r\n|]*\bsecrets?\b)"
    r"(?=[^\r\n]*(?:(?:^|\s)(?:-o|--output(?:[=\s]|$)|--template(?:[=\s]|$))|\|))"
    r"|\bjsonpath\s*=\s*['\"]?[^'\"]*\.(?:data|stringData)\b"
    r"|\bgcloud\b[^\r\n|;&]*\bsecrets\s+versions\s+access\b"
    r"|\baws\b[^\r\n|;&]*\bsecretsmanager\s+get-secret-value\b"
    r"|\baws\b[^\r\n|;&]*\bssm\s+get-parameters?\b"
    r"|\baz\b[^\r\n|;&]*\bkeyvault\s+secret\s+show\b"
    r"|\bvault\s+(?:kv\s+get|read)\b"
    r"|\bsops\s+(?:-d|--decrypt|decrypt)\b"
    # A file read (also after `kubectl exec ... --`) under a well-known secret
    # mount, or under a directory component that is, or ends in `-`,
    # `secret(s)`, `creds` or `credentials` (`/etc/db-creds/password`).
    r"|\b(?:cat|head|tail|less|more|base64)\b[^\r\n]*/"
    r"(?:var/run/secrets|etc/kubernetes/pki|etc/ssl/private|\.ssh/id_|(?:[^\s/]*-)?(?:secrets?|creds|credentials)/)[^\r\n]*"
    r")"
)


def scrub_secret_blocks(redactor: Any, text: str) -> str:
    # `stringData` carries its only "data" capitalised; the fast path must
    # let it through to the block rules below.
    if not any(word in text for word in SECRET_BLOCK_FAST_PATH_WORDS):
        return text
    lines = text.split("\n")
    block: Optional[int] = None
    for i, line in enumerate(lines):
        opener = YAML_SECRET_BLOCK_RE.match(line)
        if opener:
            block = len(opener.group(1))
            continue
        if block is None or not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= block:
            block = None
            continue
        pair = YAML_PAIR_RE.match(line)
        if pair is None:
            lines[i] = " " * indent + REDACTED_SECRET_MARKER
        elif pair.group(3):
            lines[i] = f"{pair.group(1)}{pair.group(2)}: {REDACTED_SECRET_MARKER}"
    joined = "\n".join(lines)

    def _mask_json_block(m: "re.Match[str]") -> str:
        if JSON_STRING_DATA_KEY_QUOTED not in m.group(1) and not redactor.JSON_CREDENTIAL_KIND_PATTERN.search(joined):
            return m.group(0)
        return (
            m.group(1)
            + JSON_PAIR_RE.sub(lambda p: p.group(1) + REDACTED_SECRET_MARKER + p.group(3), m.group(2))
            + m.group(3)
        )

    return JSON_SECRET_BLOCK_RE.sub(_mask_json_block, joined)


def is_non_credential_key_suffix(redactor: Any, name: str) -> bool:
    last_word = redactor._last_key_word(name)
    # The list lives beside the class in the module it was loaded from,
    # under whatever name the caller registered that module.
    non_cred_suffixes = getattr(
        sys.modules.get(getattr(redactor, "__module__", "")),
        REDACTOR_NON_CREDENTIAL_SUFFIXES_ATTR,
        NON_CREDENTIAL_KEY_SUFFIXES_FALLBACK,
    )
    return last_word in non_cred_suffixes


def cli_flag_is_sensitive(redactor: Any, flag: str) -> bool:
    norm_flag = flag.lower().replace(CLI_FLAG_WORD_SEPARATOR, CLI_FLAG_NORMALISED_SEPARATOR)
    if norm_flag in CLI_NON_SECRET_FLAGS or is_non_credential_key_suffix(redactor, norm_flag):
        return False
    return norm_flag in CLI_EXTRA_SECRET_FLAGS or bool(redactor.CREDENTIAL_NAME_PATTERN.match(norm_flag))


def dict_key_needs_supplement(redactor: Any, name: str) -> bool:
    """Supplemental dictionary key check for keys `AuditRedactor.SENSITIVE_KEYS`
    does not cover (`client-key-data`, `client-certificate-data`, and `.key`/`.crt`
    file-name keys)."""
    if is_non_credential_key_suffix(redactor, name):
        return False
    words = redactor._get_key_words(name)
    return (
        any(w <= words for w in KEY_DATA_WORDS)
        or name.lower().endswith(SENSITIVE_KEY_SUFFIXES)
    )


def named_value_is_sensitive(redactor: Any, name: str) -> bool:
    if is_non_credential_key_suffix(redactor, name):
        return False
    words = redactor._get_key_words(name)
    return (
        bool(words & redactor.SENSITIVE_KEYS)
        or any(w <= words for w in KEY_DATA_WORDS)
        or name.lower().endswith(SENSITIVE_KEY_SUFFIXES)
    )


def blank_quoted_spans(text: str) -> str:
    """Return `text` with the interior of every single- or double-quoted span
    replaced by spaces, keeping the quotes and the length."""
    if not any(q in text for q in SHELL_QUOTE_CHARS):
        return text
    return SHELL_QUOTED_SPAN_RE.sub(
        lambda m: m.group(0)[0] + SHELL_QUOTE_BLANK_CHAR * (len(m.group(0)) - 2) + m.group(0)[-1], text
    )


def sub_shell_segments(
    segment_re: "re.Pattern[str]",
    text: str,
    mask: "Callable[[re.Match[str], str], str]",
) -> str:
    """Find each `segment_re` match on the quote-blanked copy of `text` and
    replace that span of the original with `mask(match, text)`."""
    blanked = blank_quoted_spans(text)
    out: list[str] = []
    pos = 0
    for match in segment_re.finditer(blanked):
        out.append(text[pos : match.start()])
        out.append(mask(match, text))
        pos = match.end()
    out.append(text[pos:])
    return "".join(out)


def unquote_token(raw: str) -> str:
    stripped = raw.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in SHELL_QUOTE_CHARS:
        return stripped[1:-1]
    if len(stripped) >= 1 and stripped[0] in SHELL_QUOTE_CHARS:
        return stripped[1:]
    return stripped


def scrub_supplement_env_pairs(redactor: Any, text: str) -> str:
    if ENV_NAME_KEY not in text or ENV_VALUE_KEY not in text:
        return text

    def _mask_env_block(match: "re.Match[str]") -> str:
        if not named_value_is_sensitive(redactor, match.group("name")):
            return match.group(0)
        body = match.group("body")
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if lines and all(line.startswith(REDACTED_MARKER_PREFIX) for line in lines):
            return match.group(0)
        body_indent = body[: len(body) - len(body.lstrip(" \t"))]
        ending = "\n" if body.endswith("\n") else ""
        return f"{match.group(0)[: match.start('body') - match.start()]}{body_indent}{REDACTED_SECRET_MARKER}{ending}"

    def _mask_env_span(match: "re.Match[str]", start: int, end: int) -> str:
        value = match.string[start:end]
        if not redactor._is_credential_value(value) or not named_value_is_sensitive(
            redactor, match.group("name")
        ):
            return match.group(0)
        whole = match.group(0)
        return f"{whole[:start - match.start()]}{REDACTED_SECRET_MARKER}{whole[end - match.start():]}"

    def _mask_env_yaml(match: "re.Match[str]") -> str:
        raw = match.group("value").rstrip()
        start = match.start("value")
        if len(raw) > 1 and raw[0] in SHELL_QUOTE_CHARS and raw[-1] == raw[0]:
            return _mask_env_span(match, start + 1, start + len(raw) - 1)
        return _mask_env_span(match, start, start + len(raw))

    def _mask_env_json(match: "re.Match[str]") -> str:
        return _mask_env_span(match, match.start("value"), match.end("value"))

    text = SUPPLEMENT_ENV_BLOCK_RE.sub(_mask_env_block, text)
    for pattern in SUPPLEMENT_ENV_YAML_PATTERNS:
        text = pattern.sub(_mask_env_yaml, text)
    for pattern in getattr(redactor, "ENV_PAIR_JSON_PATTERNS", ()):
        text = pattern.sub(_mask_env_json, text)
    return text


def file_key_value_is_benign(value: str, tail: str) -> bool:
    """True for what a file-named key holds on a diagnostic line and never as
    key material: a line number, a byte count that the word `bytes` follows
    (tail is the text after the value), or one of the words the tools print."""
    if FILE_KEY_LINE_NUMBER_RE.match(value):
        return True
    if FILE_KEY_BYTE_COUNT_RE.match(value):
        return bool(FILE_KEY_BYTES_WORD_RE.match(tail))
    return value.lower() in FILE_KEY_BENIGN_WORDS


def blank_named_values(redactor: Any, text: str) -> str:
    def _mask_block(match: "re.Match[str]") -> str:
        if not named_value_is_sensitive(redactor, match.group("key")):
            return match.group(0)
        body = match.group("body")
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if lines and all(line.startswith(REDACTED_MARKER_PREFIX) for line in lines):
            return match.group(0)
        body_indent = body[: len(body) - len(body.lstrip(" \t"))]
        ending = "\n" if body.endswith("\n") else ""
        head = match.group(0)[: match.start("body") - match.start()]
        return f"{head}{body_indent}{REDACTED_SECRET_MARKER}{ending}"

    def _mask_colon_kv(match: "re.Match[str]") -> str:
        key = match.group("key")
        unquoted = unquote_token(match.group("val"))
        if key.lower().endswith(FILE_NAME_KEY_SUFFIXES) and file_key_value_is_benign(
            unquoted, match.string[match.end() :]
        ):
            return match.group(0)
        if named_value_is_sensitive(redactor, key) and redactor._is_credential_value(unquoted):
            return f"{match.group('prefix')}{key}{match.group('sep')}{REDACTED_SECRET_MARKER}"
        return match.group(0)

    def _mask_equals_kv(match: "re.Match[str]") -> str:
        key = match.group("key")
        raw_val = match.group("val")
        unquoted = unquote_token(raw_val)
        if named_value_is_sensitive(redactor, key) and redactor._is_credential_value(unquoted):
            return f"{match.group('prefix')}{key}{match.group('sep')}{REDACTED_SECRET_MARKER}"
        if "=" in unquoted:
            rescanned = TEXT_EQUALS_KV_RE.sub(_mask_equals_kv, unquoted)
            if rescanned != unquoted:
                if len(raw_val) >= 2 and raw_val[0] == raw_val[-1] and raw_val[0] in SHELL_QUOTE_CHARS:
                    return f"{match.group('prefix')}{key}{match.group('sep')}{raw_val[0]}{rescanned}{raw_val[0]}"
                return f"{match.group('prefix')}{key}{match.group('sep')}{rescanned}"
        return match.group(0)

    def _mask_long_flag(match: "re.Match[str]") -> str:
        flag = match.group("flag")
        raw_val = match.group("val")
        unquoted = unquote_token(raw_val)
        if unquoted.startswith(CLI_FLAG_LEAD_CHAR):
            return match.group(0)
        if cli_flag_is_sensitive(redactor, flag) and redactor._is_credential_value(unquoted):
            return f"{match.group('prefix')}{flag}{match.group('sep')}{REDACTED_SECRET_MARKER}"
        return match.group(0)

    def _mask_short_p(match: "re.Match[str]", source: str) -> str:
        # `match` comes from the quote-blanked copy; the value is read from `source`.
        unquoted = unquote_token(source[match.start("val") : match.end("val")])
        if unquoted.startswith(CLI_FLAG_LEAD_CHAR) or not redactor._is_credential_value(unquoted):
            return source[match.start() : match.end()]
        return f"{source[match.start() : match.start('val')]}{REDACTED_SECRET_MARKER}"

    def _mask_userpass_segment(match: "re.Match[str]", source: str) -> str:
        return CLI_USERPASS_ARG_RE.sub(_mask_userpass, source[match.start() : match.end()])

    def _mask_userpass(match: "re.Match[str]") -> str:
        quote = match.group("quote") or ""
        user = match.group("quser") if quote else match.group("user")
        val = match.group("qval") if quote else unquote_token(match.group("val"))
        if val.startswith(CLI_FLAG_LEAD_CHAR) or not redactor._is_credential_value(val):
            return match.group(0)
        return f"{match.group('prefix')}{quote}{user}{REDACTED_SECRET_MARKER}{quote}"

    def _mask_auth_scheme(match: "re.Match[str]") -> str:
        val = unquote_token(match.group("val"))
        if not named_value_is_sensitive(redactor, match.group("key")) or not redactor._is_credential_value(val):
            return match.group(0)
        return f"{match.group('prefix')}{REDACTED_SECRET_MARKER}"

    text = scrub_supplement_env_pairs(redactor, text)
    text = SUPPLEMENT_BLOCK_SCALAR_RE.sub(_mask_block, text)
    text = AUTH_SCHEME_HEADER_RE.sub(_mask_auth_scheme, text)
    text = CLI_LONG_FLAG_RE.sub(_mask_long_flag, text)
    text = sub_shell_segments(CLI_USERPASS_FLAG_RE, text, _mask_userpass_segment)
    text = sub_shell_segments(CLI_SHORT_P_RE, text, _mask_short_p)
    text = TEXT_COLON_KV_RE.sub(_mask_colon_kv, text)
    text = QUOTED_COLON_KV_RE.sub(_mask_colon_kv, text)
    return TEXT_EQUALS_KV_RE.sub(_mask_equals_kv, text)


def as_json_structure(text: str) -> Any:
    if text.lstrip()[:1] not in JSON_STRUCTURE_OPENERS:
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def is_base64_encoded_secret_value(v: str) -> bool:
    if len(v) < BASE64_SECRET_VALUE_MIN_CHARS or len(v) % BASE64_QUANTUM_CHARS != 0 or BASE64_SECRET_VALUE_RE.fullmatch(v) is None:
        return False
    try:
        raw = base64.b64decode(v, validate=True)
    except ValueError:
        return False
    if not raw:
        return False
    try:
        decoded_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return v.endswith(BASE64_PADDING_CHAR)
    return all(ch.isprintable() or ch in BASE64_DECODED_TEXT_WHITESPACE for ch in decoded_text)


def is_flat_base64_secret_map(redactor: Any, value: Dict[Any, Any]) -> bool:
    if not value:
        return False
    has_long_or_sensitive = False
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            return False
        if not is_base64_encoded_secret_value(v):
            return False
        if len(v) >= FLAT_BASE64_LONG_VALUE_MIN_CHARS or named_value_is_sensitive(redactor, k):
            has_long_or_sensitive = True
    return has_long_or_sensitive


def supplement_redact(
    redactor: Any,
    value: Any,
    key: Optional[str] = None,
    *,
    is_tool_result: bool = False,
    parent_is_secret: bool = False,
) -> Any:
    if isinstance(value, str):
        nested = as_json_structure(value)
        if nested is not None:
            return json.dumps(
                supplement_redact(redactor, nested, key=None, is_tool_result=is_tool_result)
            )
        if is_tool_result and JSON_OBJECT_OPEN in value and JSON_OBJECT_CLOSE in value:

            def _scrub_inline_obj(m: "re.Match[str]") -> str:
                obj = as_json_structure(m.group(0))
                if isinstance(obj, dict) and is_flat_base64_secret_map(redactor, obj):
                    return json.dumps(
                        {
                            k: REDACTED_SECRET_MARKER if isinstance(v, str) else v
                            for k, v in obj.items()
                        }
                    )
                return m.group(0)

            value = INLINE_JSON_OBJECT_RE.sub(_scrub_inline_obj, value)
        text = redactor.PRIVATE_KEY_PATTERN.sub(REDACTED_PRIVATE_KEY_MARKER, value)
        text = UNTERMINATED_PRIVATE_KEY_RE.sub(REDACTED_PRIVATE_KEY_MARKER, text)
        text = URL_USERINFO_RE.sub(lambda m: m.group(1) + REDACTED_SECRET_MARKER, text)
        return blank_named_values(redactor, scrub_secret_blocks(redactor, text))
    if isinstance(value, dict):
        if (
            key == SECRET_STRING_DATA_KEY
            or (key == SECRET_DATA_KEY and parent_is_secret)
            or (is_tool_result and key is None and is_flat_base64_secret_map(redactor, value))
        ):
            return {
                k: REDACTED_SECRET_MARKER
                if isinstance(v, str)
                else supplement_redact(redactor, v, str(k), is_tool_result=is_tool_result)
                for k, v in value.items()
            }
        is_secret_obj = value.get(KIND_KEY) == SECRET_KIND
        env_name = value.get(ENV_NAME_KEY)
        is_cred_env = isinstance(env_name, str) and named_value_is_sensitive(redactor, env_name)
        return {
            k: (
                REDACTED_SECRET_MARKER
                if isinstance(v, str)
                and ((is_cred_env and k == ENV_VALUE_KEY) or dict_key_needs_supplement(redactor, str(k)))
                and redactor._is_credential_value(v.strip())
                else supplement_redact(
                    redactor,
                    v,
                    str(k),
                    is_tool_result=is_tool_result,
                    parent_is_secret=is_secret_obj,
                )
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [supplement_redact(redactor, v, is_tool_result=is_tool_result) for v in value]
    return value


def scrub_step_text(redactor: Any, kind: str, text: str, withheld: str) -> str:
    """Scrub one worker step's text: `redactor.redact` first, then the supplement.

    `kind` is the step kind (`thinking`, `tool_call`, `tool_result`, `reply`);
    a `tool_call` has its shell continuations folded first and a
    `tool_result` takes the inline-object rule. Returns `withheld` for every
    kind when `redactor` is `None` or the structure is deeper than Python
    recurses, so an unscrubbable step is never served as written.
    """
    if redactor is None:
        return withheld
    try:
        parsed = as_json_structure(text)
        if kind == STEP_KIND_TOOL_CALL:
            # Fold a shell line continuation once, here, so every line-bounded
            # CLI rule below (`-u user:pass`, `-p pass`, `--password pass`)
            # reads the command the shell would run; `as_json_structure`
            # returns a fresh object, so the fold does not touch the row.
            if isinstance(parsed, dict):
                for arg_key in COMMAND_ARG_KEYS:
                    if isinstance(parsed.get(arg_key), str):
                        parsed[arg_key] = SHELL_CONTINUATION_RE.sub(" ", parsed[arg_key])
            elif parsed is None:
                text = SHELL_CONTINUATION_RE.sub(" ", text)
        if parsed is not None:
            return json.dumps(
                supplement_redact(redactor, redactor.redact(parsed), is_tool_result=(kind == STEP_KIND_TOOL_RESULT))
            )
        return str(supplement_redact(redactor, redactor.redact(text), is_tool_result=(kind == STEP_KIND_TOOL_RESULT)))
    except RecursionError:
        return withheld

def is_secret_read_tool_call(raw_args: Any) -> bool:
    """Whether a tool call's arguments run a command that prints a secret's value.

    `raw_args` is the arguments as Hermes stores them: a JSON string or an
    object whose `command`/`cmd` carries the shell line, or the bare line.
    """
    parsed = as_json_structure(raw_args) if isinstance(raw_args, str) else raw_args
    if isinstance(parsed, dict):
        cmd = next((parsed.get(k) for k in COMMAND_ARG_KEYS if parsed.get(k)), "")
        if isinstance(cmd, str) and SECRET_READ_COMMAND_RE.search(SHELL_CONTINUATION_RE.sub(" ", cmd)):
            return True
    elif isinstance(raw_args, str) and SECRET_READ_COMMAND_RE.search(SHELL_CONTINUATION_RE.sub(" ", raw_args)):
        return True
    return False


def worker_prompt_exact_re(task_id: str) -> "re.Pattern[str]":
    """The pattern that matches the dispatcher's prompt for `task_id` and no longer id.

    A `LIKE '%work kanban task <id>%'` scan also takes a card whose id merely
    starts with this one; the id has to end where the prompt's does.
    """
    return re.compile(re.escape(WORKER_PROMPT_PREFIX + task_id) + WORKER_PROMPT_TASK_ID_BOUNDARY_RE)


def profile_state_db_path(data_root: Path, profile: Optional[str]) -> Path:
    """Where `profile`'s Hermes session store sits under `data_root`.

    The default profile (or none) keeps its store in the root; every other
    profile's is under `profiles/<name>/`. The layout only: the server
    validates the profile name before it opens the path.
    """
    if not profile or profile == DEFAULT_PROFILE:
        return Path(data_root) / HERMES_STATE_DB_FILENAME
    return Path(data_root) / PROFILES_DIR / profile / HERMES_STATE_DB_FILENAME


def _run_field(run: Any, field: str) -> Any:
    # A `task_runs` row as `sqlite3.Row` (the bench) or as a dict (the feed):
    # both index by column name, and neither has every column on every read.
    try:
        return run[field]
    except (KeyError, IndexError, TypeError):
        return None


def run_worker_session_id(run: Any) -> Optional[str]:
    """The session id a `task_runs` row's metadata stamps, or `None`."""
    raw_meta = _run_field(run, TASK_RUN_METADATA_FIELD)
    try:
        meta = json.loads(raw_meta) if isinstance(raw_meta, str) and raw_meta else {}
    except ValueError:
        meta = {}
    sid = meta.get(WORKER_SESSION_ID_METADATA_KEY) if isinstance(meta, dict) else None
    return sid if isinstance(sid, str) and sid else None


def stamped_worker_sessions(runs: Sequence[Any], assignee: Optional[str]) -> list[tuple[str, str]]:
    """The `(session_id, profile)` pairs `task_runs.metadata.worker_session_id` stamps.

    In run order, each session once; the profile is the run's, or the card's
    assignee when the run records none.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for run in runs:
        sid = run_worker_session_id(run)
        if sid is None or sid in seen:
            continue
        seen.add(sid)
        found.append((sid, str(_run_field(run, TASK_RUN_PROFILE_FIELD) or assignee or "")))
    return found
