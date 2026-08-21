"""OpenCV Zoo（Apache-2.0）由来の第三者コード。

出典: https://github.com/opencv/opencv_zoo
  models/person_detection_mediapipe/mp_persondet.py
  models/pose_estimation_mediapipe/mp_pose.py

MediaPipe BlazePose の前処理・後処理は仕様が細かく（SSDアンカー、ROIの回転、
ランドマークの逆回転）、書き直すと取得元との差異が事故になる。上流の実装を
そのまま置き、変更は加えない。
"""
