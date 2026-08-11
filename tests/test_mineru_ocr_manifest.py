"""全量 OCR 清单测试：验证书籍隔离、稳定编号和 PDF 分片映射。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from scripts.mineru_ocr.manifest import (
    build_manifest,
    materialize_pending_chunks,
    save_manifest_atomic,
)


def _write_pdf(path: Path, page_count: int) -> None:
    """创建指定页数的最小 PDF，供分片测试使用。"""
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=100, height=100)
    with path.open("wb") as stream:
        writer.write(stream)


class ManifestTests(unittest.TestCase):
    def test_build_manifest_uses_stable_book_ids_and_contiguous_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            math_dir = root / "math_text"
            output_dir = root / "ocr_text"
            math_dir.mkdir()
            _write_pdf(math_dir / "B书.pdf", 181)
            _write_pdf(math_dir / "A书.pdf", 2)

            first = build_manifest(math_dir, output_dir, max_pages=180)
            save_manifest_atomic(output_dir / "manifest.json", first)
            second = build_manifest(math_dir, output_dir, max_pages=180)

        self.assertEqual(
            [book["book_id"] for book in first["books"]],
            [
                "01_A书",
                "02_B书",
            ],
        )
        self.assertEqual(
            [book["book_id"] for book in second["books"]],
            [book["book_id"] for book in first["books"]],
        )
        chunks = first["books"][1]["chunks"]
        self.assertEqual(
            [(item["page_start"], item["page_end"], item["page_count"]) for item in chunks],
            [(1, 180, 180), (181, 181, 1)],
        )
        self.assertEqual(chunks[0]["data_id"], "02_B书__0001")

    def test_materialize_chunks_writes_expected_page_counts_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            math_dir = root / "math_text"
            output_dir = root / "ocr_text"
            math_dir.mkdir()
            _write_pdf(math_dir / "测试教材.pdf", 3)
            manifest = build_manifest(math_dir, output_dir, max_pages=2)

            updated = materialize_pending_chunks(manifest, output_dir)
            chunk_paths = [Path(item["pdf_path"]) for item in updated["books"][0]["chunks"]]

            self.assertEqual([len(PdfReader(str(path)).pages) for path in chunk_paths], [2, 1])
            self.assertTrue(all(item["sha256"] for item in updated["books"][0]["chunks"]))
            self.assertTrue(
                all(item["status"] == "prepared" for item in updated["books"][0]["chunks"])
            )
            self.assertTrue(all(path.is_file() for path in chunk_paths))

    def test_atomic_save_writes_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nested" / "manifest.json"
            save_manifest_atomic(path, {"schema_version": 1, "books": []})
            loaded = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded["schema_version"], 1)
        self.assertEqual(loaded["books"], [])


if __name__ == "__main__":
    unittest.main()
