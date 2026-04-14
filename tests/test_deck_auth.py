"""Test deck auth middleware."""
import sys, os, unittest

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

os.environ["AGT_DECK_TOKEN"] = "test_token_12345"

from fastapi.testclient import TestClient
from agt_deck.main import app


class TestDeckAuth(unittest.TestCase):
    pytestmark = pytest.mark.agt_tripwire_exempt

    def setUp(self):
        self.client = TestClient(app)

    def test_missing_token_returns_401(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_returns_401(self):
        resp = self.client.get("/?t=wrong_token")
        self.assertEqual(resp.status_code, 401)

    def test_correct_token_returns_200(self):
        resp = self.client.get("/?t=test_token_12345")
        self.assertEqual(resp.status_code, 200)

    def test_static_no_auth_needed(self):
        resp = self.client.get("/static/app.css")
        # May be 200 or 404 depending on file, but NOT 401
        self.assertNotEqual(resp.status_code, 401)


if __name__ == '__main__':
    unittest.main()
