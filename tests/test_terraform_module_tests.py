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
too), and a `mock_provider` block, as a block and not a mention in a comment,
for every provider a root needs: what it declares in `required_providers` in
any of its `.tf` files (a composition declares them in providers.tf, a module
in versions.tf) and what every local module it calls declares, since
Terraform hands a child a default provider the root never named. Both test
file spellings Terraform loads, `.tftest.hcl` and `.tftest.json`, count. The
helpers that make those judgements have cases of their own below, on
fixtures, so the pin is known to fire on the shapes it exists for.
tests/test_shellcheck_gate_wiring.py pins a workflow step the same way.
"""

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
#: was written.
_RECIPE_LINE = re.compile(rf"^{_TARGET}: ## \S", re.MULTILINE)
_LOOP_GLOB = "for dir in terraform/modules/*/ terraform/examples/*/; do"
_TEST_COMMAND = "terraform test"
_VERIFY_TARGET = "verify"
_VERIFY_LINE = f"$(MAKE) --no-print-directory {_TARGET}"

#: Where a root declares its providers, and the entries inside that block
#: (`google-beta` included, hence the hyphen). The block is cut at its own
#: closing brace, so a `kubernetes = {` inside a later `provider "helm"`
#: block is not read as a provider.
_TF_FILE_GLOB = "*.tf"
_REQUIRED_PROVIDERS_OPEN = re.compile(r"^\s*required_providers\s*\{", re.MULTILINE)
_REQUIRED_PROVIDER = re.compile(r"^\s*([\w-]+)\s*=\s*\{", re.MULTILINE)
#: A module block calling a local directory, whose providers the root needs
#: too: Terraform gives such a child a default provider the root never
#: declared (full-install never declares `http`; the scope resolver it calls
#: requires it).
_LOCAL_MODULE_SOURCE = re.compile(r'^\s*source\s*=\s*"(\.\.?/[^"]*)"', re.MULTILINE)
#: The block a test file needs per provider: at line start, so the literal
#: surviving only in a comment (`# mock_provider "http" {}`) does not count.
_MOCK_PROVIDER = r'^\s*mock_provider "{provider}"\s*\{{'
#: Both spellings Terraform loads from tests/.
_TEST_FILE_SUFFIXES = (".tftest.hcl", ".tftest.json")
_TESTS_DIR = "tests"


def _is_test_file(path: pathlib.Path) -> bool:
    return path.name.endswith(_TEST_FILE_SUFFIXES)


def _suite_files(root: pathlib.Path) -> list:
    tests_dir = root / _TESTS_DIR
    return sorted(p for p in tests_dir.iterdir() if _is_test_file(p)) if tests_dir.is_dir() else []


def _suites(parents=_SUITE_PARENTS) -> dict:
    return {
        root: _suite_files(root)
        for parent in parents
        for root in sorted(parent.iterdir())
        if root.is_dir() and _suite_files(root)
    }


def _unreached_test_files(repo_root: pathlib.Path, parents) -> list:
    reached = {path for files in _suites(parents).values() for path in files}
    everywhere = {
        path
        for path in repo_root.rglob("*")
        if _is_test_file(path) and not _WALK_EXCLUDED_PARTS & set(path.parts)
    }
    return sorted(everywhere - reached)


def _recipe(makefile: str, target: str) -> str:
    recipe = makefile[makefile.index(f"{target}:"):]
    return recipe[: recipe.index("\n\n")]


def _block(text: str, start: int) -> str:
    """The text from the `{` at `start` to its matching `}`."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]


def _declared_providers(root: pathlib.Path) -> list:
    providers = set()
    for tf_file in sorted(root.glob(_TF_FILE_GLOB)):
        text = tf_file.read_text()
        for match in _REQUIRED_PROVIDERS_OPEN.finditer(text):
            providers.update(_REQUIRED_PROVIDER.findall(_block(text, match.end() - 1)))
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
        for source in _LOCAL_MODULE_SOURCE.findall(tf_file.read_text()):
            child = (root / source).resolve()
            if child.is_dir():
                providers.update(_needed_providers(child, seen))
    return sorted(providers)


def _unmocked(text: str, providers) -> list:
    return [
        provider
        for provider in providers
        if not re.search(_MOCK_PROVIDER.format(provider=provider), text, re.MULTILINE)
    ]


class TerraformModuleTestsWiringTest(unittest.TestCase):
    def test_at_least_one_module_carries_a_suite(self):
        self.assertTrue(_suites(), f"no {_TESTS_DIR}/*{_TEST_FILE_SUFFIXES[0]} under {_SUITE_PARENTS}; this test pins their wiring")

    def test_no_test_file_sits_where_the_loop_does_not_look(self):
        self.assertEqual(
            _unreached_test_files(_REPO_ROOT, _SUITE_PARENTS),
            [],
            f"these test files are outside {_TESTS_DIR}/ under {[p.name for p in _SUITE_PARENTS]}, so `{_GATE_COMMAND}` never runs them",
        )

    def test_the_makefile_target_reaches_every_module(self):
        makefile = _MAKEFILE.read_text()
        self.assertRegex(
            makefile,
            _RECIPE_LINE,
            f"the Makefile has no `{_TARGET}:` recipe with a `## description`; `make help` is the only place a contributor finds it",
        )
        recipe = _recipe(makefile, _TARGET)
        self.assertIn(
            _LOOP_GLOB,
            recipe,
            f"`{_TARGET}` must loop over every terraform/modules/*/ and terraform/examples/*/ rather than name directories: a new one's tests/ is otherwise a suite nothing runs",
        )
        self.assertIn(_TEST_COMMAND, recipe, f"`{_TARGET}` does not run `{_TEST_COMMAND}`")

    def test_make_verify_runs_the_target(self):
        self.assertIn(
            _VERIFY_LINE,
            _recipe(_MAKEFILE.read_text(), _VERIFY_TARGET),
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
            providers = _needed_providers(root)
            self.assertTrue(providers, f"{root.name} needs no provider in any {_TF_FILE_GLOB}, its own or a called module's")
            for path in files:
                with self.subTest(root=root.name, file=path.name):
                    self.assertEqual(
                        _unmocked(path.read_text(), providers),
                        [],
                        f"{path.relative_to(_REPO_ROOT)} does not mock every provider its root needs ({providers}); a read the file forgets to override would reach a real API from CI",
                    )


class TerraformModuleTestsHelpersTest(unittest.TestCase):
    """The three judgements above, on the shapes they exist to catch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_providers_are_read_from_any_tf_file_and_only_inside_the_block(self):
        # A composition's shape: providers.tf, no versions.tf, and a
        # `kubernetes = {` attribute inside a provider block after it.
        (self.root / "providers.tf").write_text(
            "terraform {\n  required_providers {\n    google = {\n      source = \"hashicorp/google\"\n    }\n"
            "    google-beta = {\n      source = \"hashicorp/google-beta\"\n    }\n  }\n}\n\n"
            "provider \"helm\" {\n  kubernetes = {\n    host = \"x\"\n  }\n}\n"
        )
        (self.root / "main.tf").write_text("resource \"null_resource\" \"x\" {}\n")
        self.assertEqual(_declared_providers(self.root), ["google", "google-beta"])

    def test_a_root_declaring_nothing_reads_as_no_providers(self):
        (self.root / "main.tf").write_text("locals {\n  a = {\n    b = {}\n  }\n}\n")
        self.assertEqual(_declared_providers(self.root), [])

    def test_a_called_local_module_adds_the_providers_it_declares(self):
        # A composition that declares google and calls a module requiring
        # http, with no providers map: the child gets a default http provider,
        # so the composition's tests must mock it too. The root file wins for
        # the declared set, the union for the needed set; a module calling
        # itself does not recurse forever.
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

    def test_a_json_test_file_counts_as_a_test_file(self):
        parents = (self.root / "terraform" / "modules",)
        suite = self.root / "terraform" / "modules" / "m" / "tests"
        suite.mkdir(parents=True)
        (suite / "a.tftest.hcl").write_text("")
        (suite / "b.tftest.json").write_text("{}")
        (suite / "notes.md").write_text("")
        stray = self.root / "terraform" / "modules" / "m" / "b.tftest.json"
        stray.write_text("{}")
        self.assertEqual(
            [p.name for p in _suites(parents)[self.root / "terraform" / "modules" / "m"]],
            ["a.tftest.hcl", "b.tftest.json"],
        )
        self.assertEqual(_unreached_test_files(self.root, parents), [stray])

    def test_a_mock_in_a_comment_does_not_count(self):
        text = '# mock_provider "http" {}\nmock_provider "google" {}\n\nrun "x" {\n  command = plan\n}\n'
        self.assertEqual(_unmocked(text, ["google", "http"]), ["http"])

    def test_an_indented_mock_block_counts(self):
        text = '  mock_provider "google-beta" {\n  }\n'
        self.assertEqual(_unmocked(text, ["google-beta"]), [])

    def test_a_test_file_outside_the_reached_set_is_reported(self):
        parents = (self.root / "terraform" / "modules",)
        reached = self.root / "terraform" / "modules" / "m" / "tests"
        stray = self.root / "bench" / "tf" / "fleet" / "tests"
        ignored = self.root / "terraform" / "modules" / "m" / ".terraform" / "tests"
        worktree = self.root / ".claude" / "worktrees" / "pr-1" / "terraform" / "modules" / "m" / "tests"
        for directory in (reached, stray, ignored, worktree):
            directory.mkdir(parents=True)
            (directory / "a.tftest.hcl").write_text("")
        self.assertEqual(
            _unreached_test_files(self.root, parents),
            [stray / "a.tftest.hcl"],
        )


if __name__ == "__main__":
    unittest.main()
