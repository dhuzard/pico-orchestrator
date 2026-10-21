import unittest
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "approved-stopwords.txt"


class StopwordsTests(unittest.TestCase):
    def test_approved_input_is_present(self):
        self.assertTrue(DATA.is_file(), f"missing approved input: {DATA.name}")

    def test_strip_stopwords_uses_the_approved_list(self):
        from synthlib.text import strip_stopwords

        words = [w for w in DATA.read_text(encoding="utf-8").split() if w]
        self.assertTrue(words, "approved list is empty")
        self.assertEqual(strip_stopwords(f"{words[0]} keep", DATA), "keep")


if __name__ == "__main__":
    unittest.main()
