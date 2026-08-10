"""实验运行器回归测试：确保预签名 PUT 不会注入未参与签名的请求头。"""

import unittest
from unittest.mock import MagicMock, patch

from run_compare import api_request, download_zip, upload_presigned_file


class PresignedUploadHeaderTest(unittest.TestCase):
    """验证签名上传请求可以显式省略 Content-Type。"""

    def test_put_without_content_type_does_not_send_content_type_header(self) -> None:
        response = MagicMock()
        response.status = 200
        response.read.return_value = b""
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch("urllib.request.urlopen", return_value=response) as open_url:
            api_request(
                "PUT",
                "https://example.invalid/upload",
                raw_body=b"pdf",
                content_type=None,
            )
        request = open_url.call_args.args[0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertNotIn("content-type", headers)

    @patch("run_compare.http.client.HTTPSConnection")
    def test_presigned_upload_uses_no_content_type_header(self, connection_class) -> None:
        connection = connection_class.return_value
        response = MagicMock()
        response.status = 200
        response.read.return_value = b""
        connection.getresponse.return_value = response
        path = __import__("pathlib").Path(__file__).with_name("input") / "scan_formula.pdf"

        upload_presigned_file("https://example.invalid/upload?Signature=test", path)

        header_names = [call.args[0].lower() for call in connection.putheader.call_args_list]
        self.assertNotIn("content-type", header_names)

    @patch("run_compare.subprocess.run")
    def test_zip_download_uses_curl_for_signed_cdn_url(self, run_command) -> None:
        from pathlib import Path

        destination = Path("/tmp/mineru-test-result.zip")
        download_zip("https://cdn-mineru.openxlab.org.cn/result.zip?Signature=test", destination)

        command = run_command.call_args.args[0]
        self.assertEqual(command[0], "curl")
        self.assertIn("-L", command)
        self.assertIn(str(destination), command)


if __name__ == "__main__":
    unittest.main()
