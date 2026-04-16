#!/usr/bin/env python3
"""Run selected export functions from ``sefaria.export`` with Django configured."""
import os
import sys
import traceback


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


def parse_requested_functions() -> list[str]:
    if len(sys.argv) > 1:
        raw_items = sys.argv[1:]
    else:
        raw_items = os.environ.get("SEFARIA_EXPORT_FUNCTIONS", "").split(",")

    requested = []
    for item in raw_items:
        for part in item.split():
            name = part.strip()
            if name:
                requested.append(name)
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

    available_functions = [
        ("export_all_merged", ex.export_all_merged),
        ("export_links", ex.export_links),
        ("export_schemas", ex.export_schemas),
        ("export_toc", ex.export_toc),
    ]

    requested_functions = parse_requested_functions()
    if requested_functions:
        available_by_name = {name: func for name, func in available_functions}
        unknown_functions = [name for name in requested_functions if name not in available_by_name]
        if unknown_functions:
            print(f"❌ Unknown export functions requested: {', '.join(unknown_functions)}")
            print(
                "Available functions: "
                + ", ".join(name for name, _ in available_functions)
            )
            return 1

        functions_to_run = [(name, available_by_name[name]) for name in requested_functions]
    else:
        functions_to_run = available_functions

    print(
        "📦 Export functions selected: "
        + ", ".join(name for name, _ in functions_to_run)
    )

    for fn_name, fn_callable in functions_to_run:
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

    print("\n✅ All exports completed successfully")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
