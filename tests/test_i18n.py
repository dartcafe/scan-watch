import ast
import importlib.util
import string
import unittest
from pathlib import Path
from unittest import mock

from scan_watch import i18n
from scan_watch.i18n import _, negotiate, parse_po, render

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("update_po", ROOT / "tools" / "update_po.py")
update_po = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update_po)


def fields(text):
    return {name for _lit, name, _spec, _conv in string.Formatter().parse(text) if name}


class CatalogTest(unittest.TestCase):
    def test_every_text_is_translated_with_the_same_placeholders(self):
        for lang in i18n.languages():
            if lang == i18n.SOURCE_LANGUAGE:
                continue
            translations = i18n.catalog(lang)
            for msgid in update_po.collect():
                with self.subTest(lang=lang, msgid=msgid):
                    self.assertTrue(msgid in translations, f"untranslated, fill in scan_watch/locale/{lang}.po")
                    self.assertEqual(fields(translations[msgid]), fields(msgid))

    def test_catalogs_are_up_to_date(self):
        with mock.patch("sys.argv", ["update_po.py", "--check"]), mock.patch("builtins.print") as out:
            self.assertEqual(update_po.main(), 0, out.call_args_list)

    def test_underscore_is_never_shadowed(self):
        # `_, x = ...` in a function makes _ local there and breaks every _() call in it.
        for path in (ROOT / "scan_watch").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text("utf-8"))):
                targets = []
                if isinstance(node, (ast.Assign, ast.For, ast.comprehension, ast.With)):
                    targets = [n for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]
                for name in targets:
                    self.assertNotEqual(name.id, "_", f"{path.name}:{name.lineno}")


class TextTest(unittest.TestCase):
    def test_render_in_both_languages(self):
        text = _("Saved: {path} ({info})", path="/scans/a.pdf", info=_("{n} pages", n=3))
        self.assertEqual(render(text, "en"), "Saved: /scans/a.pdf (3 pages)")
        self.assertEqual(render(text, "de"), "Gespeichert: /scans/a.pdf (3 Seiten)")

    def test_format_spec_and_exception_arguments(self):
        self.assertEqual(render(_("{page}: {ratio:.2f} % content", page=_("Page {n}", n=2), ratio=1.234), "de"),
                         "Seite 2: 1.23 % Inhalt")
        error = ValueError(_("Scanner address missing"))
        self.assertEqual(render(_("Error: {error}", error=error), "de"), "Fehler: Scanner-Adresse fehlt")
        self.assertEqual(render(OSError(2, "No such file"), "de"), "[Errno 2] No such file")

    def test_unknown_text_and_language_fall_back_to_english(self):
        self.assertEqual(render(_("not in any catalog {x}", x=1), "de"), "not in any catalog 1")
        self.assertEqual(render(_("Saved."), "xx"), "Saved.")


class LanguageTest(unittest.TestCase):
    def test_log_is_english_whatever_the_environment(self):
        with mock.patch.dict("os.environ", {"LANG": "de_DE.UTF-8", "SCAN_WATCH_LANGUAGE": "de"}):
            self.assertEqual(i18n.server_language(), "en")
            self.assertEqual(render(_("Applied.")), "Applied.")

    def test_locale_for_an_interactive_terminal(self):
        self.assertEqual(i18n.locale_language({}), "en")
        self.assertEqual(i18n.locale_language({"LANG": "de_DE.UTF-8"}), "de")
        self.assertEqual(i18n.locale_language({"LANG": "de_DE.UTF-8", "LC_ALL": "C"}), "en")
        self.assertEqual(i18n.locale_language({"LANG": "es_ES.UTF-8"}), "en")
        with mock.patch.dict("os.environ", {"LANG": "de_DE.UTF-8"}, clear=True), \
                mock.patch.object(i18n, "_terminal_language", "en"):
            i18n.use_locale()
            self.assertEqual(render(_("Applied.")), "Übernommen.")

    def test_accept_language(self):
        self.assertEqual(negotiate("de-DE,de;q=0.9,en;q=0.8"), "de")
        self.assertEqual(negotiate("fr-FR,fr;q=0.9,de;q=0.5,en;q=0.7"), "en")
        self.assertEqual(negotiate("fr"), "en")
        self.assertEqual(negotiate(None), "en")
        self.assertEqual(negotiate("de;q=0"), "en")


class PoTest(unittest.TestCase):
    def test_parse_po(self):
        po = r'''msgid ""
msgstr "Language: de\n"

#, fuzzy
msgid "Old"
msgstr "Alt"

msgid ""
"Two\n"
"lines"
msgstr "Zwei\nZeilen \"zitiert\""
'''
        self.assertEqual(list(parse_po(po)), [("", "Language: de\n", False), ("Old", "Alt", True),
                                              ("Two\nlines", 'Zwei\nZeilen "zitiert"', False)])


if __name__ == "__main__":
    unittest.main()
