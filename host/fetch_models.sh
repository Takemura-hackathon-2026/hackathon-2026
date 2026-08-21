#!/bin/sh
# 姿勢推定モデルを取得する。出典は OpenCV 公式の opencv_zoo（Apache-2.0）。
#
# 親機はインターネットへ常時接続していないため、疎通のある機械でこれを実行し、
# host/models/ を scp で持ち込む運用を想定する。
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)/models"
BASE="https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models"
mkdir -p "$DIR"
curl -fsSL -o "$DIR/person_detection.onnx" \
  "$BASE/person_detection_mediapipe/person_detection_mediapipe_2023mar.onnx"
curl -fsSL -o "$DIR/pose_estimation.onnx" \
  "$BASE/pose_estimation_mediapipe/pose_estimation_mediapipe_2023mar.onnx"
ls -l "$DIR"
