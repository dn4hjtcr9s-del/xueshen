"""命令行入口测试：确保全量 OCR 的五个操作可被稳定解析。"""

import unittest

from scripts.run_mineru_ocr import build_parser


class CLITest(unittest.TestCase):
    def test_supported_commands_and_batch_size(self) -> None:
        parser = build_parser()
        self.assertEqual(parser.parse_args(["prepare"]).command, "prepare")
        self.assertEqual(parser.parse_args(["run", "--batch-size", "32"]).batch_size, 32)
        self.assertEqual(parser.parse_args(["resume"]).command, "resume")
        self.assertEqual(parser.parse_args(["merge"]).command, "merge")
        self.assertTrue(parser.parse_args(["status", "--json"]).as_json)


if __name__ == "__main__":
    unittest.main()
