"""Finding #12 — /cure command auto-detect LAN host tests.

Tests for _detect_deck_host():
  1. AGT_DECK_HOST env override respected (+ whitespace stripping)
  2. Socket error fallback to 127.0.0.1
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestDetectDeckHost(unittest.TestCase):
    """Verify _detect_deck_host() priority: env > socket > fallback."""

    def test_env_override(self):
        """AGT_DECK_HOST env var is respected verbatim when set."""
        with patch.dict(os.environ, {"AGT_DECK_HOST": "100.64.1.5"}):
            from telegram_bot import _detect_deck_host
            self.assertEqual(_detect_deck_host(), "100.64.1.5")

    def test_env_override_stripped(self):
        """Whitespace in AGT_DECK_HOST is stripped."""
        with patch.dict(os.environ, {"AGT_DECK_HOST": "  10.0.0.65  "}):
            from telegram_bot import _detect_deck_host
            self.assertEqual(_detect_deck_host(), "10.0.0.65")

    def test_fallback_on_socket_error(self):
        """When socket auto-detect raises, fallback to 127.0.0.1."""
        import socket as _socket

        class _BrokenSocket:
            def __init__(self, *a, **kw):
                pass
            def connect(self, *a, **kw):
                raise OSError("network unreachable")
            def close(self):
                pass
            def getsockname(self):
                return ("", 0)

        env = os.environ.copy()
        env.pop("AGT_DECK_HOST", None)
        with patch.dict(os.environ, env, clear=True):
            with patch.object(_socket, "socket", _BrokenSocket):
                with patch("telegram_bot.socket", _socket):
                    from telegram_bot import _detect_deck_host
                    self.assertEqual(_detect_deck_host(), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
