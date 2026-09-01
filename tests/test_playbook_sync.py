"""Invariants the playbook wiring leans on, guarded nowhere else: the two
gateway plugins' PLATFORM_HINT must RENDER to the same string (python
adjacent-literal concatenation vs JS `+`), every shipped copy of
MULTI-AGENT-PLAYBOOK.md must be byte-identical, and the skill may only
point at a playbook the README's install actually copies. Text-only —
needs neither Hermes nor node."""
import ast
import hashlib
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The openclaw plugin dir carries no copy on purpose: its documented install
# is a full checkout at ~/.klatalk-agent/src, where the repo-root copy
# already sits.
COPIES = (
    "MULTI-AGENT-PLAYBOOK.md",
    "skill/MULTI-AGENT-PLAYBOOK.md",
    "plugins/hermes/klatalk/MULTI-AGENT-PLAYBOOK.md",
)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _py_hint():
    for node in ast.walk(ast.parse(_read("plugins/hermes/klatalk/adapter.py"))):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "PLATFORM_HINT"
                for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("PLATFORM_HINT not found in adapter.py")


def _js_hint():
    m = re.search(r"const PLATFORM_HINT =\n([\s\S]*?);\n",
                  _read("plugins/openclaw/klatalk/index.js"))
    assert m, "PLATFORM_HINT not found in index.js"
    body = m.group(1)
    assert "\\" not in body, (
        "an escape sequence appeared in the JS hint — teach this test to "
        "unescape before comparing")
    parts = re.findall(r'"([^"]*)"', body)
    assert parts, "PLATFORM_HINT is no longer a concatenation of literals"
    return "".join(parts)


class TestPlaybookSync(unittest.TestCase):
    def test_every_shipped_playbook_copy_is_byte_identical(self):
        digests = {}
        for rel in COPIES:
            with open(os.path.join(ROOT, rel), "rb") as f:
                digests[rel] = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(len(set(digests.values())), 1, digests)

    def test_both_gateway_hints_render_the_same_string(self):
        # two hosts, one rule: a seat's manners must not depend on its gateway
        self.assertEqual(_py_hint(), _js_hint())

    def test_the_skill_points_at_a_playbook_the_install_copies(self):
        if "MULTI-AGENT-PLAYBOOK.md" in _read("skill/SKILL.md"):
            self.assertIn(
                "skill/MULTI-AGENT-PLAYBOOK.md", _read("README.md"),
                "SKILL.md points at the playbook but the README's install "
                "never copies it beside SKILL.md")


if __name__ == "__main__":
    unittest.main()
