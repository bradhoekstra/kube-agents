"""Every Terraform module test suite runs, against mocks, on every pull request.

A `terraform/modules/<module>/tests/*.tftest.hcl` file runs only if the
`terraform-test` Makefile target's loop reaches its module and the `validate`
job in validate.yml runs the target; a suite that neither reaches is a set of
cases that passes by never running, the trap AGENTS.md "Where Tests Go" names
for `PYTHON_TEST_DIRS`. And a suite that mocks one provider but not another
would reach a real API from CI on the first read nobody overrode. This pins
the loop, the step, and a `mock_provider` block for every provider a module
declares, in every one of its test files. tests/test_shellcheck_gate_wiring.py
pins a workflow step the same way.
"""

import pathlib
import re
import unittest

import yaml

try:
    from tests.test_shellcheck_gate_wiring import _JOB_ID, _run_lines
except ImportError:  # run from inside tests/
    from test_shellcheck_gate_wiring import _JOB_ID, _run_lines

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MODULES_DIR = _REPO_ROOT / "terraform" / "modules"
_MAKEFILE = _REPO_ROOT / "Makefile"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "validate.yml"

#: `_JOB_ID` and `_run_lines` are the shellcheck wiring test's: the same job
#: on main's required-status-checks list, read the same way.
_TARGET = "terraform-test"
_GATE_COMMAND = f"make {_TARGET}"

#: The recipe line that lists the target in `make help`, and the loop that
#: reaches every module rather than naming the ones that had tests when it
#: was written.
_RECIPE_LINE = re.compile(rf"^{_TARGET}: ## \S", re.MULTILINE)
_LOOP_GLOB = "for dir in terraform/modules/*/; do"
_TEST_COMMAND = "terraform test"

#: A provider a module declares (`google-beta` included, hence the hyphen),
#: and the block a test file needs for it.
_REQUIRED_PROVIDER = re.compile(r"^\s*([\w-]+) = \{\s*$", re.MULTILINE)
_MOCK_PROVIDER = 'mock_provider "{provider}"'
_TEST_FILE_GLOB = "tests/*.tftest.hcl"
_VERSIONS_FILE = "versions.tf"


def _suites() -> dict:
    return {
        module: sorted(module.glob(_TEST_FILE_GLOB))
        for module in sorted(_MODULES_DIR.iterdir())
        if any(module.glob(_TEST_FILE_GLOB))
    }


def _declared_providers(module: pathlib.Path) -> list:
    text = (module / _VERSIONS_FILE).read_text()
    block = text[text.index("required_providers"):]
    return _REQUIRED_PROVIDER.findall(block)


class TerraformModuleTestsWiringTest(unittest.TestCase):
    def test_at_least_one_module_carries_a_suite(self):
        self.assertTrue(_suites(), f"no {_TEST_FILE_GLOB} under {_MODULES_DIR}; this test pins their wiring")

    def test_the_makefile_target_reaches_every_module(self):
        makefile = _MAKEFILE.read_text()
        self.assertRegex(
            makefile,
            _RECIPE_LINE,
            f"the Makefile has no `{_TARGET}:` recipe with a `## description`; `make help` is the only place a contributor finds it",
        )
        recipe = makefile[makefile.index(f"{_TARGET}:"):]
        recipe = recipe[: recipe.index("\n\n")]
        self.assertIn(
            _LOOP_GLOB,
            recipe,
            f"`{_TARGET}` must loop over every terraform/modules/*/ rather than name modules: a new module's tests/ is otherwise a suite nothing runs",
        )
        self.assertIn(_TEST_COMMAND, recipe, f"`{_TARGET}` does not run `{_TEST_COMMAND}`")

    def test_the_validate_job_runs_the_target_unconditionally(self):
        steps = yaml.safe_load(_WORKFLOW.read_text())["jobs"][_JOB_ID]["steps"]
        gate = [s for s in steps if _GATE_COMMAND in _run_lines(s)]
        self.assertEqual(
            len(gate),
            1,
            f"the `{_JOB_ID}` job in {_WORKFLOW.name} must run `{_GATE_COMMAND}` in exactly one step",
        )
        self.assertNotIn("if", gate[0], f"the `{_GATE_COMMAND}` step must not carry an `if:`")

    def test_every_test_file_mocks_every_provider_its_module_declares(self):
        for module, files in _suites().items():
            providers = _declared_providers(module)
            self.assertTrue(providers, f"{module.name}/{_VERSIONS_FILE} declares no provider")
            for path in files:
                text = path.read_text()
                for provider in providers:
                    with self.subTest(module=module.name, file=path.name, provider=provider):
                        self.assertIn(
                            _MOCK_PROVIDER.format(provider=provider),
                            text,
                            f"{path.relative_to(_REPO_ROOT)} does not mock the `{provider}` provider its module declares; a read the file forgets to override would reach a real API from CI",
                        )


if __name__ == "__main__":
    unittest.main()
