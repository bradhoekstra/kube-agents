"""Every Terraform test suite runs, against mocks, on every pull request.

A `*.tftest.hcl` file runs only if the `terraform-test` Makefile target's loop
reaches its directory (`terraform/modules/*/tests`, `terraform/examples/*/tests`)
and the `validate` job in validate.yml runs the target; a suite that neither
reaches is a set of cases that passes by never running, the trap AGENTS.md
"Where Tests Go" names for `PYTHON_TEST_DIRS`. And a suite that mocks one
provider but not another would reach a real API from CI on the first read
nobody overrode. This pins the loop, the step, `make verify` running the
target (its help line says it runs everything a pull request must pass
offline), that no test file anywhere in the repository sits where the loop
does not look (Terraform roots live under bench/tf and k8s-operator/testing
too), and an unaliased `mock_provider` block for every provider a root needs:
what it declares in `required_providers` in any of its `.tf` files (a
composition declares them in providers.tf, a module in versions.tf; the block
form and the version-only shorthand both count), what its `resource`, `data`
and `provider` blocks imply (Terraform loads `hashicorp/<prefix>` for an
undeclared type prefix with no warning), what every local module it calls
declares, since Terraform hands a child a default provider the root never
named, and what a module a `run` block loads from the test file itself
declares; a run whose `providers` map routes a provider to a configuration
no `mock_provider` block declares is reported too, and the builtin
`terraform` provider is never demanded. HCL is read through a small tokenizer that skips comments,
strings, heredocs and template interpolation, so none of those can pass for
syntax; a `.tftest.json` is parsed; the `.tf` side is read as HCL only, and a
root or called module carrying a `*.tf.json` fails the pin loudly rather than
being read half-way. The helpers that make those judgements have cases of
their own below, on fixtures, so the pin is known to fire on the shapes it
exists for. tests/test_shellcheck_gate_wiring.py pins a workflow step the
same way.
"""

import json
import pathlib
import re
import sys
import tempfile
import unittest

try:
    from tests.test_shellcheck_gate_wiring import _JOB_ID, _run_lines, _steps
except ImportError:  # run from inside tests/
    from test_shellcheck_gate_wiring import _JOB_ID, _run_lines, _steps

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TERRAFORM_DIR = _REPO_ROOT / "terraform"
#: The directories the loop reaches: the modules and the compositions, the
#: same set the validate job's init-and-validate loop covers.
_SUITE_PARENTS = (_TERRAFORM_DIR / "modules", _TERRAFORM_DIR / "examples")
_MAKEFILE = _REPO_ROOT / "Makefile"

#: Directories a repository walk never reads, the set the Python test
#: discovery guard keeps for the same walk: provider downloads, the docs
#: site's dependencies, git's store, and `.claude`, where a review command
#: leaves worktrees of other branches inside a maintainer's checkout.
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
from test_test_discovery import IGNORED_NAMES as _WALK_EXCLUDED_PARTS  # noqa: E402

#: `_JOB_ID`, `_steps` and `_run_lines` are the shellcheck wiring test's: the
#: same job on main's required-status-checks list, read the same way.
_TARGET = "terraform-test"
_GATE_COMMAND = f"make {_TARGET}"

#: The recipe line that lists the target in `make help`, and the loop that
#: reaches every module rather than naming the ones that had tests when it
#: was written. The command is looked for in the recipe's body, below the
#: target line, since the help text names it too.
_RECIPE_LINE = re.compile(rf"^{_TARGET}: ## \S", re.MULTILINE)
#: A recipe line make or the shell ignores: `#` first on a line that does
#: not start with a tab is make's, and its trailing backslash carries the
#: next line with it; `#` after the tab, at any indent and after the `@`,
#: `-` and `+` prefixes make strips, is the shell's, which ends at the
#: newline whatever the line ends with.
_MAKE_COMMENT = re.compile(r"^ *#")
_SHELL_COMMENT = re.compile(r"^\t[ \t]*[@+-]*[ \t]*#")
_LINE_CONTINUATION = "\\"
#: The rule line itself, at the start of a line, so a mention of the
#: target elsewhere (`pre-verify:`, a comment) is not read as the rule.
_RULE_LINE = "^{target}:"
#: The shell loop skips a name beginning with a dot, so such a directory
#: is never a suite the loop runs, whatever it holds.
_HIDDEN_PREFIX = "."
_LOOP_GLOB = "for dir in terraform/modules/*/ terraform/examples/*/; do"
_TEST_COMMAND = "terraform test"
_VERIFY_TARGET = "verify"
_VERIFY_LINE = f"$(MAKE) --no-print-directory {_TARGET}"

#: The HCL the pin reads, and the spelling it refuses.
_TF_FILE_GLOB = "*.tf"
_TF_JSON_GLOB = "*.tf.json"
#: Tokenizer pieces: an identifier (`google-beta` included, hence the
#: hyphen), a heredoc opener, the comment and block characters.
_IDENT = re.compile(r"[A-Za-z_][\w-]*")
_HEREDOC_OPEN = re.compile(r"<<-?(\w+)\r?\n")
_LINE_COMMENT_OPENERS = ("#", "//")
_BLOCK_COMMENT_OPEN, _BLOCK_COMMENT_CLOSE = "/*", "*/"
_TEMPLATE_OPENERS = ("${", "%{")
_WHITESPACE = " \t\r\n"
#: Token kinds.
_STR, _WORD, _OPEN, _CLOSE, _EQUALS, _OTHER = "str", "word", "{", "}", "=", "other"
#: The blocks read: providers inside `required_providers { … }` as `name = {`
#: or the pre-0.13 `name = "version"`; a `module "x" { source = "../…" }`
#: whose providers the root needs too, since Terraform gives such a child a
#: default provider the root never declared (full-install never declares
#: `http`; the scope resolver it calls requires it); and a test file's
#: `mock_provider "name" { … }`, which mocks the default provider only when
#: it carries no `alias`.
_REQUIRED_PROVIDERS_BLOCK = "required_providers"
#: Blocks whose type prefix implies a provider the root loads whether or
#: not it is declared: `resource "google_x"`, `data "http"`, `provider "tls"`.
_IMPLYING_BLOCKS = (("resource", 2), ("data", 2), ("provider", 1))
_TYPE_PREFIX_SEPARATOR = "_"
#: `terraform_data` and `terraform_remote_state` belong to the builtin
#: provider, which is never fetched and cannot be mocked.
_BUILTIN_PROVIDER_PREFIX = "terraform"
#: A run's `providers = { http = http.live }` routes the module's `http` to
#: the `live` configuration; the pin wants that configuration mocked too.
_PROVIDERS_ATTRIBUTE = "providers"
_JSON_PROVIDERS_KEY = "providers"
_ALIAS_SEPARATOR = "."
_MODULE_BLOCK = "module"
#: A test file's `run "x" { module { source = "./…" } }`, whose module's
#: providers the file has to mock too; in JSON, `run.<name>.module.source`.
_RUN_BLOCK = "run"
_JSON_RUN_KEY, _JSON_MODULE_KEY, _JSON_SOURCE_KEY = "run", "module", "source"
_SOURCE_ATTRIBUTE = "source"
_LOCAL_SOURCE_PREFIXES = ("./", "../")
_MOCK_PROVIDER_BLOCK = "mock_provider"
_ALIAS_ATTRIBUTE = "alias"
#: Both spellings Terraform loads from a suite; the JSON form carries its
#: mocks under this key, one object or a list of them per provider.
_TEST_FILE_SUFFIXES = (".tftest.hcl", ".tftest.json")
_TEST_FILE_JSON_SUFFIX = ".tftest.json"
_TESTS_DIR = "tests"


# ─── Where test files are, and which the loop runs ───────────────────────────


def _is_test_file(path: pathlib.Path) -> bool:
    return path.name.endswith(_TEST_FILE_SUFFIXES)


def _suite_files(root: pathlib.Path) -> list:
    """The test files `terraform test` loads when the loop runs it in `root`:
    those in tests/ and those beside it. The loop runs only where tests/
    exists, so a root-level file with no tests/ beside it runs nowhere."""
    tests_dir = root / _TESTS_DIR
    if not tests_dir.is_dir():
        return []
    return sorted(p for directory in (root, tests_dir) for p in directory.iterdir() if _is_test_file(p))


def _suites(parents=_SUITE_PARENTS) -> dict:
    return {
        root: _suite_files(root)
        for parent in parents
        for root in sorted(parent.iterdir())
        if root.is_dir() and not root.name.startswith(_HIDDEN_PREFIX) and _suite_files(root)
    }


def _unreached_test_files(repo_root: pathlib.Path, parents) -> list:
    reached = {path for files in _suites(parents).values() for path in files}
    # Filtered on the path below the repository, not the absolute one: a
    # checkout that itself sits under an ignored name (a review worktree
    # under .claude/) would otherwise exclude every file and pass vacuously.
    everywhere = {
        path
        for path in repo_root.rglob("*")
        if _is_test_file(path) and not _WALK_EXCLUDED_PARTS & set(path.relative_to(repo_root).parts)
    }
    return sorted(everywhere - reached)


def _recipe(makefile: str, target: str) -> str:
    rule = re.search(_RULE_LINE.format(target=re.escape(target)), makefile, re.MULTILINE)
    if rule is None:
        raise AssertionError(f"the Makefile has no `{target}:` rule")
    recipe = makefile[rule.start():]
    end = recipe.find("\n\n")
    return recipe if end < 0 else recipe[:end]


def _recipe_body(makefile: str, target: str) -> str:
    """The recipe's lines below the target line, less commented-out ones
    and the continuation lines a commented line's trailing backslash
    carries with it."""
    kept = []
    continuing_make_comment = False
    for line in _recipe(makefile, target).split("\n")[1:]:
        if continuing_make_comment or _MAKE_COMMENT.match(line):
            continuing_make_comment = line.rstrip().endswith(_LINE_CONTINUATION)
            continue
        if _SHELL_COMMENT.match(line):
            continue
        kept.append(line)
    return "\n".join(kept)


# ─── Reading HCL ─────────────────────────────────────────────────────────────


def _string_end(text: str, start: int) -> int:
    """Index after the quote closing the string opened at `start`, escapes
    and `${ … }` / `%{ … }` templates (which may hold quotes) skipped."""
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
        elif char == '"':
            return index + 1
        elif text[index : index + 2] in _TEMPLATE_OPENERS:
            index = _template_end(text, index + 2)
        else:
            index += 1
    return len(text)


def _template_end(text: str, start: int) -> int:
    depth = 1
    index = start
    while index < len(text):
        char = text[index]
        if char == '"':
            index = _string_end(text, index)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return len(text)


def _heredoc_end(text: str, start: int, marker: str) -> int:
    index = start
    while index < len(text):
        line_end = text.find("\n", index)
        line_end = len(text) if line_end < 0 else line_end
        if text[index:line_end].strip() == marker:
            return line_end
        index = line_end + 1
    return len(text)


def _tokens(text: str) -> list:
    """HCL as (kind, value) pairs, comments dropped, strings and heredocs
    each one token, so nothing inside them reads as syntax."""
    tokens = []
    index = 0
    while index < len(text):
        char = text[index]
        pair = text[index : index + 2]
        if char in _WHITESPACE:
            index += 1
        elif char == _LINE_COMMENT_OPENERS[0] or pair == _LINE_COMMENT_OPENERS[1]:
            line_end = text.find("\n", index)
            index = len(text) if line_end < 0 else line_end
        elif pair == _BLOCK_COMMENT_OPEN:
            close = text.find(_BLOCK_COMMENT_CLOSE, index + 2)
            index = len(text) if close < 0 else close + 2
        elif char == '"':
            end = _string_end(text, index)
            tokens.append((_STR, text[index + 1 : end - 1]))
            index = end
        elif (heredoc := _HEREDOC_OPEN.match(text, index)) is not None:
            end = _heredoc_end(text, heredoc.end(), heredoc.group(1))
            tokens.append((_STR, text[heredoc.end() : end]))
            index = end
        elif char in (_OPEN, _CLOSE, _EQUALS):
            tokens.append((char, char))
            index += 1
        elif (word := _IDENT.match(text, index)) is not None:
            tokens.append((_WORD, word.group(0)))
            index = word.end()
        else:
            tokens.append((_OTHER, char))
            index += 1
    return tokens


def _body(tokens: list, open_index: int) -> list:
    """The tokens between the brace at `open_index` and its match, each with
    its depth relative to that block (1 = directly inside); a nested block's
    own braces sit at the depth that encloses them."""
    body = []
    depth = 0
    for token in tokens[open_index:]:
        if token[0] == _OPEN:
            if depth > 0:
                body.append((token, depth))
            depth += 1
        elif token[0] == _CLOSE:
            depth -= 1
            if depth == 0:
                return body
            body.append((token, depth))
        else:
            body.append((token, depth))
    return body


def _blocks(tokens: list, name: str, labels: int) -> list:
    """Each block `name "label"… {` at the top level, as (labels, body)."""
    blocks = []
    depth = 0
    for index, token in enumerate(tokens):
        if token[0] == _OPEN:
            depth += 1
        elif token[0] == _CLOSE:
            depth -= 1
        elif (
            depth == 0
            and token == (_WORD, name)
            and all(tokens[index + 1 + n][0] == _STR for n in range(labels) if index + 1 + n < len(tokens))
            and index + 1 + labels < len(tokens)
            and tokens[index + 1 + labels][0] == _OPEN
        ):
            found = [tokens[index + 1 + n][1] for n in range(labels)]
            blocks.append((found, _body(tokens, index + 1 + labels)))
    return blocks


def _headers_at_any_depth(tokens: list, name: str, labels: int) -> list:
    """The labels of every `name "label"… {` header, whatever encloses it:
    a `check` block's scoped data source implies its provider like a
    top-level one."""
    found = []
    for index, token in enumerate(tokens):
        if (
            token == (_WORD, name)
            and index + 1 + labels < len(tokens)
            and all(tokens[index + 1 + n][0] == _STR for n in range(labels))
            and tokens[index + 1 + labels][0] == _OPEN
        ):
            found.append([tokens[index + 1 + n][1] for n in range(labels)])
    return found


def _nested_blocks(body: list, name: str) -> list:
    """Bodies of `name {` blocks directly inside a body (no labels)."""
    flat = [token for token, _depth in body]
    nested = []
    for index, (token, depth) in enumerate(body):
        if depth == 1 and token == (_WORD, name) and index + 1 < len(body) and body[index + 1][0][0] == _OPEN:
            nested.append(_body(flat, index + 1))
    return nested


def _attributes(body: list) -> dict:
    """`name = value` pairs directly inside a body: the value's kind and text."""
    tokens = [token for token, depth in body if depth == 1]
    attributes = {}
    for index in range(len(tokens) - 2):
        if tokens[index][0] == _WORD and tokens[index + 1][0] == _EQUALS:
            attributes[tokens[index][1]] = tokens[index + 2]
    return attributes


def _providers_in(text: str) -> set:
    tokens = _tokens(text)
    providers = set()
    for _labels, body in _blocks(tokens, "terraform", 0):
        for block in _nested_blocks(body, _REQUIRED_PROVIDERS_BLOCK):
            providers.update(
                name for name, (kind, _value) in _attributes(block).items() if kind in (_OPEN, _STR)
            )
    for block_name, labels in _IMPLYING_BLOCKS:
        for found in _headers_at_any_depth(tokens, block_name, labels):
            providers.add(found[0].split(_TYPE_PREFIX_SEPARATOR)[0])
    providers.discard(_BUILTIN_PROVIDER_PREFIX)
    return providers


def _local_module_sources_in(text: str) -> list:
    sources = []
    for _labels, body in _blocks(_tokens(text), _MODULE_BLOCK, 1):
        source = _attributes(body).get(_SOURCE_ATTRIBUTE)
        if source is not None and source[0] == _STR and source[1].startswith(_LOCAL_SOURCE_PREFIXES):
            sources.append(source[1])
    return sources


def _declared_providers(root: pathlib.Path) -> list:
    unread = sorted(root.glob(_TF_JSON_GLOB))
    if unread:
        raise AssertionError(
            f"{root} carries {[p.name for p in unread]}: the mock-provider pin reads HCL only, "
            "so a provider or module call declared in JSON syntax would go unseen; write it as .tf"
        )
    providers = set()
    for tf_file in sorted(root.glob(_TF_FILE_GLOB)):
        providers.update(_providers_in(tf_file.read_text()))
    return sorted(providers)


def _needed_providers(root: pathlib.Path, seen=None) -> list:
    """What the root declares, plus what every local module it calls declares."""
    root = root.resolve()
    seen = set() if seen is None else seen
    if root in seen:
        return []
    seen.add(root)
    providers = set(_declared_providers(root))
    for tf_file in sorted(root.glob(_TF_FILE_GLOB)):
        for source in _local_module_sources_in(tf_file.read_text()):
            child = (root / source).resolve()
            if child.is_dir():
                providers.update(_needed_providers(child, seen))
    return sorted(providers)


def _json_bodies(value) -> list:
    """The bodies of a labelled JSON block: `{"a": body}` or `[{"a": body}]`,
    each body itself one object or a list of them."""
    entries = value.values() if isinstance(value, dict) else []
    if isinstance(value, list):
        entries = [body for element in value if isinstance(element, dict) for body in element.values()]
    bodies = []
    for entry in entries:
        bodies.extend(entry if isinstance(entry, list) else [entry])
    return [body for body in bodies if isinstance(body, dict)]


def _json_unlabelled(value) -> list:
    """An unlabelled JSON block: one object or a list of them."""
    entries = value if isinstance(value, list) else [value]
    return [entry for entry in entries if isinstance(entry, dict)]


def _json_runs(text: str) -> list:
    return _json_bodies(json.loads(text).get(_JSON_RUN_KEY, {})) if text.strip() else []


def _run_module_sources(text: str, name: str) -> list:
    """Local module sources a test file's run blocks load, relative to the root."""
    if name.endswith(_TEST_FILE_JSON_SUFFIX):
        sources = [
            module.get(_JSON_SOURCE_KEY)
            for run in _json_runs(text)
            for module in _json_unlabelled(run.get(_JSON_MODULE_KEY, []))
        ]
    else:
        sources = [
            _attributes(block).get(_SOURCE_ATTRIBUTE, (None, None))[1]
            for _labels, body in _blocks(_tokens(text), _RUN_BLOCK, 1)
            for block in _nested_blocks(body, _MODULE_BLOCK)
            if _attributes(block).get(_SOURCE_ATTRIBUTE, ("",))[0] == _STR
        ]
    return [source for source in sources if isinstance(source, str) and source.startswith(_LOCAL_SOURCE_PREFIXES)]


def _run_provider_routes(text: str, name: str) -> list:
    """Every `provider.alias` a run's `providers` map hands the module."""
    routes = []
    if name.endswith(_TEST_FILE_JSON_SUFFIX):
        for run in _json_runs(text):
            mapping = run.get(_JSON_PROVIDERS_KEY, {})
            routes.extend(v for v in mapping.values() if isinstance(v, str)) if isinstance(mapping, dict) else None
        return routes
    for _labels, body in _blocks(_tokens(text), _RUN_BLOCK, 1):
        flat = [token for token, _depth in body]
        for index, (token, depth) in enumerate(body):
            if depth == 1 and token == (_WORD, _PROVIDERS_ATTRIBUTE) and index + 2 < len(body) and body[index + 1][0][0] == _EQUALS and body[index + 2][0][0] == _OPEN:
                entries = [t for t, d in _body(flat, index + 2) if d == 1]
                position = 0
                while position + 2 < len(entries):
                    if entries[position][0] == _WORD and entries[position + 1][0] == _EQUALS and entries[position + 2][0] == _WORD:
                        route = entries[position + 2][1]
                        if position + 4 < len(entries) and entries[position + 3] == (_OTHER, _ALIAS_SEPARATOR) and entries[position + 4][0] == _WORD:
                            route += _ALIAS_SEPARATOR + entries[position + 4][1]
                            position += 5
                        else:
                            position += 3
                        routes.append(route)
                    else:
                        position += 1
    return routes


def _needed_by_test_file(root: pathlib.Path, path: pathlib.Path) -> list:
    """What the root needs, plus what the modules this file's runs load need."""
    providers = set(_needed_providers(root))
    for source in _run_module_sources(path.read_text(), path.name):
        child = (root / source).resolve()
        if child.is_dir():
            providers.update(_needed_providers(child))
    return sorted(providers)


def _mock_configurations_in_hcl(text: str) -> set:
    """Each `mock_provider` block as `name` or `name.alias`."""
    configurations = set()
    for labels, body in _blocks(_tokens(text), _MOCK_PROVIDER_BLOCK, 1):
        alias = _attributes(body).get(_ALIAS_ATTRIBUTE)
        configurations.add(labels[0] if alias is None else f"{labels[0]}{_ALIAS_SEPARATOR}{alias[1]}")
    return configurations


def _mock_configurations_in_json(text: str) -> set:
    mocks = json.loads(text).get(_MOCK_PROVIDER_BLOCK, {}) if text.strip() else {}
    configurations = set()
    for name, entries in (mocks.items() if isinstance(mocks, dict) else []):
        for entry in _json_unlabelled(entries):
            alias = entry.get(_ALIAS_ATTRIBUTE)
            configurations.add(name if alias is None else f"{name}{_ALIAS_SEPARATOR}{alias}")
    return configurations


def _unmocked(text: str, providers, name: str = _TEST_FILE_SUFFIXES[0]) -> list:
    """Providers with no default mock, then every configuration a run's
    `providers` map routes to that no mock block declares."""
    is_json = name.endswith(_TEST_FILE_JSON_SUFFIX)
    mocked = _mock_configurations_in_json(text) if is_json else _mock_configurations_in_hcl(text)
    missing = [provider for provider in providers if provider not in mocked]
    missing.extend(route for route in _run_provider_routes(text, name) if route not in mocked and route not in missing)
    return missing


class TerraformModuleTestsWiringTest(unittest.TestCase):
    def test_at_least_one_module_carries_a_suite(self):
        self.assertTrue(_suites(), f"no {_TESTS_DIR}/*{_TEST_FILE_SUFFIXES[0]} under {_SUITE_PARENTS}; this test pins their wiring")

    def test_no_test_file_sits_where_the_loop_does_not_look(self):
        self.assertEqual(
            _unreached_test_files(_REPO_ROOT, _SUITE_PARENTS),
            [],
            f"these test files are not in, or beside, a {_TESTS_DIR}/ directory under {[p.name for p in _SUITE_PARENTS]}, the only places `{_GATE_COMMAND}` runs `{_TEST_COMMAND}`, so they never run",
        )

    def test_the_makefile_target_reaches_every_module(self):
        makefile = _MAKEFILE.read_text()
        self.assertRegex(
            makefile,
            _RECIPE_LINE,
            f"the Makefile has no `{_TARGET}:` recipe with a `## description`; `make help` is the only place a contributor finds it",
        )
        body = _recipe_body(makefile, _TARGET)
        self.assertIn(
            _LOOP_GLOB,
            body,
            f"`{_TARGET}` must loop over every terraform/modules/*/ and terraform/examples/*/ rather than name directories: a new one's tests/ is otherwise a suite nothing runs",
        )
        self.assertIn(_TEST_COMMAND, body, f"`{_TARGET}`'s recipe does not run `{_TEST_COMMAND}`")

    def test_make_verify_runs_the_target(self):
        self.assertIn(
            _VERIFY_LINE,
            _recipe_body(_MAKEFILE.read_text(), _VERIFY_TARGET),
            f"`make {_VERIFY_TARGET}` says it runs everything a pull request must pass offline; `{_TARGET}` is one of them",
        )

    def test_the_validate_job_runs_the_target_unconditionally(self):
        gate = [s for s in _steps() if _GATE_COMMAND in _run_lines(s)]
        self.assertEqual(
            len(gate),
            1,
            f"the `{_JOB_ID}` job in validate.yml must run `{_GATE_COMMAND}` in exactly one step",
        )
        self.assertNotIn("if", gate[0], f"the `{_GATE_COMMAND}` step must not carry an `if:`")

    def test_every_test_file_mocks_every_provider_its_root_needs(self):
        for root, files in _suites().items():
            self.assertTrue(_needed_providers(root), f"{root.name} needs no provider in any {_TF_FILE_GLOB}, its own or a called module's")
            for path in files:
                providers = _needed_by_test_file(root, path)
                with self.subTest(root=root.name, file=path.name):
                    self.assertEqual(
                        _unmocked(path.read_text(), providers, path.name),
                        [],
                        f"{path.relative_to(_REPO_ROOT)} has no unaliased mock_provider for every provider its root, or a module its runs load, needs ({providers}); a read the file forgets to override would reach a real API from CI",
                    )


class TerraformModuleTestsHelpersTest(unittest.TestCase):
    """The judgements above, on the shapes they exist to catch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_providers_are_read_from_any_tf_file_and_only_inside_the_block(self):
        # A composition's shape: a provider configured before the terraform
        # block (which implies `helm`), with a URL (`//`) and a template
        # holding quotes in its strings; providers.tf, no versions.tf; a
        # `kubernetes = {` attribute inside a provider block after it; the
        # version-only shorthand; a resource implying `null`.
        (self.root / "providers.tf").write_text(
            'provider "helm" {\n  kubernetes = {\n    host  = "https://x.example/#frag"\n'
            '    token = "${var.a == "b" ? "c" : "d"} /* not a comment */"\n  }\n}\n\n'
            "terraform {\n  required_providers {\n    google = {\n      source = \"hashicorp/google\"\n    }\n"
            "    google-beta = {\n      source = \"hashicorp/google-beta\"\n    }\n"
            '    random = ">= 3.5"\n  }\n}\n'
        )
        (self.root / "main.tf").write_text("resource \"null_resource\" \"x\" {}\n")
        self.assertEqual(_declared_providers(self.root), ["google", "google-beta", "helm", "null", "random"])

    def test_an_undeclared_resource_data_or_provider_block_implies_its_provider(self):
        # A check block's scoped data source counts too; the builtin
        # terraform provider (terraform_data) never does.
        (self.root / "main.tf").write_text(
            'data "http" "x" {\n  url = "https://x"\n}\nresource "google_project_iam_member" "y" {}\n'
            'provider "tls" {}\nlocals {\n  z = 1\n}\nresource "terraform_data" "t" {}\n'
            'check "health" {\n  data "dns_a_record_set" "probe" {\n    host = "x"\n  }\n  assert {\n    condition     = true\n    error_message = "x"\n  }\n}\n'
        )
        self.assertEqual(_declared_providers(self.root), ["dns", "google", "http", "tls"])

    def test_a_root_declaring_nothing_reads_as_no_providers(self):
        (self.root / "main.tf").write_text("locals {\n  a = {\n    b = {}\n  }\n}\n")
        self.assertEqual(_declared_providers(self.root), [])

    def test_a_brace_or_comment_opener_in_a_comment_string_or_heredoc_is_not_syntax(self):
        (self.root / "versions.tf").write_text(
            "terraform {\n  required_providers {\n    # the {google} entry below }\n    google = {\n"
            "      source  = \"hashicorp/google\"\n      version = \"} // not a comment\"\n    }\n    /* } */\n"
            "    http = {\n      source = \"hashicorp/http\"\n    }\n  }\n}\n\n"
            'variable "v" {\n  description = <<-EOT\n    A "quote and a } and a # and https://x\n  EOT\n}\n'
            'variable "w" {\n  default = "{"\n}\n'
        )
        self.assertEqual(_declared_providers(self.root), ["google", "http"])

    def test_a_root_with_a_tf_json_file_is_refused_not_half_read(self):
        (self.root / "versions.tf.json").write_text('{"terraform": {"required_providers": {"http": {}}}}')
        with self.assertRaises(AssertionError):
            _declared_providers(self.root)

    def test_a_called_local_module_adds_the_providers_it_declares(self):
        # A composition that declares google and calls a module requiring
        # http, with no providers map: the child gets a default http provider,
        # so the composition's tests must mock it too. The root file wins for
        # the declared set, the union for the needed set; a module calling
        # itself does not recurse forever; a remote source is ignored.
        composition = self.root / "examples" / "c"
        module = self.root / "modules" / "m"
        for directory in (composition, module):
            directory.mkdir(parents=True)
        (composition / "providers.tf").write_text(
            "terraform {\n  required_providers {\n    google = {\n      source = \"hashicorp/google\"\n    }\n  }\n}\n"
        )
        (composition / "main.tf").write_text(
            'module "m" {\n  source = "../../modules/m"\n}\n'
            'module "remote" {\n  source = "git::https://example.com/x.git//m"\n}\n'
        )
        (module / "versions.tf").write_text(
            "terraform {\n  required_providers {\n    http = {\n      source = \"hashicorp/http\"\n    }\n  }\n}\n"
        )
        (module / "main.tf").write_text('module "self" {\n  source = "./"\n}\n')
        self.assertEqual(_declared_providers(composition), ["google"])
        self.assertEqual(_needed_providers(composition), ["google", "http"])

    def test_a_module_a_run_block_loads_adds_its_providers_for_that_file(self):
        root = self.root / "terraform" / "modules" / "m"
        setup = root / _TESTS_DIR / "setup"
        setup.mkdir(parents=True)
        (root / "versions.tf").write_text(
            "terraform {\n  required_providers {\n    google = {\n      source = \"hashicorp/google\"\n    }\n  }\n}\n"
        )
        (setup / "main.tf").write_text('data "http" "seed" {\n  url = "https://x"\n}\n')
        hcl = root / _TESTS_DIR / "a.tftest.hcl"
        hcl.write_text('mock_provider "google" {}\n\nrun "seed" {\n  module {\n    source = "./tests/setup"\n  }\n}\n')
        plain = root / _TESTS_DIR / "b.tftest.hcl"
        plain.write_text('mock_provider "google" {}\n\nrun "x" {\n  command = plan\n}\n')
        as_json = root / _TESTS_DIR / "c.tftest.json"
        as_json.write_text('{"mock_provider": {"google": {}}, "run": {"seed": {"module": {"source": "./tests/setup"}}}}')
        # The array forms HCL-JSON admits: a list of {label: body} runs, and
        # a list of module objects.
        as_array = root / _TESTS_DIR / "d.tftest.json"
        as_array.write_text('{"mock_provider": {"google": {}}, "run": [{"seed": {"module": [{"source": "./tests/setup"}]}}]}')
        self.assertEqual(_needed_providers(root), ["google"])
        self.assertEqual(_needed_by_test_file(root, hcl), ["google", "http"])
        self.assertEqual(_needed_by_test_file(root, plain), ["google"])
        self.assertEqual(_needed_by_test_file(root, as_json), ["google", "http"])
        self.assertEqual(_needed_by_test_file(root, as_array), ["google", "http"])
        self.assertEqual(_unmocked(hcl.read_text(), _needed_by_test_file(root, hcl)), ["http"])
        self.assertEqual(_unmocked(as_json.read_text(), _needed_by_test_file(root, as_json), as_json.name), ["http"])
        self.assertEqual(_unmocked(as_array.read_text(), _needed_by_test_file(root, as_array), as_array.name), ["http"])

    def test_a_run_routing_a_provider_to_a_live_configuration_is_reported(self):
        text = (
            'mock_provider "google" {}\nmock_provider "http" {}\nprovider "http" {\n  alias = "live"\n}\n'
            'run "x" {\n  providers = {\n    google = google\n    http   = http.live\n  }\n  command = plan\n}\n'
        )
        self.assertEqual(_unmocked(text, ["google", "http"]), ["http.live"])
        mocked_alias = text.replace('provider "http" {\n  alias = "live"\n}', 'mock_provider "http" {\n  alias = "live"\n}')
        self.assertEqual(_unmocked(mocked_alias, ["google", "http"]), [])
        as_json = '{"mock_provider": {"google": {}, "http": [{}, {"alias": "live"}]}, "run": {"x": {"providers": {"http": "http.live"}}}}'
        self.assertEqual(_unmocked(as_json, ["google", "http"], "x.tftest.json"), [])
        routed_live = '{"mock_provider": {"google": {}, "http": {}}, "run": {"x": {"providers": {"http": "http.live"}}}}'
        self.assertEqual(_unmocked(routed_live, ["google", "http"], "x.tftest.json"), ["http.live"])

    def test_a_mock_in_a_comment_does_not_count(self):
        text = '# mock_provider "http" {}\nmock_provider "google" {}\n\nrun "x" {\n  command = plan\n}\n'
        self.assertEqual(_unmocked(text, ["google", "http"]), ["http"])
        blocked = '/*\nmock_provider "http" {}\n*/\nmock_provider "google" {}\n'
        self.assertEqual(_unmocked(blocked, ["google", "http"]), ["http"])
        slashed = '// mock_provider "http" {}\nmock_provider "google" {}\n'
        self.assertEqual(_unmocked(slashed, ["google", "http"]), ["http"])

    def test_an_aliased_mock_does_not_mock_the_default_provider(self):
        text = 'mock_provider "google" {\n  alias = "offline"\n}\nmock_provider "http" {\n}\n'
        self.assertEqual(_unmocked(text, ["google", "http"]), ["google"])
        both = 'mock_provider "google" {\n  alias = "offline"\n}\nmock_provider "google" {}\n'
        self.assertEqual(_unmocked(both, ["google"]), [])

    def test_an_indented_mock_block_counts(self):
        text = '  mock_provider "google-beta" {\n  }\n'
        self.assertEqual(_unmocked(text, ["google-beta"]), [])

    def test_a_json_test_file_is_read_as_json(self):
        text = '{"mock_provider": {"google": {}, "http": [{"alias": "x"}, {}]}, "run": {"x": {"command": "plan"}}}'
        self.assertEqual(_unmocked(text, ["google", "http"], "b.tftest.json"), [])
        aliased = '{"mock_provider": {"google": {"alias": "x"}}}'
        self.assertEqual(_unmocked(aliased, ["google"], "b.tftest.json"), ["google"])
        self.assertEqual(_unmocked('{"run": {"x": {}}}', ["google"], "b.tftest.json"), ["google"])
        self.assertEqual(_unmocked("", ["google"], "b.tftest.json"), ["google"])

    def test_a_test_file_beside_tests_runs_and_one_without_tests_does_not(self):
        parents = (self.root / "terraform" / "modules",)
        with_tests = self.root / "terraform" / "modules" / "m"
        (with_tests / _TESTS_DIR).mkdir(parents=True)
        (with_tests / _TESTS_DIR / "a.tftest.hcl").write_text("")
        (with_tests / _TESTS_DIR / "b.tftest.json").write_text("{}")
        (with_tests / _TESTS_DIR / "notes.md").write_text("")
        (with_tests / "c.tftest.hcl").write_text("")
        without_tests = self.root / "terraform" / "modules" / "n"
        without_tests.mkdir()
        (without_tests / "d.tftest.hcl").write_text("")
        self.assertEqual(
            [p.name for p in _suites(parents)[with_tests]],
            ["c.tftest.hcl", "a.tftest.hcl", "b.tftest.json"],
        )
        self.assertEqual(_unreached_test_files(self.root, parents), [without_tests / "d.tftest.hcl"])

    def test_a_test_file_outside_the_reached_set_is_reported(self):
        # A dot-named module directory is one the shell glob skips, so its
        # suite is unreached however it is laid out.
        parents = (self.root / "terraform" / "modules",)
        reached = self.root / "terraform" / "modules" / "m" / "tests"
        stray = self.root / "bench" / "tf" / "fleet" / "tests"
        hidden = self.root / "terraform" / "modules" / ".archived" / "tests"
        ignored = self.root / "terraform" / "modules" / "m" / ".terraform" / "tests"
        worktree = self.root / ".claude" / "worktrees" / "pr-1" / "terraform" / "modules" / "m" / "tests"
        for directory in (reached, stray, hidden, ignored, worktree):
            directory.mkdir(parents=True)
            (directory / "a.tftest.hcl").write_text("")
        self.assertEqual(
            _unreached_test_files(self.root, parents),
            sorted([hidden / "a.tftest.hcl", stray / "a.tftest.hcl"]),
        )
        self.assertNotIn(hidden.parent, _suites(parents))

    def test_a_checkout_under_an_ignored_name_is_still_walked(self):
        # A review worktree lives under .claude/; the filter applies below
        # the repository root, not to the root's own ancestors.
        repo = self.root / ".claude" / "worktrees" / "pr-1"
        parents = (repo / "terraform" / "modules",)
        stray = repo / "bench" / "tf" / "fleet" / "tests"
        stray.mkdir(parents=True)
        (stray / "a.tftest.hcl").write_text("")
        (repo / "terraform" / "modules").mkdir(parents=True)
        self.assertEqual(_unreached_test_files(repo, parents), [stray / "a.tftest.hcl"])

    def test_the_recipe_body_is_what_is_read_not_the_help_line(self):
        makefile = "x: ## run terraform test\n\tterraform init\n\ntf: ## nothing\n\tterraform test\n"
        self.assertNotIn(_TEST_COMMAND, _recipe_body(makefile, "x"))
        self.assertIn(_TEST_COMMAND, _recipe_body(makefile, "tf"))

    def test_a_commented_out_recipe_line_does_not_count(self):
        # make's column-0 comment, the shell's comment after the tab, and a
        # backslash continuation carried by either.
        makefile = (
            "v: ## verify\n\t@echo a\n#\t$(MAKE) --no-print-directory terraform-test\n\t#@echo terraform test\n\n"
            "t: ## t\n#\t@for dir in x; do \\\n\t  terraform test; \\\n\tdone\n\t@echo done\n"
        )
        self.assertNotIn("terraform-test", _recipe_body(makefile, "v"))
        self.assertNotIn(_TEST_COMMAND, _recipe_body(makefile, "v"))
        self.assertNotIn(_TEST_COMMAND, _recipe_body(makefile, "t"))
        self.assertIn("@echo done", _recipe_body(makefile, "t"))
        self.assertIn(_VERIFY_LINE, _recipe_body(_MAKEFILE.read_text(), _VERIFY_TARGET))
        # An indented shell comment inside a loop body is dropped; a shell
        # comment's trailing backslash carries nothing, so the line after it
        # still counts.
        indented = "t: ## t\n\t@for dir in x; do \\\n\t    # (cd $$dir && terraform test) || failed=1; \\\n\t  done\n"
        self.assertNotIn(_TEST_COMMAND, _recipe_body(indented, "t"))
        carried = "v: ## v\n\t#@echo x \\\n\t$(MAKE) --no-print-directory terraform-test\n"
        self.assertIn(_VERIFY_LINE, _recipe_body(carried, "v"))
        # The prefixes make strips before the shell sees the line, and a
        # make comment indented with spaces rather than a tab.
        for prefix in ("@#", "-#", "+#", "@ #", "@-#"):
            with self.subTest(prefix=prefix):
                silenced = f"v: ## v\n\t{prefix}echo x; $(MAKE) --no-print-directory terraform-test\n\t@echo done\n"
                self.assertNotIn(_VERIFY_LINE, _recipe_body(silenced, "v"))
                self.assertIn("@echo done", _recipe_body(silenced, "v"))
        spaced = "v: ## v\n   # $(MAKE) --no-print-directory terraform-test\n\t@echo done\n"
        self.assertNotIn(_VERIFY_LINE, _recipe_body(spaced, "v"))

    def test_the_rule_is_found_by_its_own_line_not_a_mention(self):
        makefile = (
            "# run `make verify:` first\npre-verify: ## p\n\t@echo pre\n\n"
            "verify: ## v\n\t$(MAKE) --no-print-directory terraform-test\n\nother: ## o\n\t@echo verify:\n"
        )
        self.assertIn(_VERIFY_LINE, _recipe_body(makefile, "verify"))
        self.assertNotIn("pre", _recipe_body(makefile, "verify"))
        with self.assertRaises(AssertionError):
            _recipe("x: ## x\n\t@echo\n", "verify")


if __name__ == "__main__":
    unittest.main()
