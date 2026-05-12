#!/usr/bin/env python3
"""
Run selected export functions from `sefaria.export` with Django configured.

Only the JSON merged format is retained — `txt/`, `cltk-flat/`, `cltk-full/`
are suppressed at write time (open() filter) and a safety-net cleanup removes
them after the export call in case the upstream exporter bypasses open().
"""
import builtins
import os
import shutil
import sys
import traceback


SUPPRESSED_FORMAT_DIRS = ("txt", "cltk-flat", "cltk-full")


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


def install_format_filter(export_base: str):
    """Monkey-patch builtins.open to drop writes into unwanted format dirs.

    Returns a restore callable.
    """
    real_open = builtins.open
    suppressed_segments = tuple(
        os.sep + d + os.sep for d in SUPPRESSED_FORMAT_DIRS
    )

    def _is_write_mode(mode) -> bool:
        if not isinstance(mode, str):
            return False
        return any(ch in mode for ch in ("w", "a", "x", "+"))

    def filtered_open(file, mode="r", *args, **kwargs):
        if isinstance(file, (str, bytes, os.PathLike)) and _is_write_mode(mode):
            try:
                path_str = os.fspath(file)
            except TypeError:
                path_str = None
            if path_str is not None:
                # Normalize to absolute path for reliable segment matching
                abs_path = path_str if os.path.isabs(path_str) else os.path.abspath(path_str)
                if any(seg in abs_path for seg in suppressed_segments):
                    return real_open(os.devnull, mode, *args, **kwargs)
        return real_open(file, mode, *args, **kwargs)

    builtins.open = filtered_open

    def restore():
        builtins.open = real_open

    return restore


def cleanup_unwanted_formats(export_base: str) -> None:
    for d in SUPPRESSED_FORMAT_DIRS:
        target = os.path.join(export_base, d)
        if os.path.isdir(target):
            print(f"🧹 Removing unused format dir: {target}")
            shutil.rmtree(target, ignore_errors=True)


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

    restore_open = install_format_filter(export_base)
    try:
        functions_to_run = [
            ("export_all_merged", ex.export_all_merged),
            ("export_links", ex.export_links),
            ("export_schemas", ex.export_schemas),
            ("export_toc", ex.export_toc),
        ]

        for fn_name, fn_callable in functions_to_run:
            print(f"\n{'='*60}")
            print(f"▶️  Running {fn_name}...")
            print(f"{'='*60}")
            try:
                fn_callable()
                print(f"✅ {fn_name} completed")
                # Free disk progressively: drop unwanted formats as soon as
                # the merged exporter has run.
                if fn_name == "export_all_merged":
                    cleanup_unwanted_formats(export_base)
                print(f"📂 Contents of {export_base} after {fn_name}:")
                if os.path.isdir(export_base):
                    list_dir_limited(export_base)
                else:
                    print("(export directory not found)")
            except Exception as e:  # pragma: no cover
                print(f"❌ {fn_name} failed: {e}")
                traceback.print_exc()
                return 1
    finally:
        restore_open()

    # Final safety-net cleanup in case anything slipped through.
    cleanup_unwanted_formats(export_base)

    print("\n✅ All exports completed successfully")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
