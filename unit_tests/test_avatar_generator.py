import re
import unittest
import xml.dom.minidom

from backend import avatar_generator as ag
from backend.avatar_generator import DEFAULT_COLORS, VARIANTS, generate_avatar_svg


class AvatarGeneratorTests(unittest.TestCase):
    def test_hash_matches_the_original_js_implementation(self):
        # Values produced by boring-avatars' own hashCode() in Node
        expected = {"galactus": 1801023714, "thanos": 874940603, "日本語": 25921943, "😀 bot": 927968650, "a": 97}
        for name, value in expected.items():
            self.assertEqual(ag._hash_code(name), value, name)

    def test_same_name_gives_same_avatar_in_every_variant(self):
        for v in VARIANTS:
            self.assertEqual(generate_avatar_svg("Galactus", variant=v), generate_avatar_svg("Galactus", variant=v))

    def test_name_is_normalised(self):
        self.assertEqual(generate_avatar_svg("Galactus"), generate_avatar_svg("  galactus "))

    def test_different_names_give_different_avatars(self):
        names = ["Galactus", "Thanos", "Specter", "Apski", "Otentik", "A", "B", "C", "D", "E"]
        for v in VARIANTS:
            self.assertEqual(len({generate_avatar_svg(n, variant=v) for n in names}), len(names), v)

    def test_every_variant_is_well_formed_svg(self):
        for v in VARIANTS:
            for name in ["Galactus", "", "Nama <script>alert(1)</script>", "日本語のエージェント", "a" * 500]:
                doc = xml.dom.minidom.parseString(generate_avatar_svg(name, variant=v))
                self.assertEqual(doc.documentElement.tagName, "svg")

    def test_user_text_never_reaches_the_markup(self):
        for v in VARIANTS:
            svg = generate_avatar_svg('"><script>alert(1)</script> onload=x', variant=v)
            self.assertNotIn("script", svg.lower())
            self.assertIsNone(re.search(r"\son\w+=", svg))

    def test_unknown_variant_falls_back_to_default(self):
        self.assertEqual(generate_avatar_svg("Galactus", variant="nope"), generate_avatar_svg("Galactus"))

    def test_invalid_colours_are_ignored(self):
        bad = ['red"><script>', "#GGGGGG", "javascript:alert(1)"]
        self.assertEqual(generate_avatar_svg("Galactus", colors=bad), generate_avatar_svg("Galactus", colors=DEFAULT_COLORS))

    def test_custom_palette_is_used(self):
        svg = generate_avatar_svg("Galactus", variant="marble", colors=["#111111", "#222222", "#333333"])
        self.assertTrue(set(re.findall(r"#[0-9A-F]{6}", svg.upper())) <= {"#111111", "#222222", "#333333", "#FFFFFF"})

    def test_ids_are_unique_per_variant_so_inlining_several_does_not_collide(self):
        ids = {re.search(r'<mask id="([^"]+)"', generate_avatar_svg("Galactus", variant=v)).group(1) for v in VARIANTS}
        self.assertEqual(len(ids), len(VARIANTS))


if __name__ == "__main__":
    unittest.main()
