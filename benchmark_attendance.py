from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from attendance_pipeline import inspect_pdf_document, process_attendance_list, select_processing_profile


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark rapido para OCR de lista de presenca.")
    parser.add_argument("input_pdf", help="Caminho do PDF de entrada")
    parser.add_argument("--lang", default="por", help="Idioma para OCR")
    parser.add_argument("--assert-max-seconds", type=float, default=0.0, help="Limite maximo de tempo")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input_pdf)
    if not input_path.exists():
        print(f"erro=arquivo_nao_encontrado path={input_path}")
        return 1

    page_count, mime_type = inspect_pdf_document(input_path)
    file_size_bytes = input_path.stat().st_size
    profile = select_processing_profile(input_path, page_count, file_size_bytes)

    output_path = input_path.with_name(f"{input_path.stem}_benchmark.xlsx")

    t0 = time.perf_counter()
    rows = process_attendance_list(input_path, output_path, args.lang)
    elapsed = time.perf_counter() - t0

    print(
        f"profile={profile.name} pages={page_count} mime={mime_type} "
        f"rows={rows} tempo_total_s={elapsed:.2f}"
    )

    if args.assert_max_seconds and elapsed > args.assert_max_seconds:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
