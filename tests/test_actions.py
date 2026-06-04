import unittest
from unittest.mock import patch

from core import actions


class SessionLaunchTests(unittest.TestCase):
    def test_builds_claude_resume_and_fork_commands(self):
        self.assertEqual(
            actions.session_cli_command("claude", "sid-123", "/tmp/project"),
            "cd /tmp/project && claude --resume sid-123",
        )
        self.assertEqual(
            actions.session_cli_command("claude", "sid-123", "/tmp/project", fork=True),
            "cd /tmp/project && claude --resume sid-123 --fork-session",
        )

    def test_builds_codex_resume_and_fork_commands(self):
        self.assertEqual(
            actions.session_cli_command("codex", "sid-123", "/tmp/project"),
            "cd /tmp/project && codex resume sid-123",
        )
        self.assertEqual(
            actions.session_cli_command("codex", "sid-123", "/tmp/project", fork=True),
            "cd /tmp/project && codex fork sid-123",
        )

    def test_launch_returns_manual_command_when_no_terminal_is_available(self):
        with patch.object(actions, "_terminal_command", return_value=None):
            result = actions.launch_session("codex", "sid-123", "/tmp/project")
        self.assertFalse(result["ok"])
        self.assertEqual(result["platform"], "codex")
        self.assertEqual(result["command"], "cd /tmp/project && codex resume sid-123")

    def test_launch_rejects_unsupported_platform(self):
        result = actions.launch_session("opencode", "sid-123", "/tmp/project")
        self.assertFalse(result["ok"])
        self.assertIn("cannot be resumed", result["error"])


if __name__ == "__main__":
    unittest.main()
