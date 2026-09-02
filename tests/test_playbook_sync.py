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
# already sits. A copy that reappeared there would ship stale bytes unseen,
# so the identity test DISCOVERS copies instead of trusting this set — a
# copy that appears or vanishes must be added here, with the reason above.
EXPECTED_COPIES = {
    "MULTI-AGENT-PLAYBOOK.md",
    "skill/MULTI-AGENT-PLAYBOOK.md",
    "plugins/hermes/klatalk/MULTI-AGENT-PLAYBOOK.md",
}


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _found_copies():
    found = set()
    for dirpath, dirnames, names in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "node_modules", "__pycache__")]
        if "MULTI-AGENT-PLAYBOOK.md" in names:
            rel = os.path.relpath(
                os.path.join(dirpath, "MULTI-AGENT-PLAYBOOK.md"), ROOT)
            found.add(rel.replace(os.sep, "/"))
    return found


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
    # a // comment inside the chain is not part of the rendered string
    body = re.sub(r"(?m)^\s*//.*$", "", m.group(1))
    assert "\\" not in body, (
        "an escape sequence appeared in the JS hint — teach this test to "
        "unescape before comparing")
    grammar = re.sub(r'"[^"]*"', '""', body)
    assert re.fullmatch(r'\s*""(?:\s*\+\s*"")*\s*', grammar), (
        "PLATFORM_HINT must be string literals joined by + and nothing "
        "else — a stray comma or expression would render differently in JS "
        "than this test assumes")
    parts = re.findall(r'"([^"]*)"', body)
    assert parts, "PLATFORM_HINT is no longer a concatenation of literals"
    return "".join(parts)


def _serve_rules():
    """The working-room clause serve_prompt splices in (bin/klatalk) — the
    third host of the same rules, worded for a seat that can only run the
    CLI."""
    m = re.search(r'working = \("" if not others_ai else\n([\s\S]*?)\)\n',
                  _read("bin/klatalk"))
    assert m, "the serve working-room clause moved — teach this test"
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))


def _hint_rules():
    """Only the working-room clause of the gateway hint — the whole hint
    also says 'your owner' for unrelated reasons, and a keyword satisfied
    by unrelated prose pins nothing."""
    hint = _py_hint()
    open_, close = " In a working room (", " The full field guide is"
    assert open_ in hint and close in hint[hint.index(open_):], (
        "the gateway hint's working-room clause moved — teach this test "
        f"({open_!r} .. {close!r})")
    start = hint.index(open_)
    return hint[start:hint.index(close, start)]


# What every host must say, whatever its wording: the baton by exact
# nickname, a call is a cue (not authority), the ownerless stop, quorum
# over an unconfirmed seat, hearts for agreement, speak only to add.
WORKING_ROOM_RULES = (
    "exact roster nickname", "Next: <name>", "cue to", "five turns",
    "close on quorum", "agree with a heart",
    "new information, an attack, or a transformation",
)
# The baton-skip rule belongs to the gateways alone: their hint rides every
# turn, 1:1 rooms included, where "the only other member is your owner" can
# be true. serve's paragraph appears only beside another AI member, where
# that condition cannot hold — so it says nothing about skipping.
GATEWAY_ONLY_RULES = ("only other member is your owner",)


class TestPlaybookSync(unittest.TestCase):
    def test_serve_says_the_same_working_room_rules_as_the_gateways(self):
        # three hosts, one rule set — serve's prose is its own (it names the
        # CLI command and cannot point at a file), but no rule may go missing
        serve, hint = _serve_rules(), _hint_rules()
        for rule in WORKING_ROOM_RULES:
            self.assertIn(rule, serve, f"serve's paragraph lost: {rule}")
            self.assertIn(rule, hint, f"the gateway hint lost: {rule}")
        for rule in GATEWAY_ONLY_RULES:
            self.assertIn(rule, hint, f"the gateway hint lost: {rule}")
            self.assertNotIn(rule, serve, f"serve states a rule it cannot reach: {rule}")

    def test_every_shipped_playbook_copy_is_byte_identical(self):
        found = _found_copies()
        self.assertEqual(found, EXPECTED_COPIES,
                         "a playbook copy appeared or vanished — update "
                         "EXPECTED_COPIES and its comment, and say why")
        digests = {}
        for rel in sorted(found):
            with open(os.path.join(ROOT, rel), "rb") as f:
                digests[rel] = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(len(set(digests.values())), 1, digests)

    def test_both_gateway_hints_render_the_same_string(self):
        # two hosts, one rule: a seat's manners must not depend on its gateway
        self.assertEqual(_py_hint(), _js_hint())

    def test_the_skill_points_at_a_playbook_the_install_copies(self):
        if "MULTI-AGENT-PLAYBOOK.md" in _read("skill/SKILL.md"):
            self.assertRegex(
                _read("README.md"),
                r"(?m)^[^#\n]*\bcp[ \t]+skill/SKILL\.md[ \t]+"
                r"skill/MULTI-AGENT-PLAYBOOK\.md[ \t]+\S+",
                "SKILL.md points at the playbook but the README's install "
                "never copies it beside SKILL.md")


if __name__ == "__main__":
    unittest.main()
