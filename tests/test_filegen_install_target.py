"""Test that file-gen libraries install into the interpreter we actually run on.

The auto-installer passed a hardcoded ``--python /opt/hermes/.venv/bin/python3``
to ``uv pip install``. No Hermes install has a venv at that path — the real one
is ``/opt/hermes/hermes-agent/.venv`` — so every attempt failed. It failed
*quietly*: the exception is caught and logged as a WARNING, and the bot simply
answers as if the document feature does not exist.

Observed on two production VPSes on 2026-08-18: 117 failed attempts on one,
and on both, python-pptx / openpyxl / weasyprint / pypdf were missing, so
customers asking for PowerPoint, Excel or PDF got nothing.

A hardcoded interpreter path cannot be right for every deployment, so this
test pins the invariant instead of the path: install into ``sys.executable``,
the interpreter the gateway is running under.
"""

import ast
import os
import re
import unittest

_ADAPTER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter.py"
)


def _source() -> str:
    with open(_ADAPTER, encoding="utf-8") as f:
        return f.read()


class TestFileGenInstallTarget(unittest.TestCase):
    def test_installs_into_the_running_interpreter(self):
        m = re.search(
            r'\["uv",\s*"pip",\s*"install",\s*"--python",\s*([^\]]+?)\]\s*\+\s*needed',
            _source(),
        )
        self.assertIsNotNone(m, "could not find the uv pip install call")
        target = m.group(1).strip()
        self.assertEqual(
            target, "sys.executable",
            f"file-gen libs install into {target}; a hardcoded interpreter path "
            f"is wrong on any host whose venv lives elsewhere, and the failure "
            f"is only a WARNING so nobody notices until a customer asks for a file",
        )

    def test_no_hardcoded_venv_path_remains(self):
        # assertFalse, not assertNotIn: the container here is the whole
        # 500 KB adapter source and assertNotIn would dump all of it into the
        # failure message, burying the one line that matters.
        self.assertFalse(
            "/opt/hermes/.venv" in _source(),
            "hardcoded venv path is back; that directory does not exist on a "
            "standard Hermes install",
        )

    def test_sys_is_imported_where_it_is_used(self):
        """sys.executable at runtime needs sys in scope — NameError otherwise."""
        tree = ast.parse(_source())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            src = ast.get_source_segment(_source(), node) or ""
            if "sys.executable" not in src:
                continue
            imports_sys = any(
                isinstance(n, ast.Import) and any(a.name == "sys" for a in n.names)
                for n in ast.walk(node)
            )
            module_level = re.search(r"^import sys$", _source(), re.MULTILINE)
            self.assertTrue(
                imports_sys or module_level,
                f"{node.name}() uses sys.executable but sys is not imported "
                f"in the function or at module level",
            )


if __name__ == "__main__":
    unittest.main()
