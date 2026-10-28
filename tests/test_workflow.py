"""The workflow's schedule: two slots an hour, clear of the external :00 dispatch."""
import pathlib
import re
import unittest

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "hazemy.yml"


def schedule_minutes():
    crons = re.findall(r"^\s*-\s*cron:\s*'([^']+)'", WORKFLOW.read_text(), re.MULTILINE)
    minutes = []
    for cron in crons:
        fields = cron.split()
        if fields[1:] != ["*", "*", "*", "*"]:
            raise AssertionError(f"Cron {cron!r} is not hourly; expected 'M,M * * * *'.")
        minutes.extend(int(m) for m in fields[0].split(","))
    return sorted(minutes)


class WorkflowScheduleTest(unittest.TestCase):
    def test_two_slots_an_hour_30_minutes_apart(self):
        minutes = schedule_minutes()
        self.assertEqual(len(minutes), 2)
        self.assertEqual(minutes[1] - minutes[0], 30)

    def test_slots_keep_clear_of_the_dispatch_at_00(self):
        for minute in schedule_minutes():
            self.assertGreaterEqual(minute % 30, 5)
            self.assertLessEqual(minute % 30, 25)

    def test_manual_dispatch_is_still_allowed(self):
        self.assertIn("workflow_dispatch:", WORKFLOW.read_text())


if __name__ == "__main__":
    unittest.main()
