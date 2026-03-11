#!/usr/bin/env python3
"""
Run selected export functions from `sefaria.export` with Django configured.

"""
import os
import sys
import traceback


EXPORT_FUNCTION_NAMES = (
    "export_all_merged",
    "export_links",
    "export_schemas",
    "export_toc",
)


def list_dir_limited(base: str) -> None:
    for root, dirs, files in os.walk(base):
        level = root.replace(base, '').count(os.sep)
        indent = ' ' * 2 * level
        print(f"{indent}{os.path.basename(root)}/")
        subindent = ' ' * 2 * (level + 1)
        for file in files[:10]:
            print(f"{subindent}{file}")
        if len(files) > 10:
            print(f"{subindent}... and {len(files) - 10} more files")
        if level > 2:
            break


def parse_requested_functions(argv: list[str]) -> list[str]:
    requested = argv[1:] or os.environ.get("SEFARIA_EXPORT_FUNCTIONS", "").split()
    if not requested:
        return list(EXPORT_FUNCTION_NAMES)

    invalid = [name for name in requested if name not in EXPORT_FUNCTION_NAMES]
    if invalid:
        valid = ", ".join(EXPORT_FUNCTION_NAMES)
        invalid_joined = ", ".join(invalid)
        raise SystemExit(f"Unknown export function(s): {invalid_joined}. Valid values: {valid}")

    return requested


def main() -> int:
    workspace = os.environ.get('GITHUB_WORKSPACE', os.getcwd())
    proj_dir = os.path.join(workspace, 'Sefaria-Project')
    sys.path.insert(0, os.path.abspath(proj_dir))
    os.chdir(proj_dir)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sefaria.settings")

    export_base = os.path.join(workspace, 'exports')
    os.environ["SEFARIA_EXPORT_PATH"] = export_base

    print(f"📁 Export base directory: {export_base}")
    print(f"📁 Current working directory: {os.getcwd()}")

    import django
    django.setup()

    from django.conf import settings
    print(f"📋 Django SEFARIA_EXPORT_PATH: {getattr(settings, 'SEFARIA_EXPORT_PATH', 'NOT SET')}")

    from sefaria import export as ex

    available_functions = {
        "export_all_merged": ex.export_all_merged,
        "export_links": ex.export_links,
        "export_schemas": ex.export_schemas,
        "export_toc": ex.export_toc,
    }
    requested_functions = parse_requested_functions(sys.argv)

    for fn_name in requested_functions:
        fn_callable = available_functions[fn_name]
        print(f"\n{'='*60}")
        print(f"▶️  Running {fn_name}...")
        print(f"{'='*60}")
        try:
            fn_callable()
            print(f"✅ {fn_name} completed")
            print(f"📂 Contents of {export_base} after {fn_name}:")
            if os.path.isdir(export_base):
                list_dir_limited(export_base)
            else:
                print("(export directory not found)")
        except Exception as e:  # pragma: no cover
            print(f"❌ {fn_name} failed: {e}")
            traceback.print_exc()
            return 1

    print("\n✅ Requested exports completed successfully")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
