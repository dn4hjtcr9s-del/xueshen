"""OCR 编排器测试：覆盖批量限制、断点恢复和已完成分片跳过。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.mineru_ocr.runner import OCRRunner, find_chunks, select_runnable_chunks


class FakeClient:
    """测试专用客户端，不访问网络。"""

    def __init__(self) -> None:
        self.submitted: list[list[str]] = []

    def submit_batch(self, chunks):
        ids = [chunk["data_id"] for chunk in chunks]
        self.submitted.append(ids)
        return {"batch_id": "batch-1", "state": "created", "data_ids": ids, "file_urls": [f"https://upload/{item}" for item in ids], "request": {}, "last_results": []}

    def persist_batch(self, path, batch):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(batch), encoding="utf-8")

    def upload_batch(self, batch, chunks, on_uploaded=None):
        for chunk in chunks:
            if on_uploaded:
                on_uploaded(chunk)

    def poll_batch(self, batch_id, interval_seconds=0, timeout_seconds=1, on_poll=None):
        results = [{"data_id": data_id, "state": "done", "full_zip_url": "https://result"} for data_id in self.submitted[-1]]
        if on_poll:
            on_poll(results)
        return results


def make_manifest(statuses: list[str]) -> dict:
    chunks = []
    for index, status in enumerate(statuses, start=1):
        chunks.append({
            "chunk_id": f"c{index}",
            "data_id": f"book__{index:04d}",
            "pdf_path": f"/tmp/c{index}.pdf",
            "chunk_dir": f"/tmp/c{index}",
            "page_start": index,
            "page_end": index,
            "page_count": 1,
            "status": status,
            "batch_id": None,
            "attempt_count": 0,
            "error": None,
        })
    return {"books": [{"book_id": "book", "book_name": "书", "source_filename": "book.pdf", "page_count": len(chunks), "chunks": chunks}]}


class RunnerTest(unittest.TestCase):
    """验证状态机不会重复提交已完成分片。"""

    def test_select_runnable_chunks_caps_batch_at_40_and_skips_done(self) -> None:
        manifest = make_manifest(["downloaded"] + ["prepared"] * 45)
        selected = select_runnable_chunks(manifest, batch_size=100)
        self.assertEqual(len(selected), 40)
        self.assertNotIn("book__0001", [item["data_id"] for item in selected])

    def test_find_chunks_matches_data_id(self) -> None:
        manifest = make_manifest(["prepared", "downloaded"])
        found = find_chunks(manifest, ["book__0002"])
        self.assertEqual([item["data_id"] for item in found], ["book__0002"])

    @patch("scripts.mineru_ocr.runner.download_and_extract_result")
    def test_run_next_batch_persists_and_marks_downloaded(self, download) -> None:
        download.return_value = {"valid": True}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest = make_manifest(["prepared", "downloaded"])
            runner = OCRRunner(output, manifest, FakeClient(), poll_interval_seconds=0, poll_timeout_seconds=1)
            count = runner.run_next_batch(batch_size=40)
            self.assertEqual(count, 1)
            self.assertEqual(manifest["books"][0]["chunks"][0]["status"], "downloaded")
            self.assertEqual(manifest["books"][0]["chunks"][1]["status"], "downloaded")
            jobs = list((output / "_jobs").glob("*.json"))
            self.assertEqual(len(jobs), 1)
            self.assertEqual(json.loads(jobs[0].read_text())["state"], "done")

    @patch("scripts.mineru_ocr.runner.download_and_extract_result")
    def test_resume_downloads_remote_done_without_resubmitting(self, download) -> None:
        download.return_value = {"valid": True}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest = make_manifest(["remote_done"])
            chunk = manifest["books"][0]["chunks"][0]
            chunk["batch_id"] = "old-batch"
            job = {
                "batch_id": "old-batch",
                "state": "processing",
                "data_ids": [chunk["data_id"]],
                "file_urls": ["https://expired"],
                "uploaded_data_ids": [chunk["data_id"]],
                "last_results": [{"data_id": chunk["data_id"], "state": "done", "full_zip_url": "https://result"}],
            }
            (output / "_jobs").mkdir(parents=True)
            (output / "_jobs" / "old-batch.json").write_text(json.dumps(job), encoding="utf-8")
            client = FakeClient()
            runner = OCRRunner(output, manifest, client, poll_interval_seconds=0, poll_timeout_seconds=1)
            runner.resume_jobs()
            self.assertEqual(chunk["status"], "downloaded")
            self.assertEqual(client.submitted, [])

    def test_upload_failure_stops_retrying_after_max_attempts(self) -> None:
        class UploadFailClient(FakeClient):
            def upload_batch(self, batch, chunks, on_uploaded=None):
                raise RuntimeError("network down")

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            manifest = make_manifest(["retry"])
            chunk = manifest["books"][0]["chunks"][0]
            chunk["attempt_count"] = 2
            runner = OCRRunner(output, manifest, UploadFailClient(), poll_interval_seconds=0, poll_timeout_seconds=1, max_attempts=3)
            runner.run_next_batch(batch_size=40)
            self.assertEqual(chunk["status"], "error")
            self.assertEqual(select_runnable_chunks(manifest), [])



if __name__ == "__main__":
    unittest.main()
