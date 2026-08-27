"""Lambda アダプタ: SQS 経由で S3 の PDF アップロードを受け、.txt バケットへ書き出す。

経路: S3(ObjectCreated) → SNS → SQS → この Lambda（イベントソースマッピング）。
変換ロジックは pdf_to_text_local に委譲し、ここは S3 入出力とメッセージ処理だけを担う。

環境変数: OUTPUT_BUCKET（必須） / OUTPUT_PREFIX / OCR_MODE / OCR_DPI

リトライ方針: BatchSize=1（1 メッセージ = 1 PDF）。例外はそのまま送出し、SQS が
そのメッセージを再配信、QueueMaxReceiveCount を超えたら DLQ へ。変換は「同じキーへ
上書き」で冪等なので、再配信で再変換されても害はない。
"""

from __future__ import annotations

import json
import os
import traceback
import urllib.parse
from pathlib import Path

import boto3

import pdf_to_text_local as core

_TMP = Path("/tmp")
_S3 = boto3.client("s3")

OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "")
OCR_MODE = os.environ.get("OCR_MODE", "auto")
OCR_DPI = int(os.environ.get("OCR_DPI", core.DEFAULT_OCR_DPI))


def handler(event: dict, context=None) -> None:
    try:
        _execute(event)
    except Exception:
        # SQS がメッセージを再配信、上限超で DLQ
        traceback.print_exc()
        raise


def _execute(event: dict) -> None:
    # 1. SQS メッセージから対象オブジェクトを取り出す（S3 通知は 1 レコード前提で先頭だけ）
    record = json.loads(event["Records"][0]["body"])["Records"][0]
    src_bucket = record["s3"]["bucket"]["name"]
    src_key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

    # 2. PDF 以外は無視（S3 側でも suffix フィルタ済みだが念のため）
    if not src_key.lower().endswith(".pdf"):
        print(f"skip (not pdf): s3://{src_bucket}/{src_key}")
        return

    local_pdf = _TMP / Path(src_key).name
    local_txt = local_pdf.with_suffix(".txt")
    try:
        # 3. PDF を取得してテキスト化する
        _S3.download_file(src_bucket, src_key, str(local_pdf))
        pages = core.convert(local_pdf, ocr_mode=OCR_MODE, dpi=OCR_DPI)
        local_txt.write_text(core.render_text(pages), encoding="utf-8")

        # 4. 出力バケットへアップロードする
        dst_key = _upload(local_txt, src_key)
        print(
            f"ok: s3://{src_bucket}/{src_key} -> s3://{OUTPUT_BUCKET}/{dst_key} "
            f"({len(pages)}p, OCR {core.ocr_pages(pages)})"
        )
    finally:
        # 5. /tmp を片付ける（実行環境が使い回されるため）
        for p in (local_pdf, local_txt):
            p.unlink(missing_ok=True)


def _upload(local_txt: Path, src_key: str) -> str:
    """テキストを出力バケットへ置き、その出力キー（拡張子を .txt に変えたもの）を返す。"""
    dst_key = f"{OUTPUT_PREFIX}{src_key.rsplit('.', 1)[0]}.txt"
    _S3.upload_file(
        str(local_txt),
        OUTPUT_BUCKET,
        dst_key,
        ExtraArgs={"ContentType": "text/plain; charset=utf-8"},
    )
    return dst_key
