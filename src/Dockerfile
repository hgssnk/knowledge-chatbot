# PDF→テキスト変換 Lambda（コンテナイメージ）。
# ローカル依存合計が zip の 250MB 制限を超えるため zip ではなくイメージで配布する。
FROM public.ecr.aws/lambda/python:3.13

# opencv-python-headless / onnxruntime が dlopen する共有ライブラリ。
# （headless でも libGL / libgthread を要求するビルドがあるため入れておく）
RUN dnf install -y --setopt=install_weak_deps=0 mesa-libGL glib2 \
    && dnf clean all

# 依存は先に入れてレイヤーキャッシュを効かせる。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# アプリコード。
COPY pdf_to_text_local.py pdf_to_text_lambda.py ./

# OCR モデルをイメージに焼き込む（Lambda は外向き通信不可。実行時ダウンロードを避ける）。
# get_ocr_engine() が site-packages/rapidocr/models/ に PP-OCRv4 の 3 ファイル（~14MB）を取得する。
# import が通ることの build 時チェックも兼ねる。
RUN python -c "import pdf_to_text_local; pdf_to_text_local.get_ocr_engine()"

CMD ["pdf_to_text_lambda.handler"]
