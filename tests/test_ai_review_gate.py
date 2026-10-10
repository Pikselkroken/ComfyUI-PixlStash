"""The AI review gate: who may spend the review key.

``.github/workflows/ai-review.yml`` runs on ``pull_request_target``, so it holds
the OpenRouter key on pull requests from anybody. The script in its first step
is the only thing that decides whether PR-Agent runs: for the review owner
acting on a pull request they opened or on a contributor's fork, and for the
owner's comment commands, and for nobody else.

What is pinned is both directions: each refusal sits beside the nearest case
that is still allowed, so a gate inverted or widened fails here.

The script is run in Node as ``actions/github-script`` runs it, as the body of
an async function given ``context`` and ``core``, so this is skipped where
there is no ``node``. It is cut out of the workflow by its indentation, since
the standard library has no YAML parser.
"""

import json
import os
import pathlib
import shutil
import subprocess
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ai-review.yml"

OWNER = {"id": 1160606}
SOMEONE = {"id": 42}

DRIVER = """
const [script, events] = JSON.parse(require("fs").readFileSync(0, "utf8"));
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const gate = new AsyncFunction("context", "core", script);
(async () => {
    const results = {};
    for (const [name, context] of Object.entries(events)) {
        const out = {};
        await gate(context, { info() {}, setOutput: (key, value) => { out[key] = value; } });
        results[name] = out;
    }
    console.log(JSON.stringify(results));
})();
"""


def _gate_script() -> str:
    """The ``script:`` block of the gate step, dedented."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "script: |")
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = []
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


def _pull_request(author, sender, *, draft=False, fork=False):
    return {
        "eventName": "pull_request_target",
        "payload": {
            "sender": sender,
            "pull_request": {
                "draft": draft,
                "user": author,
                "head": {"repo": {"id": 2 if fork else 1}},
                "base": {"repo": {"id": 1}},
            },
        },
    }


def _comment(author, body, *, on_pull_request=True):
    return {
        "eventName": "issue_comment",
        "payload": {
            "issue": {"pull_request": {}} if on_pull_request else {},
            "comment": {"user": author, "body": body},
        },
    }


BOTH = {"review": "true", "improve": "true"}
NEITHER = {"review": "false", "improve": "false"}
REVIEW_ONLY = {"review": "true", "improve": "false"}
IMPROVE_ONLY = {"review": "false", "improve": "true"}

CASES = {
    "owner_opens_own": (_pull_request(OWNER, OWNER), BOTH),
    "owner_own_draft": (_pull_request(OWNER, OWNER, draft=True), NEITHER),
    "someone_pushes_to_owners": (_pull_request(OWNER, SOMEONE), NEITHER),
    "owner_pushes_to_fork": (_pull_request(SOMEONE, OWNER, fork=True), BOTH),
    "contributor_pushes_to_fork": (
        _pull_request(SOMEONE, SOMEONE, fork=True),
        NEITHER,
    ),
    "owner_pushes_to_others_same_repo": (_pull_request(SOMEONE, OWNER), NEITHER),
    # A deleted fork leaves no head repository: that counts as a fork head, and
    # still needs the owner to be the one acting.
    "owner_pushes_to_deleted_fork": (
        {
            "eventName": "pull_request_target",
            "payload": {
                "sender": OWNER,
                "pull_request": {
                    "draft": False,
                    "user": SOMEONE,
                    "head": {"repo": None},
                    "base": {"repo": {"id": 1}},
                },
            },
        },
        BOTH,
    ),
    "owner_review": (_comment(OWNER, "/review"), REVIEW_ONLY),
    "owner_improve": (_comment(OWNER, "  /improve --extra"), IMPROVE_ONLY),
    "owner_ask": (_comment(OWNER, "/ask why is this safe?"), REVIEW_ONLY),
    "owner_plain_comment": (_comment(OWNER, "looks good"), NEITHER),
    "someone_review": (_comment(SOMEONE, "/review"), NEITHER),
    "deleted_account_review": (_comment(None, "/review"), NEITHER),
    "owner_review_on_issue": (
        _comment(OWNER, "/review", on_pull_request=False),
        NEITHER,
    ),
}


@unittest.skipUnless(shutil.which("node"), "needs node")
class AiReviewGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        events = {name: event for name, (event, _expected) in CASES.items()}
        run = subprocess.run(
            ["node", "-e", DRIVER],
            input=json.dumps([_gate_script(), events]),
            env={**os.environ, "REVIEW_OWNER_ID": str(OWNER["id"])},
            capture_output=True,
            text=True,
            check=False,
        )
        if run.returncode:
            raise AssertionError(run.stderr)
        cls.results = json.loads(run.stdout)

    def test_each_event_gets_the_expected_decision(self):
        for name, (_event, expected) in CASES.items():
            with self.subTest(name):
                self.assertEqual(self.results[name], expected)

    def test_the_owner_id_in_the_workflow_is_the_one_tested(self):
        # The cases above mean nothing if the workflow gates on another account.
        self.assertIn(
            f'REVIEW_OWNER_ID: "{OWNER["id"]}"', WORKFLOW.read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
