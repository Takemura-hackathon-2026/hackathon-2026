"""主機側の区間計測。TEST1〜5 で共用する。

使い方:

    prof = Profiler(enabled=args.profile)
    with prof.span("render"):
        indexed = renderer.render(now)
    prof.frame()
    ...
    prof.report()

計測は既定で無効。無効時は `span()` が使い回しの空オブジェクトを返すだけで、
時刻取得も記録も行わない。「計測を入れたら遅くなった」を避けるための構造で、
このオーバーヘッドの有無自体を `selftest.py` で検証している。

区間は入れ子にできるが、集計は区間名ごとに独立して行う。したがって外側の区間の
時間には内側の区間が含まれる。合計が実時間を超えるのはそのため。

制約: 区間オブジェクトを名前ごとに使い回して確保を避けているため、**同じ名前の
区間を入れ子にすることはできない**（開始時刻が上書きされる）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from time import perf_counter_ns
from typing import IO, Sequence

import numpy as np

# 出力する分位点。
PERCENTILES = (50, 95, 99)


class _NullSpan:
    """計測が無効なときに使い回す空の区間。"""

    __slots__ = ()

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


_NULL_SPAN = _NullSpan()


class _Span:
    """1 区間の計測。`with` を抜けた時点で所要時間を親へ渡す。"""

    __slots__ = ("_samples", "_start")

    def __init__(self, samples: list[int]) -> None:
        self._samples = samples
        self._start = 0

    def __enter__(self) -> "_Span":
        self._start = perf_counter_ns()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self._samples.append(perf_counter_ns() - self._start)
        return False


class Profiler:
    """区間ごとの所要時間と、任意のカウンターを集計する。"""

    def __init__(self, enabled: bool = False, label: str = "") -> None:
        self.enabled = enabled
        self.label = label
        self._samples: dict[str, list[int]] = {}
        self._spans: dict[str, _Span] = {}
        self._counters: dict[str, int] = {}
        self._frames = 0
        self._start_ns = perf_counter_ns() if enabled else 0
        self._stop_ns = 0

    # -- 計測 ---------------------------------------------------------------
    def span(self, name: str):
        """区間計測の `with` オブジェクトを返す。無効時は空オブジェクト。"""
        if not self.enabled:
            return _NULL_SPAN
        span = self._spans.get(name)
        if span is None:
            samples: list[int] = []
            self._samples[name] = samples
            span = _Span(samples)
            self._spans[name] = span
        return span

    def frame(self) -> None:
        """1 フレーム分の処理が終わったことを記録する。"""
        if self.enabled:
            self._frames += 1

    def count(self, name: str, value: int = 1) -> None:
        """バイト数・パケット数などの累計を加算する。"""
        if self.enabled:
            self._counters[name] = self._counters.get(name, 0) + value

    def stop(self) -> None:
        """計測を終える。`report()` / `to_dict()` の前に呼ぶ。"""
        if self.enabled and self._stop_ns == 0:
            self._stop_ns = perf_counter_ns()

    # -- 集計 ---------------------------------------------------------------
    @property
    def elapsed_seconds(self) -> float:
        if not self.enabled:
            return 0.0
        end = self._stop_ns or perf_counter_ns()
        return (end - self._start_ns) / 1e9

    @property
    def frames(self) -> int:
        return self._frames

    @property
    def fps(self) -> float:
        elapsed = self.elapsed_seconds
        return self._frames / elapsed if elapsed > 0 else 0.0

    def summary(self) -> dict[str, dict[str, float]]:
        """区間ごとの 件数 / 合計 / 平均 / 分位点 / 最大 [ミリ秒]。"""
        result: dict[str, dict[str, float]] = {}
        for name, samples in self._samples.items():
            if not samples:
                continue
            values = np.asarray(samples, dtype=np.float64) / 1e6  # ns -> ms
            entry = {
                "count": float(values.size),
                "total_ms": float(values.sum()),
                "mean_ms": float(values.mean()),
                "max_ms": float(values.max()),
            }
            for percentile, value in zip(
                PERCENTILES, np.percentile(values, PERCENTILES)
            ):
                entry[f"p{percentile}_ms"] = float(value)
            # 1 秒あたりこの区間に費やした割合。律速の判定に使う。
            elapsed = self.elapsed_seconds
            entry["share"] = float(values.sum() / 1e3 / elapsed) if elapsed > 0 else 0.0
            result[name] = entry
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "frames": self._frames,
            "elapsed_s": self.elapsed_seconds,
            "fps": self.fps,
            "spans": self.summary(),
            "counters": dict(self._counters),
        }

    # -- 出力 ---------------------------------------------------------------
    def report(self, stream: IO[str] | None = None) -> None:
        """人が読む表を出す。既定は標準エラー（標準出力を汚さない）。"""
        if not self.enabled:
            return
        self.stop()
        out = stream if stream is not None else sys.stderr
        title = f"profile: {self.label}" if self.label else "profile"
        print(f"\n=== {title} ===", file=out)
        print(
            f"frames={self._frames} elapsed={self.elapsed_seconds:.2f}s "
            f"fps={self.fps:.1f}",
            file=out,
        )
        summary = self.summary()
        if summary:
            header = f"{'span':<12}{'n':>7}{'mean':>9}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}{'share':>8}"
            print(header, file=out)
            print("-" * len(header), file=out)
            # 占有率の大きい順。律速がいちばん上に来る。
            for name, entry in sorted(
                summary.items(), key=lambda item: -item[1]["share"]
            ):
                print(
                    f"{name:<12}{int(entry['count']):>7}"
                    f"{entry['mean_ms']:>9.3f}{entry['p50_ms']:>9.3f}"
                    f"{entry['p95_ms']:>9.3f}{entry['p99_ms']:>9.3f}"
                    f"{entry['max_ms']:>9.3f}{entry['share'] * 100:>7.1f}%",
                    file=out,
                )
            print("時間は [ms]。share は実時間に占める割合（入れ子は二重に数える）。", file=out)
        if self._counters:
            print("counters:", file=out)
            elapsed = self.elapsed_seconds
            for name, value in sorted(self._counters.items()):
                rate = value / elapsed if elapsed > 0 else 0.0
                print(f"  {name:<20}{value:>14,}{rate:>14,.0f}/s", file=out)

    def write_json(self, path: Path) -> None:
        self.stop()
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))


def add_profile_arguments(parser) -> None:
    """TEST1〜5 で共通の計測オプションを足す。"""
    parser.add_argument(
        "--profile", action="store_true", help="区間計測を有効にし、終了時に集計を出す"
    )
    parser.add_argument(
        "--profile-json", type=Path, default=None, metavar="PATH",
        help="計測結果を JSON で保存する（--profile と併用）",
    )


def finish_profile(profiler: Profiler, json_path: Path | None) -> None:
    """終了処理。`report()` と JSON 保存をまとめる。"""
    if not profiler.enabled:
        return
    profiler.stop()
    profiler.report()
    if json_path is not None:
        profiler.write_json(json_path)
        print(f"profile json: {json_path}", file=sys.stderr)


def merge(profilers: Sequence[Profiler], label: str = "merged") -> dict[str, object]:
    """複数の計測結果をひとつの辞書へまとめる（ramp 試験用）。"""
    return {
        "label": label,
        "runs": [profiler.to_dict() for profiler in profilers],
    }
