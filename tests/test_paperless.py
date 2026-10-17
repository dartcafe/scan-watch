import unittest

from scan_watch.paperless import task_outcome


class TaskOutcomeTest(unittest.TestCase):
    def test_old_api_plain_list(self):
        self.assertIsNone(task_outcome([{"status": "STARTED"}]))
        self.assertEqual(task_outcome([{"status": "SUCCESS", "related_document": "12", "result": "ok"}]),
                         (True, "12", "ok"))
        self.assertEqual(task_outcome([{"status": "FAILURE", "result": "Duplikat"}]), (False, None, "Duplikat"))

    def test_new_api_paginated(self):
        running = {"count": 1, "results": [{"status": "pending", "result_data": None}]}
        self.assertIsNone(task_outcome(running))
        done = {"count": 1, "results": [{"status": "success", "status_display": "Success",
                                         "result_data": {"document_id": 1452}, "related_document_ids": [1452]}]}
        self.assertEqual(task_outcome(done)[:2], (True, 1452))
        failed = {"count": 1, "results": [{"status": "failure", "result_data": {"error": "Duplikat"},
                                           "related_document_ids": []}]}
        self.assertEqual(task_outcome(failed), (False, None, "Duplikat"))
        revoked = {"count": 1, "results": [{"status": "revoked", "status_display": "Revoked", "result_data": None}]}
        self.assertEqual(task_outcome(revoked), (False, None, "Revoked"))

    def test_task_not_yet_listed(self):
        self.assertIsNone(task_outcome([]))
        self.assertIsNone(task_outcome({"count": 0, "results": []}))


if __name__ == "__main__":
    unittest.main()
