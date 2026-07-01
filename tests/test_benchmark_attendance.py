from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class BenchmarkAttendanceTests(unittest.TestCase):
    def test_exits_with_error_when_threshold_is_exceeded(self) -> None:
        import benchmark_attendance

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "arquivo.pdf"
            input_path.write_bytes(b"%PDF-1.4 fake")

            with mock.patch("benchmark_attendance.inspect_pdf_document", return_value=(2, "application/pdf")), \
                mock.patch("benchmark_attendance.process_attendance_list", return_value=12), \
                mock.patch("benchmark_attendance.select_processing_profile") as profile_mock, \
                mock.patch("benchmark_attendance.time.perf_counter", side_effect=[0.0, 12.5]), \
                mock.patch("sys.argv", ["benchmark_attendance.py", str(input_path), "--assert-max-seconds", "10"]):
                profile_mock.return_value = SimpleNamespace(name="medium")
                result = benchmark_attendance.main()

        self.assertEqual(1, result)

    def test_prints_profile_and_succeeds_within_threshold(self) -> None:
        import benchmark_attendance

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "arquivo.pdf"
            input_path.write_bytes(b"%PDF-1.4 fake")
            stdout = io.StringIO()

            with mock.patch("benchmark_attendance.inspect_pdf_document", return_value=(2, "application/pdf")), \
                mock.patch("benchmark_attendance.process_attendance_list", return_value=12), \
                mock.patch("benchmark_attendance.select_processing_profile") as profile_mock, \
                mock.patch("benchmark_attendance.time.perf_counter", side_effect=[0.0, 2.5]), \
                mock.patch("sys.argv", ["benchmark_attendance.py", str(input_path), "--assert-max-seconds", "10"]):
                profile_mock.return_value = SimpleNamespace(name="medium")
                with redirect_stdout(stdout):
                    result = benchmark_attendance.main()

        self.assertEqual(0, result)
        output = stdout.getvalue()
        self.assertIn("profile=medium", output)
        self.assertIn("tempo_total_s=2.50", output)


if __name__ == "__main__":
    unittest.main()
