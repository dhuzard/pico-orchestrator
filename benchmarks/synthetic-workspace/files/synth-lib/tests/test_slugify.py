import unittest

from synthlib.text import slugify


class SlugifyTests(unittest.TestCase):
    def test_lowercases_and_joins_with_hyphens(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_collapses_runs_of_other_characters(self):
        self.assertEqual(slugify("Hello,   World!!"), "hello-world")

    def test_strips_leading_and_trailing_separators(self):
        self.assertEqual(slugify("  --Hello--  "), "hello")

    def test_keeps_ascii_numbers(self):
        self.assertEqual(slugify("Release 2.0 (final)"), "release-2-0-final")

    def test_non_ascii_letters_are_separators_not_transliterated(self):
        self.assertEqual(slugify("Café au Lait"), "caf-au-lait")

    def test_empty_and_symbol_only_input_give_empty_string(self):
        self.assertEqual(slugify(""), "")
        self.assertEqual(slugify("!!! ???"), "")

    def test_rejects_non_strings(self):
        with self.assertRaises(TypeError):
            slugify(None)


if __name__ == "__main__":
    unittest.main()
