# Getting Things Done · Toy demo

A standalone, Chinese-language demo using synthetic examples. It shares `web/deck.js` and the workbench's visual styles. All changes stay in this browser's localStorage; no Computer History, Apple APIs, Codex connection, backend API or model service is used.

The default is one card at a time. Use horizontal touch/trackpad gestures, arrow buttons or keyboard arrows. Settings switches to the retained list layout. Browse Today, clarify a candidate, save an answer, explicitly confirm a concrete next step, complete it and see it in Done. Capture creates a new candidate. Settings also offers a confirmed reset of demo data.

Completing or dismissing shows an in-card acknowledgement before the card slides away. Saving an answer transitions to its saved response state on the same card so you can review and confirm it. Every success marker and toast holds for 0.4 seconds and fades over 0.3 seconds; markers remain independent of card navigation and operation receipts stay readable. Reduced-motion settings skip the transitions while keeping the acknowledgement.

## Build and run

From the repository root:

```sh
python3 scripts/build_toy_demo.py --output dist/toy
python3 -m http.server 8765 --directory dist/toy
```

Open `http://127.0.0.1:8765/index.html`. `dist/toy/` is the uploadable build; this source folder is not the upload package. Asset names include content hashes, references are relative, and the build uses an explicit resource list.

## Toy preview

Install and read the official [`bilibili/toy` skill](https://github.com/bilibili/toy/tree/main/skills/toy). Discover current CLI options with `toy --help-json --json`. Run the skill's `toy_doctor.py` against `dist/toy` before upload, then create a preview with `toy create` and `--json`. The official workflow separates preview from review submission. Inspect the returned preview and explicitly confirm submission before adding `--yes`. The demo does not require cloud-storage or ranking SDK capabilities.

## Verification

`scripts/verify_cards_browser.py` checks the shared carousel and the Toy's full local flow against a nested path, including mobile layout and touch input. See [verification](../../references/verification.md) for scope and limitations.
