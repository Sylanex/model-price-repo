# model-price-repo

Filtered model pricing data for CRS and sub2api projects. Syncs from the upstream [litellm](https://github.com/BerriAI/litellm) pricing file on a schedule, applying configurable prefix filters to keep only the models you actually use.

## How it works

A GitHub Actions workflow runs every 10 minutes (and on manual trigger):

1. Downloads the full `model_prices_and_context_window.json` from litellm
2. Filters models by the prefix rules in `config.json`
3. Merges new models into the existing output (additive — never removes)
4. Applies alias mappings and custom model definitions
5. Writes the output JSON + SHA-256 hash, commits only if content changed

## Configuration

All settings live in [`config.json`](config.json):

| Field | Description |
|---|---|
| `upstream_url` | URL to the upstream litellm pricing JSON |
| `output_file` | Output filename (default: `model_prices_and_context_window.json`) |
| `hash_file` | SHA-256 hash filename for change detection |
| `sync_mode` | `"additive"` (only add new) or `"full"` (replace each run) |
| `update_existing` | In additive mode, `false` freezes existing models' complete pricing field set, including absent rates and `long_context_*` tiers; only new metadata fields are absorbed. `true` replaces existing entries from upstream. |
| `refresh_existing_models` | Existing model keys that should be replaced from current upstream data on every sync |
| `prefix_filters` | List of prefixes — a model key must start with one to be included |
| `exclude_patterns` | Substring patterns to exclude (applied before prefix matching) |
| `aliases` | Map alias model keys to existing source models (deep copy pricing) |
| `custom_models` | Manually defined pricing objects, always injected |
| `replace_custom_models` | Custom model keys that replace an existing entry instead of merging with it |

With `sync_mode: "additive"` and `update_existing: false`, a missing price is
also preserved as missing. For example, adding an upstream Priority rate or a
long-context threshold must not silently change an existing model's billing.
Fields containing `cost`, `price`, or `pricing`, and fields starting with
`long_context_`, are frozen along with existing values (including zero).
Existing metadata values are preserved; newly introduced metadata can be added.
New models are still imported with all their pricing fields.

Use `refresh_existing_models` to opt selected existing models into full upstream
refreshes, including price changes and removal of obsolete fields. Aliases and
`custom_models` are applied afterwards as explicit configuration overrides.

### Adding new model prefixes

Edit the `prefix_filters` array in `config.json`:

```json
{
  "prefix_filters": [
    "claude-",
    "gpt-",
    "your-new-prefix/"
  ]
}
```

### Adding aliases

Aliases create copies of an existing model's pricing under a new key:

```json
{
  "aliases": {
    "claude-opus-4-6-thinking": {
      "source": "claude-opus-4-6",
      "description": "Thinking variant, same pricing"
    }
  }
}
```

If the source model doesn't exist in the filtered data, the alias is skipped with a warning.

## Running locally

```bash
python3 scripts/sync_prices.py --config config.json --repo-root .
```

No pip dependencies — uses Python standard library only.

Run the regression tests without network access:

```bash
python3 -m unittest discover -s tests -v
```

## CRS integration

Point CRS to the raw output file from this repo:

```
MODEL_PRICES_URL=https://raw.githubusercontent.com/<owner>/model-price-repo/main/model_prices_and_context_window.json
```

The output JSON structure is identical to what litellm produces (model key -> pricing object), so CRS `pricingService.js` works without changes.

## License

[MIT](LICENSE)
