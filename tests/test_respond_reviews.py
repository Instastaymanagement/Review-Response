import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import respond_reviews as r  # noqa: E402

TODAY = dt.date(2026, 10, 15)


def fields(**kw):
    base = {"Overall Rating": "5", "Host Responded": "", "Review ID": "abc", "Review Date": "2026-10-10"}
    base.update(kw)
    return base


class CandidateTests(unittest.TestCase):
    def test_recent_unanswered_five_star(self):
        self.assertTrue(r.is_candidate(fields(), TODAY, 30))

    def test_not_five_stars(self):
        self.assertFalse(r.is_candidate(fields(**{"Overall Rating": "4"}), TODAY, 30))
        self.assertFalse(r.is_candidate(fields(**{"Overall Rating": ""}), TODAY, 30))

    def test_already_responded(self):
        self.assertFalse(r.is_candidate(fields(**{"Host Responded": "v"}), TODAY, 30))

    def test_too_old(self):
        self.assertTrue(r.is_candidate(fields(**{"Review Date": "2026-09-15"}), TODAY, 30))
        self.assertFalse(r.is_candidate(fields(**{"Review Date": "2026-09-14"}), TODAY, 30))

    def test_missing_review_id_or_date(self):
        self.assertFalse(r.is_candidate(fields(**{"Review ID": ""}), TODAY, 30))
        self.assertFalse(r.is_candidate(fields(**{"Review Date": ""}), TODAY, 30))


class PromptTests(unittest.TestCase):
    review = {
        "platform": "airbnb",
        "public": {"rating": 5, "review": "Spotless and close to Fenway."},
        "private": {"feedback": "SECRET: the washer is broken"},
        "guest": {"first_name": "Sue", "last_name": "Reynolds"},
    }

    def test_private_feedback_never_sent_to_model(self):
        prompt = r.build_prompt(self.review, "Brookline", ["Thanks, Bob!"])
        self.assertNotIn("SECRET", prompt)
        self.assertNotIn("Reynolds", prompt)
        self.assertIn("Sue", prompt)
        self.assertIn("Spotless and close to Fenway.", prompt)
        self.assertIn("<example>Thanks, Bob!</example>", prompt)
        self.assertIn("City of the stay: Brookline", prompt)

    def test_rating_only_review(self):
        review = {**self.review, "public": {"rating": 5, "review": ""}}
        self.assertIn("without writing a review", r.build_prompt(review, "P", []))

    def test_clean_reply_strips_quotes_and_caps_length(self):
        self.assertEqual(r.clean_reply('  "Thanks, Sue!"  '), "Thanks, Sue!")
        long = ("Great stay. " * 200).strip()
        cleaned = r.clean_reply(long)
        self.assertLessEqual(len(cleaned), r.RESPONSE_LIMIT)
        self.assertTrue(cleaned.endswith("."))


class StyleExampleTests(unittest.TestCase):
    def test_uses_newest_existing_replies(self):
        reviews = {
            str(i): {"review": {"reviewed_at": f"2026-10-0{i}", "public": {"review": "x", "response": f"reply {i}"}}}
            for i in range(1, 7)
        }
        reviews["none"] = {"review": {"reviewed_at": "2026-10-09", "public": {"review": "x", "response": None}}}
        self.assertEqual(r.style_examples(reviews, limit=3), ["reply 6", "reply 5", "reply 4"])


if __name__ == "__main__":
    unittest.main()
