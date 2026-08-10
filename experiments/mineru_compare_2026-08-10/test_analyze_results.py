"""MinerU 对照结果分析器测试：验证指标统计、表格解析和模型对比行为。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from analyze_results import (
    _build_aggregate,
    build_metrics,
    render_report,
    compare_result_summaries,
    count_numeric_spacing_artifacts,
    summarize_result_directory,
)


class AnalyzeResultsTests(unittest.TestCase):
    def test_counts_broken_numeric_spacing_without_counting_normal_latex_spacing(self) -> None:
        formula = r"$1 . 1 5 + 2 0 2 . 5 + 2 k_1 + \\frac {1} {2}$"

        self.assertEqual(count_numeric_spacing_artifacts(formula), 5)

    def test_counts_latex_spacing_commands_inside_broken_numbers(self) -> None:
        self.assertEqual(count_numeric_spacing_artifacts(r"$1 \ 2 ~ 3$"), 2)

    def test_summarizes_content_images_tables_and_embedding_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result_dir = Path(temp_dir)
            (result_dir / "images").mkdir()
            for name in ("equation.jpg", "table.jpg", "unused.jpg"):
                (result_dir / "images" / name).write_bytes(b"image")
            (result_dir / "full.md").write_text("正文\n\n$$1 . 2 3$$\n", encoding="utf-8")
            content_list = [
                {
                    "type": "text",
                    "text": "正文",
                    "bbox": [0, 0, 10, 10],
                    "page_idx": 0,
                },
                {
                    "type": "equation",
                    "text": "$$1 . 2 3$$",
                    "img_path": "images/equation.jpg",
                    "bbox": [0, 10, 10, 20],
                    "page_idx": 0,
                },
                {
                    "type": "table",
                    "table_body": "<table><tr><td>A</td><td>1</td></tr></table>",
                    "img_path": "images/table.jpg",
                    "bbox": [0, 20, 10, 30],
                    "page_idx": 1,
                },
                {
                    "type": "footer",
                    "text": "页脚",
                    "bbox": [0, 30, 10, 40],
                    "page_idx": 1,
                },
            ]
            (result_dir / "demo_content_list.json").write_text(
                json.dumps(content_list, ensure_ascii=False), encoding="utf-8"
            )
            (result_dir / "demo_content_list_v2.json").write_text(
                json.dumps([[{"type": "paragraph"}], [{"type": "table"}]]),
                encoding="utf-8",
            )
            (result_dir / "layout.json").write_text(
                json.dumps(
                    {
                        "pdf_info": [
                            {
                                "preproc_blocks": [
                                    {
                                        "type": "interline_equation",
                                        "lines": [
                                            {
                                                "spans": [
                                                    {
                                                        "type": "interline_equation",
                                                        "image_path": "equation.jpg",
                                                        "bbox": [0, 10, 10, 20],
                                                    }
                                                ]
                                            }
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            summary = summarize_result_directory(result_dir, expected_page_count=2)

        self.assertEqual(summary["page_count"], 2)
        self.assertEqual(summary["content_type_counts"]["equation"], 1)
        self.assertEqual(summary["equations"]["content_list_image_coverage"], 1.0)
        self.assertEqual(summary["equations"]["layout_equation_count"], 1)
        self.assertEqual(summary["equations"]["layout_image_coverage"], 1.0)
        self.assertEqual(summary["equations"]["numeric_spacing_artifact_count"], 2)
        self.assertEqual(summary["tables"]["row_count"], 1)
        self.assertEqual(summary["tables"]["cell_count"], 2)
        self.assertEqual(summary["embedding_noise"]["block_count"], 1)
        self.assertEqual(summary["images"]["unreferenced_file_count"], 1)
        self.assertEqual(summary["images"]["missing_reference_count"], 0)

    def test_aggregate_ignores_empty_formula_samples_for_coverage_and_similarity(self) -> None:
        def model_summary(equation_count: int, coverage: float) -> dict:
            return {
                "page_count": 1,
                "content_block_count": 1,
                "markdown": {"character_count": 1},
                "equations": {
                    "count": equation_count,
                    "character_count": equation_count * 10,
                    "numeric_spacing_artifact_count": 0,
                    "suspicious_command_count": 0,
                    "content_list_image_coverage": coverage,
                    "layout_image_coverage": coverage,
                    "layout_equation_count": equation_count,
                },
                "tables": {"count": 0},
                "images": {"file_count": 0},
            }

        samples = {
            "with_formula": {
                "models": {
                    "pipeline": model_summary(2, 1.0),
                    "vlm": model_summary(2, 0.0),
                },
                "comparison_pipeline_to_vlm": {
                    "page_count_equal": True,
                    "content_type_counts_equal": True,
                    "formula_pair_count": 2,
                    "formula_similarity_mean": 0.8,
                    "table_pair_count": 0,
                    "table_similarity_mean": 0.0,
                    "plain_text_similarity": 0.9,
                    "numeric_spacing_artifact_delta": 0,
                    "suspicious_command_delta": 0,
                },
            },
            "without_formula": {
                "models": {
                    "pipeline": model_summary(0, 0.0),
                    "vlm": model_summary(0, 0.0),
                },
                "comparison_pipeline_to_vlm": {
                    "page_count_equal": True,
                    "content_type_counts_equal": True,
                    "formula_pair_count": 0,
                    "formula_similarity_mean": 0.0,
                    "table_pair_count": 0,
                    "table_similarity_mean": 0.0,
                    "plain_text_similarity": 0.9,
                    "numeric_spacing_artifact_delta": 0,
                    "suspicious_command_delta": 0,
                },
            },
        }

        aggregate = _build_aggregate(samples)

        self.assertEqual(aggregate["pipeline"]["content_list_equation_image_coverage_mean"], 1.0)
        self.assertEqual(aggregate["comparison"]["formula_similarity_mean"], 0.8)

    def test_report_states_numeric_spacing_caveat_and_table_counterexample(self) -> None:
        experiment_dir = Path(__file__).resolve().parent
        report = render_report(build_metrics(experiment_dir))

        self.assertIn("数字断裂问题没有显著改善", report)
        self.assertIn("A_2:4.5310.219635.575227.0132", report)
        self.assertIn("188-195", report)

    def test_compares_formula_and_table_pairs(self) -> None:
        pipeline = {
            "formula_texts": [r"1 . 2 3 + x _ { 1 }"],
            "table_cell_sequences": [["A", "1 . 2 3"]],
            "plain_text": "一次函数 1 . 2 3",
        }
        vlm = {
            "formula_texts": [r"1.23 + x_{1}"],
            "table_cell_sequences": [["A", "1.23"]],
            "plain_text": "一次函数 1.23",
        }

        comparison = compare_result_summaries(pipeline, vlm)

        self.assertEqual(comparison["formula_pair_count"], 1)
        self.assertEqual(comparison["table_pair_count"], 1)
        self.assertGreater(comparison["formula_similarity_mean"], 0.7)
        self.assertGreater(comparison["table_similarity_mean"], 0.7)
        self.assertGreater(comparison["plain_text_similarity"], 0.9)


if __name__ == "__main__":
    unittest.main()
