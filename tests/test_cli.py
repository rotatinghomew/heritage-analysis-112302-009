import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.helpers import Book, TempBook, stable_controls

ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, "-m", "street_buffer", *args],
        cwd=cwd, capture_output=True, text=True,
    )


class CliTests(unittest.TestCase):
    def test_estimate_json(self):
        result = run("estimate", "--data", "fixtures")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("总额", payload)
        self.assertIn("经营人", payload)

    def test_review_text_and_json(self):
        text = run("review", "--data", "examples", "--format", "text")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("缓冲通知审核", text.stdout)
        payload = json.loads(run("review", "--data", "examples").stdout)
        self.assertIn("通知审核", payload)
        self.assertIn("汇总", payload)

    def test_merchant_requires_shop(self):
        missing = run("merchant", "--data", "fixtures")
        self.assertNotEqual(missing.returncode, 0)
        ok = run("merchant", "--data", "examples", "--shop", "S1")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        payload = json.loads(ok.stdout)
        self.assertIn("有效依据", payload)

    def test_save_and_reassess_roundtrip(self):
        with TempBook(Book()) as _:
            pass
        book = Book()
        book.series("S1", "O1", ["W1", "W2", "W3"], [1000, 1000, 1000], street="A", phase="pre")
        book.series("S1", "O1", ["W4", "W5"], [400, 500], street="A", phase="P1",
                    detour="yes", notice="N1")
        stable_controls(book)
        book.notice("N1", "S1", "O1", "W4-W5", 1100)
        with TempBook(book) as root:
            saved = Path(root) / "before.json"
            first = run("review", "--data", str(root), "--save", str(saved))
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(saved.exists())
            second = run("reassess", "--data", str(root), "--previous", str(saved))
            self.assertEqual(second.returncode, 0, second.stderr)
            rows = json.loads(second.stdout)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["类型"], "无实质变化")

    def test_scenario_compare(self):
        result = run("scenario", "--data", "examples", "--scenarios", "examples/scenarios.csv")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload["比较"]), 2)
        self.assertTrue(all("预计缓冲占用" in row for row in payload["比较"]))
        self.assertIn("不得据模拟结果直接发放款项", payload["说明"])

    def test_reassess_requires_previous(self):
        result = run("reassess", "--data", "examples")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
