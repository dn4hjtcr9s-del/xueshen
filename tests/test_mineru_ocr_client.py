"""MinerU API 客户端测试：覆盖安全上传、批次状态持久化与结果解压。"""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.mineru_ocr.client import (
    MinerUClient,
    load_api_key,
    safe_extract_zip,
    validate_result_files,
)


class MinerUClientTest(unittest.TestCase):
    """验证客户端不会泄露密钥，并能正确构造 MinerU v4 请求。"""

    def test_load_api_key_from_project_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("OTHER=1\nMinerU_API='secret-token'\n", encoding="utf-8")
            self.assertEqual(load_api_key(root), "secret-token")

    @patch("scripts.mineru_ocr.client.api_request")
    def test_submit_batch_uses_vlm_configuration_and_unique_names(self, request) -> None:
        request.return_value = (
            200,
            {"code": 0, "data": {"batch_id": "batch-1", "file_urls": ["https://u/1"]}},
        )
        client = MinerUClient("secret-token")
        chunks = [
            {
                "data_id": "01_book__0001",
                "pdf_path": "/tmp/upload.pdf",
                "page_count": 12,
            }
        ]

        batch = client.submit_batch(chunks)

        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["model_version"], "vlm")
        self.assertEqual(payload["language"], "ch")
        self.assertTrue(payload["enable_formula"])
        self.assertTrue(payload["enable_table"])
        self.assertEqual(
            payload["files"],
            [{"name": "01_book__0001.pdf", "is_ocr": True, "data_id": "01_book__0001"}],
        )
        self.assertEqual(batch["batch_id"], "batch-1")
        self.assertEqual(batch["data_ids"], ["01_book__0001"])

    @patch("scripts.mineru_ocr.client.http.client.HTTPSConnection")
    def test_upload_presigned_file_omits_content_type(self, connection_class) -> None:
        response = MagicMock(status=200)
        response.read.return_value = b""
        connection_class.return_value.getresponse.return_value = response
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "part.pdf"
            pdf.write_bytes(b"pdf-data")
            client = MinerUClient("secret-token")
            client.upload_presigned_file("https://example.invalid/upload?Signature=x", pdf)

        header_names = [call.args[0].lower() for call in connection_class.return_value.putheader.call_args_list]
        self.assertNotIn("content-type", header_names)
        self.assertIn("content-length", header_names)

    def test_persist_batch_does_not_write_api_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "batch.json"
            client = MinerUClient("secret-token")
            client.persist_batch(
                path,
                {
                    "batch_id": "batch-1",
                    "state": "uploading",
                    "file_urls": ["https://example.invalid/upload"],
                },
            )
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("secret-token", text)
            self.assertEqual(json.loads(text)["batch_id"], "batch-1")


class ResultArchiveTest(unittest.TestCase):
    """验证 ZIP 路径安全和 MinerU 必需结果文件门禁。"""

    def test_safe_extract_zip_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "bad.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../escape.txt", "bad")
            with self.assertRaisesRegex(ValueError, "路径穿越"):
                safe_extract_zip(archive_path, root / "result")
            self.assertFalse((root / "escape.txt").exists())

    def test_validate_result_files_accepts_complete_vlm_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            (raw / "full.md").write_text("# 文本", encoding="utf-8")
            (raw / "layout.json").write_text("[]", encoding="utf-8")
            (raw / "abc_content_list.json").write_text("[]", encoding="utf-8")
            (raw / "abc_content_list_v2.json").write_text("[]", encoding="utf-8")
            summary = validate_result_files(raw)
            self.assertTrue(summary["valid"])
            self.assertEqual(summary["content_list"], "abc_content_list.json")
            self.assertEqual(summary["content_list_v2"], "abc_content_list_v2.json")

    def test_validate_result_files_reports_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = validate_result_files(Path(tmp))
            self.assertFalse(summary["valid"])
            self.assertIn("full.md", summary["missing"])
            self.assertIn("layout.json", summary["missing"])


if __name__ == "__main__":
    unittest.main()
