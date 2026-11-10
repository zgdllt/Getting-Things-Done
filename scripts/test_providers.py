"""Tests for model provider profiles and their workbench routes. Synthetic data only."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gtd
import gtd_dashboard as web
import gtd_provider
import gtd_providers

SECRET = "sk-test-secret-value-1234567890"


class ProviderProfileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = gtd.State(Path(self.temp.name))

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def save(self, **overrides):
        data = {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1/",
                "api_key": SECRET, "model": "deepseek-chat", "disable_thinking": False, "max_tokens": None}
        return gtd_providers.save(self.state, {**data, **overrides})

    def stored(self, profile_id="deepseek"):
        return json.loads((self.state.base / "secrets" / f"{profile_id}.json").read_text())

    def test_hand_written_provider_files_are_listed_without_keys(self):
        folder = self.state.base / "secrets"
        folder.mkdir(mode=0o700)
        (folder / "madmodel.json").write_text(json.dumps({"base_url": "https://m.example/v1", "api_key": SECRET,
                                                          "model": "DeepSeek-V4.1-Flash"}))
        (folder / "notes.json").write_text(json.dumps({"unrelated": True}))
        value = gtd_providers.listing(self.state)
        self.assertEqual(value["backend"], "codex")
        self.assertEqual([p["id"] for p in value["profiles"]], ["madmodel"])
        self.assertTrue(value["presets"])
        self.assertNotIn(SECRET, json.dumps(value))

    def test_save_writes_private_profile_and_blank_key_keeps_the_stored_one(self):
        result = self.save()
        path = self.state.base / "secrets" / "deepseek.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.stored()["base_url"], "https://api.deepseek.com/v1")
        self.assertNotIn(SECRET, json.dumps(result))
        self.save(api_key="", model="deepseek-reasoner")
        self.assertEqual((self.stored()["api_key"], self.stored()["model"]), (SECRET, "deepseek-reasoner"))

    def test_thinking_and_max_tokens_map_to_extra_body_and_keep_other_options(self):
        self.save(disable_thinking=True, max_tokens=8000)
        value = self.stored()
        value["extra_body"]["top_p"] = 0.9
        gtd.atomic_json(self.state.base / "secrets" / "deepseek.json", value)
        self.save(api_key="")
        profile = gtd_providers.listing(self.state)["profiles"][0]
        self.assertFalse(profile["disable_thinking"])
        self.assertIsNone(profile["max_tokens"])
        self.assertEqual(self.stored()["extra_body"], {"top_p": 0.9})
        self.save(api_key="", disable_thinking=True, max_tokens=4096)
        profile = gtd_providers.listing(self.state)["profiles"][0]
        self.assertEqual((profile["disable_thinking"], profile["max_tokens"]), (True, 4096))

    def test_local_server_without_key_and_url_rules(self):
        self.save(id="strata", base_url="http://127.0.0.1:18080/v1", api_key="")
        self.assertEqual(self.stored("strata")["api_key"], gtd_providers.NO_AUTH_KEY)
        self.assertFalse(gtd_providers.listing(self.state)["profiles"][0]["has_key"])
        for url in ("http://api.example.com/v1", "ftp://127.0.0.1/v1", ""):
            with self.assertRaises(ValueError):
                self.save(id="other", base_url=url)

    def test_new_ids_must_start_lowercase_and_values_are_validated(self):
        for bad in ("Upper", "1abc", "../x", "a b"):
            with self.assertRaises(ValueError):
                self.save(id=bad)
        for field, value in (("model", "two words"), ("max_tokens", 10), ("max_tokens", True)):
            with self.assertRaises(ValueError):
                self.save(**{field: value})
        self.assertFalse((self.state.base / "secrets").exists() and any((self.state.base / "secrets").iterdir()))

    def test_use_switches_backend_and_label(self):
        self.save()
        gtd.atomic_json(self.state.base / "config.json", {"extractor_model": "keep-me"})
        gtd_providers.use(self.state, {"backend": "provider", "id": "deepseek"})
        self.assertEqual(gtd.provider_file(self.state).resolve(), (self.state.base / "secrets" / "deepseek.json").resolve())
        self.assertEqual(gtd_providers.label(self.state), {"model": "deepseek-chat", "reasoning_effort": "DeepSeek",
                                                           "backend": "provider"})
        self.assertEqual(gtd_providers.listing(self.state)["active"], "deepseek")
        gtd_providers.use(self.state, {"backend": "codex"})
        self.assertIsNone(gtd.provider_file(self.state))
        self.assertEqual(gtd_providers.label(self.state)["model"], "keep-me")
        with self.assertRaises(ValueError):
            gtd_providers.use(self.state, {"backend": "provider", "id": "missing"})

    def test_active_profile_cannot_be_deleted(self):
        self.save()
        self.save(id="qwen", name="Qwen", model="qwen-plus")
        gtd_providers.use(self.state, {"backend": "provider", "id": "deepseek"})
        with self.assertRaises(ValueError):
            gtd_providers.delete(self.state, {"id": "deepseek"})
        gtd_providers.delete(self.state, {"id": "qwen"})
        self.assertEqual([p["id"] for p in gtd_providers.listing(self.state)["profiles"]], ["deepseek"])

    def test_check_records_status_without_the_key(self):
        self.save()
        with patch.object(gtd_provider, "chat_json", return_value={"ok": True, "_usage": {}}) as call:
            result = gtd_providers.check(self.state, {"id": "deepseek"})
        self.assertTrue(result["ok"])
        self.assertEqual(call.call_args.kwargs["retries"], 0)
        with patch.object(gtd_provider, "chat_json", side_effect=gtd_provider.ProviderError("HTTP 401: bad key")):
            failed = gtd_providers.check(self.state, {"id": "deepseek"})
        self.assertFalse(failed["ok"])
        status = gtd_providers.listing(self.state)["profiles"][0]["status"]
        self.assertEqual((status["ok"], status["error"]), (False, "HTTP 401: bad key"))
        self.assertNotIn(SECRET, (self.state.base / "provider-status.json").read_text())

    def test_models_uses_stored_key_or_form_values(self):
        self.save()
        with patch.object(gtd_provider, "list_models", return_value=["a", "b"]) as call:
            self.assertEqual(gtd_providers.models(self.state, {"id": "deepseek"})["models"], ["a", "b"])
            self.assertEqual(call.call_args.args, ("https://api.deepseek.com/v1", SECRET))
            gtd_providers.models(self.state, {"base_url": "http://localhost:8080/v1", "api_key": ""})
            self.assertEqual(call.call_args.args, ("http://localhost:8080/v1", gtd_providers.NO_AUTH_KEY))
        with patch.object(gtd_provider, "list_models", side_effect=gtd_provider.ProviderError("HTTP 404")):
            self.assertEqual(gtd_providers.models(self.state, {"id": "deepseek"}), {"ok": False, "models": [], "error": "HTTP 404"})

    def test_list_models_parses_openai_catalog(self):
        body = {"data": [{"id": "m2"}, {"id": "m1"}, {"id": "m1"}, {"object": "model"}]}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(body).encode())) as call:
            self.assertEqual(gtd_provider.list_models("https://x.invalid/v1/", "k"), ["m1", "m2"])
        self.assertEqual(call.call_args.args[0].full_url, "https://x.invalid/v1/models")
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"error": "no"}')):
            with self.assertRaises(gtd_provider.ProviderError):
                gtd_provider.list_models("https://x.invalid/v1", "k")

    def test_workbench_routes_and_summary(self):
        payload = {"id": "strata", "base_url": "http://127.0.0.1:18080/v1", "api_key": "", "model": "swift",
                   "disable_thinking": True, "max_tokens": 8000}
        web.action(self.state, "/api/providers/save", payload)
        web.action(self.state, "/api/providers/use", {"backend": "provider", "id": "strata"})
        self.assertEqual(web.dashboard_state(self.state)["active_model"]["model"], "swift")
        self.assertEqual(self.stored("strata")["extra_body"],
                         {"chat_template_kwargs": {"enable_thinking": False}, "max_tokens": 8000})


if __name__ == "__main__":
    unittest.main()
