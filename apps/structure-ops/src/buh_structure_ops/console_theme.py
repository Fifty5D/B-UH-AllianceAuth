"""Install the scoped console theme through Alliance Auth's custom CSS support.

Archive and VPS Health remain independently installed add-ons. This presentation
layer deliberately uses their existing markup without replacing their packages.
"""

from pathlib import Path

from allianceauth.custom_css.models import CustomCSS
from django.db import transaction

START = "/* B-UH console theme: start */"
END = "/* B-UH console theme: end */"
STYLESHEET = Path(__file__).parent / "static/buh_structure_ops/css/console-theme.css"


def replace_theme(existing: str, stylesheet: str) -> str:
    """Replace only our marked block, preserving all administrator-written CSS."""
    if START in stylesheet or END in stylesheet:
        raise ValueError("The console stylesheet contains a reserved marker.")
    block = f"{START}\n{stylesheet.rstrip()}\n{END}"
    if START not in existing and END not in existing:
        return existing + ("\n\n" if existing else "") + block
    if (
        existing.count(START) != 1
        or existing.count(END) != 1
        or existing.index(END) < existing.index(START)
    ):
        raise ValueError(
            "The B-UH console CSS markers are incomplete or duplicated; "
            "existing custom CSS was left untouched."
        )
    start = existing.index(START)
    end = existing.index(END) + len(END)
    return existing[:start] + block + existing[end:]


@transaction.atomic
def sync_console_theme(*, dry_run: bool = False) -> bool:
    """Update the managed CSS block without changing any other site settings."""
    stylesheet = STYLESHEET.read_text(encoding="utf-8")
    custom_css = CustomCSS.objects.select_for_update().filter(pk=1).first()
    if custom_css is None:
        custom_css = CustomCSS(pk=1)
    updated = replace_theme(custom_css.css or "", stylesheet)
    if updated == custom_css.css:
        return False
    if not dry_run:
        custom_css.css = updated
        custom_css.save()
    return True
