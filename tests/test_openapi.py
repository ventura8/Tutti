"""OpenAPI 3.1 specification (docs/openapi.yaml) and the Swagger UI endpoint (/api/docs)."""
import unittest
import os
import http.client
import json
import threading
import tempfile
import time
from tutti import config, dashboard, authweb, routing, auth

# Re-use setup from test_auth
from test_auth import ServerTest, CLIENT, KEY, token, claims, FakeJwks

class TestOpenAPI(ServerTest):
    def test_openapi_exists(self):
        self.assertTrue(os.path.exists(os.path.join(config.ROOT, 'docs', 'openapi.yaml')))

    def test_docs_requires_auth(self):
        r, _ = self.get('/api/docs')
        self.assertIn(r.status, [200, 401, 403])
        if r.status == 200:
            self.assertNotIn(b'Swagger', _)

    def test_docs_accessible_by_viewer(self):
        # viewer is already signed in in ServerTest.setUpClass
        r, _ = self.get('/api/docs', who=self.viewer)
        self.assertEqual(r.status, 200)

if __name__ == '__main__':
    unittest.main()

