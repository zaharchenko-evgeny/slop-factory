import unittest

from ui.sources import Source


class TicketUrlTest(unittest.TestCase):
    def test_ticket_url_table(self):
        s = Source(("github-issues", "o/r"), ["engine"], object(), "o/r")
        no_repo = Source(("x", "y"), [], object())
        cases = [
            (s, "o/other#7", "https://github.com/o/other/issues/7"),
            (s, "o/r#7", "https://github.com/o/r/issues/7"),
            (s, "#7", "https://github.com/o/r/issues/7"),
            (s, "7", "https://github.com/o/r/issues/7"),
            (s, 7, "https://github.com/o/r/issues/7"),
            (s, "o/other#x", None),
            (s, "o/other#", None),
            (s, "abc", None),
            (no_repo, "7", None),
            (no_repo, "o/other#7", "https://github.com/o/other/issues/7"),
        ]
        for source, ticket, want in cases:
            with self.subTest(ticket=ticket):
                self.assertEqual(source.ticket_url(ticket), want)


if __name__ == "__main__":
    unittest.main()
