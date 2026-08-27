#!/usr/bin/env python3
"""PDF をテキスト化する（ローカル完結・AWS 非依存）。

ページ単位で PyMuPDF のテキストレイヤーを抽出し、薄いページ（≒スキャン画像）だけ
RapidOCR（PP-OCRv4 / 日本語, onnxruntime・CPU）で OCR する。

    python pdf_to_text_local.py 入力.pdf [-o 出力.txt] [--ocr auto|always|never] [--dpi 200]

Lambda アダプタ（pdf_to_text_lambda.py）はこのモジュールの convert() / render_text() を
import して使う。ここは CLI 依存を持たず、OCR 関数も注入できる。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import pymupdf
except ImportError:
    sys.exit("PyMuPDF が必要です: pip install -r requirements.txt")


# --- 設定値 -------------------------------------------------------------

# 1 ページの可視文字数（空白類を除く）がこれ未満なら、テキストレイヤーが
# 無い＝スキャン画像とみなして OCR に回す（--ocr auto のとき）。
MIN_CHARS_PER_PAGE = 50
# OCR 用にページを画像化するときの解像度。上げると精度は増すが遅く・重くなる。
DEFAULT_OCR_DPI = 200
# 可視文字数のカウントで無視する空白類（半角/タブ/改行/全角スペース）。
_BLANKS = " \t\r\n　"


# --- データ -------------------------------------------------------------


@dataclass
class PageResult:
    """1 ページの変換結果。"""

    number: int  # 1 始まりのページ番号
    text: str  # 抽出または OCR したテキスト
    used_ocr: bool  # OCR にフォールバックしたか


def ocr_pages(pages: list[PageResult]) -> list[int]:
    """OCR にフォールバックしたページ番号の一覧。"""
    return [p.number for p in pages if p.used_ocr]


# --- OCR エンジン（convert() に注入する既定実装） -----------------------

_ocr_engine = None


def get_ocr_engine():
    """RapidOCR エンジンを遅延生成して使い回す（初回だけモデルを読み込む）。"""
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
        except ImportError:
            sys.exit("rapidocr が必要です: pip install -r requirements.txt")
        # 検出・分類は既定（中国語モデルで言語非依存）、認識だけ日本語 PP-OCRv4 mobile。
        _ocr_engine = RapidOCR(
            params={
                "Det.ocr_version": OCRVersion.PPOCRV4,
                "Det.model_type": ModelType.MOBILE,
                "Cls.model_type": ModelType.MOBILE,
                "Rec.ocr_version": OCRVersion.PPOCRV4,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.lang_type": LangRec.JAPAN,
            }
        )
    return _ocr_engine


def ocr_page(page: pymupdf.Page, dpi: int) -> str:
    """1 ページを画像化して OCR し、認識テキストを返す。convert() の ocr_fn 既定値。"""
    import numpy as np

    # ページを指定 DPI でラスタライズし、RapidOCR が受け取れる BGR 配列に変換する。
    pix = page.get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    img = img[:, :, :3][:, :, ::-1]  # RGB(A) -> BGR

    # RapidOCR の戻り値は (行テキスト, 座標, 信頼度) の集合。行テキストだけ連結する。
    result = get_ocr_engine()(img)
    return "\n".join(result.txts) if result and result.txts else ""


# --- コア処理 ---------------------------------------------------------


def process_page(page: pymupdf.Page, number: int, ocr_mode: str, dpi: int, ocr_fn) -> PageResult:
    """1 ページを PageResult に変換する。方針とテキスト量から OCR 要否を決める。"""
    layer_text = page.get_text().strip()
    thin = sum(ch not in _BLANKS for ch in layer_text) < MIN_CHARS_PER_PAGE

    # always は無条件、auto はレイヤーが薄いときだけ OCR。never は常にレイヤーを使う。
    if ocr_mode == "always" or (ocr_mode == "auto" and thin):
        return PageResult(number, ocr_fn(page, dpi), used_ocr=True)
    return PageResult(number, layer_text, used_ocr=False)


def convert(
    pdf_path: Path,
    ocr_mode: str = "auto",
    dpi: int = DEFAULT_OCR_DPI,
    ocr_fn=ocr_page,
) -> list[PageResult]:
    """PDF を開き、全ページを process_page() に通して結果を返す。

    ocr_fn を差し替えれば RapidOCR 以外の OCR やテスト用ダミーを注入できる。
    """
    with pymupdf.open(pdf_path) as doc:
        return [
            process_page(page, number, ocr_mode, dpi, ocr_fn)
            for number, page in enumerate(doc, start=1)
        ]


def render_text(pages: list[PageResult], page_headers: bool = True) -> str:
    """ページ列を 1 本のテキストへ整形する（CLI / Lambda 共用）。"""
    blocks = []
    for p in pages:
        body = p.text.strip()
        blocks.append(f"## Page {p.number}\n\n{body}" if page_headers else body)
    return "\n\n".join(blocks).strip() + "\n"


# --- CLI -------------------------------------------------------------


def main() -> None:
    # 1. 引数を定義して解釈する
    parser = argparse.ArgumentParser(description="PDF をテキストに変換する")
    parser.add_argument("pdf_path", type=Path)
    parser.add_argument("-o", "--output", type=Path, default=None)
    parser.add_argument("--ocr", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--dpi", type=int, default=DEFAULT_OCR_DPI)
    parser.add_argument("--no-page-headers", action="store_true")
    args = parser.parse_args()

    # 2. 入力 PDF の存在を確認する
    if not args.pdf_path.exists():
        sys.exit(f"ファイルが見つかりません: {args.pdf_path}")

    # 3. ページ単位で変換する
    pages = convert(args.pdf_path, ocr_mode=args.ocr, dpi=args.dpi)

    # 4. テキストへ整形してファイルへ書き出す（出力先は -o 省略時は同名 .txt）
    output_path = args.output or args.pdf_path.with_suffix(".txt")
    output_path.write_text(render_text(pages, not args.no_page_headers), encoding="utf-8")

    # 5. 変換結果を報告する（OCR したページがあれば番号も）
    ocr = ocr_pages(pages)
    print(f"完了: {len(pages)}ページ → {output_path}" + (f"（OCR {ocr}）" if ocr else ""))


if __name__ == "__main__":
    main()
