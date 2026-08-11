"""按书籍合并 MinerU 结果的测试：确保页码、图片和结构化记录不串书。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.mineru_ocr.merge import merge_all_books, merge_book, validate_chunk_result


def write_chunk(
    raw_dir: Path, *, page_count: int, image_name: str, text: str, page_offset: int = 0
) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "full.md").write_text(text + "\n", encoding="utf-8")
    (raw_dir / "layout.json").write_text(json.dumps({"pdf_info": []}), encoding="utf-8")
    content = [
        {"type": "text", "text": text, "bbox": [1, 1, 10, 10], "page_idx": 0},
        {
            "type": "equation",
            "text": "$$ x_1 + 2 = 3 $$",
            "text_format": "latex",
            "bbox": [1, 10, 20, 20],
            "page_idx": 0,
        },
        {
            "type": "table",
            "table_body": "<table><tr><td>A</td></tr></table>",
            "bbox": [1, 20, 30, 30],
            "page_idx": 0,
        },
    ]
    (raw_dir / "part_content_list.json").write_text(
        json.dumps(content, ensure_ascii=False), encoding="utf-8"
    )
    v2 = [
        [
            {
                "type": "paragraph",
                "content": {"paragraph_content": [{"type": "text", "content": text}]},
                "bbox": [1, 1, 10, 10],
            },
            {
                "type": "equation_interline",
                "content": {
                    "math_content": "x_1 + 2 = 3",
                    "math_type": "latex",
                    "image_source": {"path": f"images/{image_name}"},
                },
                "bbox": [1, 10, 20, 20],
            },
            {
                "type": "table",
                "content": {"html": "<table><tr><td>A</td></tr></table>"},
                "bbox": [1, 20, 30, 30],
            },
        ]
    ]
    while len(v2) < page_count:
        v2.append([])
    (raw_dir / "part_content_list_v2.json").write_text(
        json.dumps(v2, ensure_ascii=False), encoding="utf-8"
    )
    (raw_dir / "images").mkdir(exist_ok=True)
    (raw_dir / "images" / image_name).write_bytes(b"image")


class MergeTest(unittest.TestCase):
    """验证书籍级产物的页码映射、分片拼接和跨书隔离。"""

    def test_validate_chunk_result_maps_page_idx_and_checks_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_chunk(root / "raw", page_count=2, image_name="a.jpg", text="第一页")
            chunk = {
                "book_id": "01_book",
                "book_name": "书",
                "chunk_id": "c1",
                "page_start": 5,
                "page_end": 6,
            }
            result = validate_chunk_result(chunk, root)
            self.assertTrue(result["valid"])
            self.assertEqual(result["pages"][0]["source_page"], 5)
            self.assertEqual(result["pages"][1]["source_page"], 6)

    def test_merge_book_keeps_page_order_and_renames_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            book_dir = output / "01_book"
            chunks = []
            for number, start in enumerate((1, 3), start=1):
                chunk_dir = book_dir / "chunks" / f"c{number}"
                write_chunk(
                    chunk_dir / "raw", page_count=2, image_name="same.jpg", text=f"书页{start}"
                )
                chunks.append(
                    {
                        "chunk_id": f"c{number}",
                        "chunk_dir": str(chunk_dir),
                        "page_start": start,
                        "page_end": start + 1,
                    }
                )
            book = {
                "book_id": "01_book",
                "book_name": "书一",
                "source_filename": "book.pdf",
                "page_count": 4,
                "chunks": chunks,
            }
            summary = merge_book(book, output)
            self.assertEqual(summary["status"], "merged")
            records = [
                json.loads(line)
                for line in (book_dir / "content_list.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                [r["source_page"] for r in records if r["element_type"] == "text"], [1, 3]
            )
            images = sorted((book_dir / "images").glob("*"))
            self.assertEqual(len(images), 2)
            self.assertTrue(all("c" in image.name for image in images))

    def test_merge_all_books_does_not_mix_books(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            books = []
            for book_id, text in (("01_a", "甲书"), ("02_b", "乙书")):
                book_dir = output / book_id
                chunk_dir = book_dir / "chunks" / "c1"
                write_chunk(chunk_dir / "raw", page_count=1, image_name=f"{book_id}.jpg", text=text)
                books.append(
                    {
                        "book_id": book_id,
                        "book_name": book_id,
                        "source_filename": f"{book_id}.pdf",
                        "page_count": 1,
                        "chunks": [
                            {
                                "chunk_id": "c1",
                                "chunk_dir": str(chunk_dir),
                                "page_start": 1,
                                "page_end": 1,
                            }
                        ],
                    }
                )
            summary = merge_all_books({"books": books}, output)
            self.assertEqual(summary["merged_books"], 2)
            for book_id, other in (("01_a", "乙书"), ("02_b", "甲书")):
                text = (output / book_id / "full.md").read_text(encoding="utf-8")
                self.assertNotIn(other, text)
                for line in (output / book_id / "content_list.jsonl").read_text().splitlines():
                    self.assertEqual(json.loads(line)["book_id"], book_id)


if __name__ == "__main__":
    unittest.main()
