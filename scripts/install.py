#!/usr/bin/env python3
"""Install the source-only Codex skill; never copies personal state or starts services."""
import argparse
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent.parent
PAYLOAD = (
    "SKILL.md", "README.md", "LICENSE", "agents/openai.yaml",
    "references/analysis.schema.json", "references/extractor.md",
    "references/clarification.schema.json", "references/clarification.md",
    "references/options.schema.json", "references/options.md",
    "references/native-operations.md", "references/source-adapters.md", "references/workbench.md", "references/verification.md",
    "references/file-organization.md", "references/files-extractor.md", "references/files.schema.json",
    "scripts/gtd.py", "scripts/gtd_native.swift", "scripts/gtd_dashboard.py",
    "scripts/gtd_board.py", "scripts/gtd_board_merge.py", "scripts/gtd_feedback.py", "scripts/gtd_board_notifications.py",
    "scripts/test_board.py", "scripts/test_board_support.py", "scripts/test_board_merge.py", "scripts/test_board_notifications.py",
    "scripts/gtd_threads.py", "scripts/gtd_memory.py", "scripts/test_threads.py",
    "scripts/gtd_models.py", "scripts/gtd_sources.py", "scripts/gtd_clarify.py", "scripts/gtd_options.py",
    "scripts/gtd_provider.py", "scripts/gtd_providers.py", "scripts/test_providers.py", "scripts/gtd_local.py", "scripts/gtd_compare.py",
    "scripts/gtd_files.py", "scripts/gtd_file_store.py", "scripts/gtd_projects.py",
    "scripts/gtd_notifications.py", "scripts/gtd_notify.swift", "scripts/install.py",
    "scripts/test_gtd.py", "scripts/test_workbench.py", "scripts/test_options.py", "scripts/test_local.py", "scripts/verify_workbench_browser.py",
    "scripts/test_files.py", "scripts/verify_files_browser.py", "scripts/verify_file_tags_native.py",
    "web/index.html", "web/app.js", "web/deck.js", "web/style.css", "web/icon.svg",
    "web/files.js", "web/providers.js", "web/files.css",
)


def install(destination, update=False):
    destination = Path(destination).expanduser().absolute()
    if destination.resolve() == ROOT:
        return destination
    if destination.is_symlink():
        raise ValueError("Destination is a symlink; choose a regular directory with --dest")
    if destination.exists() and any(destination.iterdir()) and not update:
        raise ValueError("Skill directory already contains files. Use --update to replace only this project's source files")
    # Check every path before copying, including existing nested links.
    for name in PAYLOAD:
        if not (ROOT / name).is_file():
            raise ValueError("Missing release source: " + name)
        target = destination / name
        for part in (target, *target.parents):
            if part == destination.parent:
                break
            if part.is_symlink():
                raise ValueError("Refusing to follow an existing symlink: " + str(part))
    destination.mkdir(parents=True, exist_ok=True)
    for name in PAYLOAD:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--dest", default=str(home / "skills/personal-gtd"))
    parser.add_argument("--update", action="store_true", help="Replace owned source files in an existing skill directory")
    args = parser.parse_args()
    try:
        destination = install(args.dest, args.update)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print("Installed Getting Things Done skill at " + str(destination))
    print("No services were started and no Calendar/Reminders permissions were requested.")
    print("Next: python3 " + str(destination / "scripts/gtd.py") + " workbench")


if __name__ == "__main__":
    main()
