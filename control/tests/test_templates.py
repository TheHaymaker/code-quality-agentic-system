"""Every `cq ...` command line that a skill or an agent file shows must be a
command the CLI accepts, and the shipped templates must be well formed."""
import contextlib
import io
import re
import shlex
import unittest
from pathlib import Path

from cq import cli

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
SAMPLES = {"<S>": "MAJOR", "<C>": "correctness", "<n>": "2", "<N>": "2"}


def command_lines(text):
    """Commands in indented code blocks, and whole commands in backticks."""
    joined = re.sub(r"\\\n\s*", " ", text)
    found = []
    for line in joined.splitlines():
        stripped = line.strip()
        if line.startswith("    ") and stripped.startswith("cq "):
            found.append(stripped)
    for inline in re.findall(r"`(cq [^`]+)`", joined):
        if "<run" in inline or inline.startswith("cq run start"):
            found.append(inline)
    return found


def to_argv(command):
    command = command.split("  #")[0]
    command = re.sub(r"\[[^\]]*\]", "", command)                       # optional parts
    command = re.sub(r"\s--min-tier N\b", " --min-tier 2", command)
    for token, value in SAMPLES.items():
        command = command.replace(token, value)
    command = re.sub(r"<[^>]+>", "x", command)
    return shlex.split(command)[1:]


class Templates(unittest.TestCase):
    def test_every_command_in_skills_and_agents_parses(self):
        parser = cli.build_parser()
        checked = 0
        for path in sorted((TEMPLATES / "claude").rglob("*.md")):
            for command in command_lines(path.read_text()):
                argv = to_argv(command)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    try:
                        parser.parse_args(argv)
                    except SystemExit:
                        self.fail("%s: `%s` is not accepted by the CLI: %s" % (path.name, command, err.getvalue().strip().splitlines()[-1]))
                checked += 1
        self.assertGreater(checked, 30)

    def test_skills_and_agents_have_name_and_description(self):
        files = sorted((TEMPLATES / "claude").rglob("*.md"))
        self.assertEqual(len(files), 9)
        for path in files:
            text = path.read_text()
            self.assertTrue(text.startswith("---\n"), path)
            front = text.split("---\n")[1]
            self.assertRegex(front, r"(?m)^name: [a-z][a-z-]+$", path)
            self.assertRegex(front, r"(?m)^description: .{40,}$", path)
            expected = path.parent.name if path.name == "SKILL.md" else path.stem
            self.assertIn("name: %s\n" % expected, front)

    def test_no_skill_tells_an_agent_to_run_a_human_command(self):
        # Commands in code blocks are instructions to run. (Prose may name the human commands to forbid them.)
        for path in sorted((TEMPLATES / "claude").rglob("*.md")):
            for line in path.read_text().splitlines():
                command = line.strip()
                if not (line.startswith("    ") and command.startswith("cq ")):
                    continue
                self.assertNotRegex(command, r"^cq (approve|decide|tier lower|run close|fix extend)\b", path)
                self.assertNotIn("--new", command, path)


if __name__ == "__main__":
    unittest.main()
