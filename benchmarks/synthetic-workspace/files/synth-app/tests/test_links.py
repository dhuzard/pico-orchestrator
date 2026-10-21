import re
import unittest

from synthapp.links import post_url


def reference_slugify(text):
    """Test double for the slugify@1 contract: lowercase ASCII alphanumerics joined by single hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


class PostUrlTests(unittest.TestCase):
    def test_builds_url_from_the_injected_slugify(self):
        self.assertEqual(post_url("Hello, World!", reference_slugify), "/posts/hello-world")

    def test_uses_the_callable_it_is_given(self):
        self.assertEqual(post_url("Anything", lambda text: "fixed"), "/posts/fixed")

    def test_empty_slug_is_an_error(self):
        with self.assertRaises(ValueError):
            post_url("!!!", reference_slugify)


if __name__ == "__main__":
    unittest.main()
