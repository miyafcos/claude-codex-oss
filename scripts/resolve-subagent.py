"""Resolve an explicit Workflow child, or prepare session-only launcher agents."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from subagent_models import load_registry, loads, prepare, select

ROOT = Path(__file__).resolve().parent.parent
ADJACENT = Path(__file__).resolve().parent
SOURCE_MODELS = ROOT / "runtime/models.json"
DEFAULT_MODELS = SOURCE_MODELS if SOURCE_MODELS.is_file() else ADJACENT / "models.json"
DEFAULT_REGISTRY = (ROOT / "runtime/claude-codex/subagent-models.json"
                    if SOURCE_MODELS.is_file() else ADJACENT / "subagent-models.json")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection", nargs="?")
    parser.add_argument("--models-json", type=Path, default=DEFAULT_MODELS)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args(argv)
    try:
        registry = load_registry(args.models_json, args.registry)
        if args.list:
            print("name\tmodel\tefforts\tdefault")
            for e in registry["models"]:
                print(f"{e['name']}\t{e['model']}\t{','.join(e['efforts'])}\t{e['defaultEffort']}")
        elif args.prepare:
            data = loads(sys.stdin.read())
            result = prepare(registry, data["selections"], data["args"], models_path=args.models_json,
                             registry_path=args.registry, config_dir=Path(data["configDir"]),
                             session_dir=Path(data["sessionDir"]))
            print(json.dumps(result, ensure_ascii=True))
        elif args.selection:
            result = select(registry, args.selection)
            result.pop("definition")
            result["workflowEnforced"] = False
            result["notice"] = "Preflight only; user authorization and self-contained non-confidential context required."
            print(json.dumps(result, ensure_ascii=True))
        else:
            parser.error("selection or --list is required")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print("subagent selection rejected: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
