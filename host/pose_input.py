#!/usr/bin/env python3
"""姿勢推定による人物計測。背景モデルを持たない入力段。

廊下・扉の開閉・通行人・環境光の変動という設置条件では、中央値背景モデルの
前提（背景が静止し照明が一定）が成立しない。ここでは背景差分を使わず、
BlazePose のキーポイントから直接 x/y/bottom/area を出す。

距離不変性: 立ち位置を固定できないため、フレーム幅に対する正規化値だけでは
閾値が距離に依存してしまう。そのため胴長 ``scale``（肩中心〜腰中心の距離）を
併せて返し、判定側で「胴長何個ぶん動いたか」に換算できるようにする。
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

HOST = Path(__file__).resolve().parent
if str(HOST) not in sys.path:
    sys.path.insert(0, str(HOST))

from vendor.mp_persondet import MPPersonDet  # noqa: E402
from vendor.mp_pose import MPPose  # noqa: E402

DEFAULT_MODEL_DIR = HOST / "models"
PERSON_MODEL = "person_detection.onnx"
POSE_MODEL = "pose_estimation.onnx"

# BlazePose 33キーポイントのうち使うもの。
NOSE = 0
LEFT_SHOULDER, RIGHT_SHOULDER = 11, 12
LEFT_HIP, RIGHT_HIP = 23, 24
FOOT_POINTS = (27, 28, 29, 30, 31, 32)  # 足首・かかと・つま先
BODY_POINTS = 33  # 33以降は補助点なので、体の範囲には使わない
MIN_VISIBILITY = 0.5  # 使用点の代表可視度。モデルの推論閾値と同じ下限を使う。


@dataclass(frozen=True)
class PoseMeasurement:
    """フレーム幅・高さで正規化した計測値と、距離補正用の胴長。"""

    x: float  # 腰中心のx
    y: float  # 肩中心と腰中心の中点のy（背景差分の重心yに相当する量）
    bottom: float  # 足の最下点
    area: float  # キーポイント全体のバウンディングボックス面積比
    scale: float  # 胴長（肩中心〜腰中心）。フレーム高さで正規化
    confidence: float
    visible: float  # 使用キーポイントの可視度の中央値

    @property
    def valid(self) -> bool:
        return (
            self.scale > 0
            and math.isfinite(self.scale)
            and math.isfinite(self.confidence)
            and self.confidence >= 0.5
            and math.isfinite(self.visible)
            and self.visible >= MIN_VISIBILITY
        )


def _mid(points: np.ndarray, a: int, b: int) -> np.ndarray:
    return (points[a] + points[b]) / 2.0


def measure(landmarks: np.ndarray, confidence: float, width: int, height: int) -> PoseMeasurement | None:
    """BlazePose のランドマーク（画像座標）を正規化計測値へ変換する。

    landmarks は (39, 5) で、各行が [x, y, z, visibility, presence]。
    x/y は元画像のピクセル座標。
    """
    array = np.asarray(landmarks, dtype=np.float64)
    if array.ndim != 2 or array.shape[0] < BODY_POINTS or array.shape[1] < 4:
        raise ValueError(f"ランドマークの形状が不正: {array.shape}")
    if width <= 0 or height <= 0:
        raise ValueError("フレームサイズが不正")

    points = array[:BODY_POINTS, :2]
    visibility = array[:BODY_POINTS, 3]

    hip = _mid(points, LEFT_HIP, RIGHT_HIP)
    shoulder = _mid(points, LEFT_SHOULDER, RIGHT_SHOULDER)
    torso = float(np.linalg.norm(shoulder - hip))
    if torso <= 0 or not math.isfinite(torso):
        return None

    feet = points[list(FOOT_POINTS)]
    # つま先・かかとのうち1点だけが外れたときに、max()だと全身のbottomが
    # フレーム外へ飛ぶ。6点の75パーセンタイルなら、片足の外れ値を除きつつ
    # 両足の下端を保持できる。
    bottom = float(np.percentile(feet[:, 1], 75.0))
    center = (hip + shoulder) / 2.0

    lo = np.min(points, axis=0)
    hi = np.max(points, axis=0)
    box = float(max(0.0, hi[0] - lo[0]) * max(0.0, hi[1] - lo[1]))

    used = [LEFT_SHOULDER, RIGHT_SHOULDER, LEFT_HIP, RIGHT_HIP, *FOOT_POINTS]
    return PoseMeasurement(
        x=float(hip[0]) / width,
        y=float(center[1]) / height,
        bottom=bottom / height,
        area=box / float(width * height),
        scale=torso / height,
        confidence=float(confidence),
        # 足先の一部が隠れても、使用点全体の代表的な可視度を残す。
        visible=float(np.median(visibility[used])),
    )


def person_row_from_landmarks(landmarks: np.ndarray) -> np.ndarray:
    """次フレームのROI用に、検出器の出力行と同じ形の配列を作る。

    MPPose._preprocess が使うのは person[4:12] の先頭2点、すなわち
    腰中心と「全身点」だけ。全身点は腰中心から体の上方向へ、全身が収まる
    距離だけ離れた点を指す。ここを再現できれば人物検出器を毎フレーム
    回さずに追跡できる。
    """
    array = np.asarray(landmarks, dtype=np.float64)
    points = array[:BODY_POINTS, :2]
    hip = _mid(points, LEFT_HIP, RIGHT_HIP)
    shoulder = _mid(points, LEFT_SHOULDER, RIGHT_SHOULDER)
    up = shoulder - hip
    norm = float(np.linalg.norm(up))
    if norm <= 0:
        raise ValueError("胴の向きを決められない")
    # 腰中心から最も遠いキーポイントまでを覆う正方形ROIにする。
    radius = float(np.max(np.linalg.norm(points - hip, axis=1)))
    full_body = hip + up / norm * max(radius, norm)
    row = np.zeros(13, dtype=np.float64)
    row[4:6] = hip
    row[6:8] = full_body
    return row


def _pick(blobs: list[np.ndarray], predicate, label: str) -> np.ndarray:
    for blob in blobs:
        if predicate(blob):
            return blob
    shapes = [tuple(blob.shape) for blob in blobs]
    raise RuntimeError(f"{label} に該当する出力がない: {shapes}")


class PersonDet(MPPersonDet):
    """出力の順序をモデルの形状から決めるMPPersonDet。

    上流実装は forward が返す順序を固定で仮定しているが、OpenCV 5.0 では
    getUnconnectedOutLayersNames() の順序が 4.x と入れ替わり、スコアと
    回帰値を取り違える。順序に依存せず形状で同定する。
    """

    def infer(self, image: np.ndarray) -> np.ndarray:
        height, width = image.shape[:2]
        input_blob, pad_bias = self._preprocess(image)
        self.model.setInput(input_blob)
        outputs = list(self.model.forward(self.model.getUnconnectedOutLayersNames()))
        # 回帰は [4個の枠 + 8個のランドマーク] で最終次元12、分類は1。
        regressor = _pick(outputs, lambda b: b.ndim == 3 and b.shape[2] >= 12, "回帰出力")
        classificator = _pick(outputs, lambda b: b.ndim == 3 and b.shape[2] == 1, "分類出力")
        return self._postprocess([regressor, classificator], np.array([width, height]), pad_bias)


class Pose(MPPose):
    """出力の順序を形状から決めるMPPose。理由は PersonDet と同じ。"""

    def infer(self, image: np.ndarray, person: np.ndarray):
        height, width = image.shape[:2]
        blob, rotated_bbox, angle, matrix, pad_bias = self._preprocess(image, person)
        self.model.setInput(blob)
        outputs = list(self.model.forward(self.model.getUnconnectedOutLayersNames()))
        landmarks = _pick(outputs, lambda b: b.size == 39 * 5, "ランドマーク")
        world = _pick(outputs, lambda b: b.size == 39 * 3, "ワールドランドマーク")
        mask = _pick(outputs, lambda b: b.size == 256 * 256, "マスク")
        heatmap = _pick(outputs, lambda b: b.size == 64 * 64 * 39, "ヒートマップ")
        conf = _pick(outputs, lambda b: b.size == 1, "信頼度")
        return self._postprocess(
            [landmarks, conf, mask, heatmap, world],
            rotated_bbox,
            angle,
            matrix,
            pad_bias,
            np.array([width, height]),
        )


class PoseTracker:
    """人物検出は必要な時だけ、以降はランドマークだけで追跡する。

    定常状態で人物検出器を回さないことが速度上の要点。実測（i7-6700、
    4スレッド）で person_det 12.9ms + pose 11.2ms に対し、pose のみなら
    11.2ms で済む。
    """

    def __init__(
        self,
        model_dir: Path = DEFAULT_MODEL_DIR,
        conf_threshold: float = 0.5,
        score_threshold: float = 0.5,
        threads: int = 4,
        redetect_after: int = 5,
    ) -> None:
        person_path = Path(model_dir) / PERSON_MODEL
        pose_path = Path(model_dir) / POSE_MODEL
        for path in (person_path, pose_path):
            if not path.exists():
                raise RuntimeError(f"モデルがない: {path}（host/fetch_models.sh を実行する）")
        if threads > 0:
            cv2.setNumThreads(threads)
        self.person = PersonDet(str(person_path), scoreThreshold=score_threshold)
        self.pose = Pose(str(pose_path), confThreshold=conf_threshold)
        self.redetect_after = max(1, redetect_after)
        self.track: np.ndarray | None = None
        self.misses = 0
        self.detections = 0  # 人物検出器を回した回数
        self.landmarks: np.ndarray | None = None

    def reset(self) -> None:
        self.track = None
        self.misses = 0
        self.landmarks = None

    def _select(self, people: np.ndarray, width: int) -> np.ndarray | None:
        """通行人が写り込むため、対象を1人に絞る。

        バウンディングボックスが最も大きい人物を採る。廊下の奥を横切る人は
        手前に立つプレイヤーより小さく写るため、これで大半は分離できる。
        """
        if people is None or len(people) == 0:
            return None
        areas = (people[:, 2] - people[:, 0]) * (people[:, 3] - people[:, 1])
        return people[int(np.argmax(areas))].copy()

    def update(self, frame: np.ndarray) -> PoseMeasurement | None:
        height, width = frame.shape[:2]
        if self.track is None:
            people = self.person.infer(frame)
            self.detections += 1
            self.track = self._select(people, width)
            if self.track is None:
                self.landmarks = None
                return None
        # MPPose._preprocess は渡した配列を書き換えるので、毎回複製して渡す。
        result = self.pose.infer(frame, self.track.copy())
        if result is None:
            self.misses += 1
            self.landmarks = None
            if self.misses >= self.redetect_after:
                self.reset()
            return None
        landmarks, confidence = result[1], result[5]
        measurement = measure(landmarks, confidence, width, height)
        if measurement is None or not measurement.valid:
            self.misses += 1
            self.landmarks = None
            if self.misses >= self.redetect_after:
                self.reset()
            return None
        try:
            self.track = person_row_from_landmarks(landmarks)
        except ValueError:
            self.reset()
            return None
        self.misses = 0
        self.landmarks = landmarks
        return measurement
