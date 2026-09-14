import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from fastapi import HTTPException


class AdminProductTests(unittest.TestCase):
    def test_clean_source_detects_store_and_rejects_bad_url(self):
        item = server._clean_source({
            "product": "Test ETB", "url": "https://www.walmart.com/ip/123",
            "game": "Pokemon", "msrp": "49.99", "priority": "high",
        })
        self.assertEqual(item["store"], "Walmart")
        self.assertEqual(item["msrp"], 49.99)
        with self.assertRaises(HTTPException):
            server._clean_source({"product": "Test", "url": "nope"})
        with self.assertRaises(HTTPException):
            server._clean_source({"product": "Test", "url": "https://example.com/1", "max_markup": -1})

    def test_write_sources_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "products.json"
            with patch.object(server, "SOURCES_FILE", path):
                server._write_sources([{"product": "Test", "url": "https://example.com"}])
                self.assertEqual(server._all_sources()[0]["product"], "Test")
                self.assertFalse(path.with_suffix(".tmp").exists())

    def test_push_subscription_validation(self):
        self.assertTrue(server._valid_subscription({"endpoint": "https://push.example/1", "keys": {"p256dh": "key", "auth": "auth"}}))
        self.assertFalse(server._valid_subscription({"endpoint": "http://push.example/1", "keys": {}}))

    def test_report_reason_validation_values(self):
        self.assertIn("false_alert", {"false_alert", "wrong_price", "broken_link", "other"})

    def test_extract_public_image_url(self):
        self.assertEqual(server.extract_image_url('<meta property="og:image" content="https://cdn.example.com/item.jpg">'), "https://cdn.example.com/item.jpg")
        self.assertIsNone(server.extract_image_url('<meta property="og:image" content="http://cdn.example.com/item.jpg">'))

    def test_push_preferences_default_and_validation(self):
        self.assertEqual(server._clean_push_preferences({}), {"max_markup": 80})
        self.assertEqual(server._clean_push_preferences({"max_markup": "20"}), {"max_markup": 20.0})
        with self.assertRaises(HTTPException):
            server._clean_push_preferences({"max_markup": -1})

    def test_push_ready_requires_key_pair(self):
        with patch.object(server, "VAPID_PUBLIC_KEY", "public"), patch.object(server, "VAPID_PRIVATE_KEY", "private"):
            self.assertTrue(server.push_ready())
        with patch.object(server, "VAPID_PUBLIC_KEY", "public"), patch.object(server, "VAPID_PRIVATE_KEY", ""):
            self.assertFalse(server.push_ready())

    def test_subscription_storage_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "push.json"
            subscription = {"endpoint": "https://push.example/1", "keys": {"p256dh": "key", "auth": "auth"}}
            with patch.object(server, "PUSH_SUBSCRIPTIONS_FILE", path):
                server._write_subscriptions([subscription])
                self.assertEqual(server._subscriptions(), [subscription])

    def test_moderator_code_has_limited_manager_role(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "moderators.json"
            code = "moderator-code"
            moderator = {"id": "mod-1", "name": "Marie", "secret_hash": server._token_hash(code)}
            with patch.object(server, "MODERATORS_FILE", path), patch.object(server, "ADMIN_SECRET", "owner-code"):
                server._write_moderators([moderator])
                self.assertEqual(server.manager_role("Bearer owner-code"), "owner")
                self.assertEqual(server.manager_role(f"Bearer {code}"), "moderator")
                with self.assertRaises(HTTPException):
                    server.manager_role("Bearer nope")

    def test_owner_secret_is_required(self):
        with patch.object(server, "ADMIN_SECRET", ""):
            with self.assertRaises(HTTPException) as error:
                server.require_admin("Bearer something")
            self.assertEqual(error.exception.status_code, 503)
        with patch.object(server, "ADMIN_SECRET", "correct-secret"):
            with self.assertRaises(HTTPException) as error:
                server.require_admin("Bearer wrong-secret")
            self.assertEqual(error.exception.status_code, 401)
            server.require_admin("Bearer correct-secret")


if __name__ == "__main__":
    unittest.main()
