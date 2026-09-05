import contextlib
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sync_prices", ROOT / "scripts" / "sync_prices.py")
sync_prices = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_prices)


def monetary_fields(entry):
    return {key: value for key, value in entry.items() if sync_prices.is_pricing_field(key)}


class MergeModelsTests(unittest.TestCase):
    def test_additive_freezes_existing_and_absent_pricing_fields(self):
        original = {
            "gpt-existing": {
                "input_cost_per_token": 2e-6,
                "cache_read_input_token_cost": 0,
                "output_cost_per_token": None,
                "long_context_input_token_threshold": 0,
                "description": "Keep existing metadata",
            }
        }
        upstream = {
            "gpt-existing": {
                "input_cost_per_token": 10e-6,
                "cache_read_input_token_cost": 1e-6,
                "output_cost_per_token": 50e-6,
                "input_cost_per_token_priority": 20e-6,
                "output_cost_per_token_above_272k_tokens": 75e-6,
                "cache_creation_input_token_cost": 12.5e-6,
                "output_cost_per_image": 0.3,
                "output_cost_per_audio_token": 12e-6,
                "search_context_cost_per_query": {"high": 0.01},
                "long_context_input_token_threshold": 272000,
                "long_context_input_cost_multiplier": 2,
                "long_context_output_multiplier": 1.5,
                "future_price_per_request": 0.2,
                "future_pricing_tiers": [{"rate": 0.1}],
                "description": "Do not replace existing metadata",
                "supports_reasoning": True,
                "max_input_tokens": 922000,
            }
        }
        before = copy.deepcopy(original)

        merged, stats = sync_prices.merge_models(original, upstream, "additive", False)

        self.assertEqual(monetary_fields(before["gpt-existing"]), monetary_fields(merged["gpt-existing"]))
        self.assertEqual(merged["gpt-existing"]["description"], "Keep existing metadata")
        self.assertTrue(merged["gpt-existing"]["supports_reasoning"])
        self.assertEqual(merged["gpt-existing"]["max_input_tokens"], 922000)
        self.assertEqual(original, before, "metadata merging must not mutate the source snapshot")
        self.assertEqual(stats["updated"], 1)

    def test_new_prices_alone_do_not_mark_existing_model_updated(self):
        existing = {"gpt-existing": {"input_cost_per_token": 2e-6}}
        upstream = {"gpt-existing": {"input_cost_per_token": 10e-6, "input_cost_per_token_priority": 20e-6}}
        merged, stats = sync_prices.merge_models(existing, upstream, "additive", False)
        self.assertEqual(merged, existing)
        self.assertEqual(stats["updated"], 0)
        self.assertEqual(stats["unchanged"], 1)

    def test_new_models_import_complete_price_cards(self):
        upstream = {"gpt-6-astra": {"input_cost_per_token": 10e-6, "input_cost_per_token_priority": 20e-6, "long_context_input_token_threshold": 272000}}
        merged, stats = sync_prices.merge_models({}, upstream, "additive", False)
        self.assertEqual(merged, upstream)
        self.assertEqual(stats["added"], 1)

    def test_update_existing_true_replaces_prices_and_removes_obsolete_fields(self):
        existing = {"gpt-existing": {"input_cost_per_token": 2e-6, "long_context_input_token_threshold": 272000}}
        upstream = {"gpt-existing": {"input_cost_per_token": 10e-6, "input_cost_per_token_priority": 20e-6}}
        merged, stats = sync_prices.merge_models(existing, upstream, "additive", True)
        self.assertEqual(merged, upstream)
        self.assertEqual(stats["updated"], 1)

    def test_full_sync_is_not_frozen(self):
        existing = {"removed": {"input_cost_per_token": 1}, "gpt-existing": {"input_cost_per_token": 2e-6}}
        upstream = {"gpt-existing": {"input_cost_per_token": 10e-6, "input_cost_per_token_priority": 20e-6}}
        merged, _ = sync_prices.merge_models(existing, upstream, "full", False)
        self.assertEqual(merged, upstream)

    def test_refresh_is_explicit_exception_to_price_freeze(self):
        existing = {
            "gpt-6-astra": {"input_cost_per_token": 99, "long_context_input_token_threshold": 1},
            "gpt-existing": {"input_cost_per_token": 2e-6},
            "missing": {"input_cost_per_token": 3e-6},
        }
        upstream = {
            "gpt-6-astra": {"input_cost_per_token": 10e-6, "cache_creation_input_token_cost": 12.5e-6},
            "gpt-existing": {"input_cost_per_token": 10e-6, "input_cost_per_token_priority": 20e-6},
        }
        merged, _ = sync_prices.merge_models(copy.deepcopy(existing), upstream, "additive", False)
        merged = sync_prices.refresh_existing_models(merged, upstream, ["gpt-6-astra", "missing"])
        self.assertEqual(merged["gpt-6-astra"], upstream["gpt-6-astra"])
        self.assertEqual(merged["gpt-existing"], existing["gpt-existing"])
        self.assertEqual(merged["missing"], existing["missing"])

    def test_field_classification_covers_rates_and_tiers(self):
        for field in ("INPUT_COST_PER_TOKEN", "price", "pricing_tiers", "long_context_input_token_threshold", "long_context_output_multiplier"):
            with self.subTest(field=field):
                self.assertTrue(sync_prices.is_pricing_field(field))
        for field in ("supports_reasoning", "max_input_tokens", "description", "deprecation_date"):
            with self.subTest(field=field):
                self.assertFalse(sync_prices.is_pricing_field(field))


class SyncOutputTests(unittest.TestCase):
    def test_full_pipeline_preserves_other_prices_and_writes_idempotent_hash(self):
        config = {
            "upstream_url": "https://example.invalid/pricing.json",
            "output_file": "prices.json",
            "hash_file": "prices.sha256",
            "sync_mode": "additive",
            "update_existing": False,
            "refresh_existing_models": ["gpt-6-astra"],
            "prefix_filters": ["gpt-"],
            "aliases": {"gpt-existing-alias": {"source": "gpt-existing"}},
            "custom_models": {"custom-model": {"input_cost_per_token": 0}},
        }
        existing = {
            "gpt-existing": {"input_cost_per_token": 2e-6, "cache_read_input_token_cost": 0},
            "gpt-6-astra": {"input_cost_per_token": 99, "long_context_input_token_threshold": 1},
        }
        upstream = {
            "gpt-existing": {"input_cost_per_token": 10e-6, "cache_read_input_token_cost": 1e-6, "input_cost_per_token_priority": 20e-6, "long_context_input_token_threshold": 272000, "supports_reasoning": True},
            "gpt-6-astra": {"input_cost_per_token": 10e-6, "output_cost_per_token": 50e-6, "cache_read_input_token_cost": 1e-6, "cache_creation_input_token_cost": 12.5e-6},
            "gpt-new": {"input_cost_per_token": 1e-6, "input_cost_per_token_priority": 2e-6},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            (root / config["output_file"]).write_text(json.dumps(existing), encoding="utf-8")

            def run_sync():
                out = io.StringIO()
                with patch.object(sync_prices, "fetch_upstream", return_value=copy.deepcopy(upstream)), patch("sys.argv", ["sync_prices.py", "--repo-root", tmp]), contextlib.redirect_stdout(out):
                    sync_prices.main()
                raw = (root / config["output_file"]).read_bytes()
                digest = (root / config["hash_file"]).read_text(encoding="utf-8").strip()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
                return json.loads(raw), raw, digest, out.getvalue()

            generated, raw, digest, output = run_sync()
            self.assertIn("CHANGED=true", output)
            self.assertEqual(monetary_fields(existing["gpt-existing"]), monetary_fields(generated["gpt-existing"]))
            self.assertEqual(generated["gpt-existing-alias"], generated["gpt-existing"])
            self.assertEqual(generated["gpt-6-astra"], upstream["gpt-6-astra"])
            self.assertEqual(generated["gpt-new"], upstream["gpt-new"])
            self.assertEqual(generated["custom-model"], {"input_cost_per_token": 0})
            repeated, repeated_raw, repeated_digest, repeated_output = run_sync()
            self.assertEqual(repeated, generated)
            self.assertEqual(repeated_raw, raw)
            self.assertEqual(repeated_digest, digest)
            self.assertIn("CHANGED=false", repeated_output)


if __name__ == "__main__":
    unittest.main()
