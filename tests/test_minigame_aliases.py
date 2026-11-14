from __future__ import annotations

import unittest

from commands.minigame import answer_matches_player, accepted_player_answers


class GuessThePlayerAliasTests(unittest.TestCase):
    def assert_aliases_match(self, card_names: tuple[str, ...], aliases: tuple[str, ...]) -> None:
        for card_name in card_names:
            for alias in aliases:
                with self.subTest(card=card_name, alias=alias):
                    self.assertTrue(answer_matches_player(alias, card_name))
                    self.assertTrue(answer_matches_player(alias.upper(), card_name))
                    self.assertTrue(answer_matches_player(alias.lower(), card_name))
                    self.assertTrue(answer_matches_player(f"  {alias}  ", card_name))

    def test_chicken_aliases_match_only_chicken(self) -> None:
        aliases = (
            "Chicken",
            "chicken",
            "Chicken Calamari",
            "Calamari",
            "calamari",
        )
        self.assert_aliases_match(("Chicken",), aliases)
        self.assertFalse(answer_matches_player("Calamari", "Megan"))

    def test_road_aliases_match_gold_toty_and_tots_versions(self) -> None:
        card_names = (
            "Road",
            "Road (Gold 86)",
            "Road (S1 TOTY 98)",
            "Road (S1 TOTS 98)",
        )
        self.assert_aliases_match(card_names, ("Road", "road"))

    def test_licha_aliases_match_all_card_versions(self) -> None:
        self.assert_aliases_match(
            ("Licha", "Licha (Gold 90)", "Licha (S2 TOTS 96)"),
            ("Licha", "licha"),
        )

    def test_cam_aliases_match_gold_and_toty_versions(self) -> None:
        self.assert_aliases_match(
            ("Cam", "Cam (Gold 90)", "Cam (S1 TOTY 97)"),
            ("Cam", "can", "cameronvikes2"),
        )

    def test_ebs_aliases_match_gold_toty_and_tots_versions(self) -> None:
        self.assert_aliases_match(
            ("Ebs", "Ebs (S1 TOTY 98)", "Ebs (S1 TOTS 98)", "EBS (S2 TOTS 99)"),
            ("EBS", "ebs", "Ebs"),
        )

    def test_future_stars_aliases_match_new_cards(self) -> None:
        for card_name, base_name in (
            ("HHH (S1 FUTURE STARS 89)", "HHH"),
            ("Lauren (S1 FUTURE STARS 93)", "Lauren"),
            ("Guzzinga (S1 FUTURE STARS 92)", "Guzzinga"),
        ):
            with self.subTest(card=card_name):
                self.assertTrue(answer_matches_player(base_name, card_name))
                self.assertTrue(answer_matches_player(card_name, card_name))

    def test_normal_names_match_with_case_and_outer_whitespace(self) -> None:
        for name in ("Apex", "Megan", "Frozend", "Sluckz"):
            with self.subTest(name=name):
                self.assertTrue(answer_matches_player(name, name))
                self.assertTrue(answer_matches_player(name.upper(), name))
                self.assertTrue(answer_matches_player(name.lower(), name))
                self.assertTrue(answer_matches_player(f"  {name}  ", name))

    def test_answer_comparison_remains_exact_not_partial(self) -> None:
        for answer in ("roadster", "road 98", "chick", "not calamari", "cammy"):
            with self.subTest(answer=answer):
                self.assertFalse(answer_matches_player(answer, "Road"))
                self.assertFalse(answer_matches_player(answer, "Chicken"))
                self.assertFalse(answer_matches_player(answer, "Cam"))

    def test_registered_answers_are_normalized_and_card_specific(self) -> None:
        self.assertEqual(
            accepted_player_answers("Road (S1 TOTY 98)"),
            frozenset({"road", "road (s1 toty 98)"}),
        )
        self.assertIn("cameronvikes2", accepted_player_answers("Cam"))
        self.assertNotIn("cam", accepted_player_answers("Megan"))


if __name__ == "__main__":
    unittest.main()
