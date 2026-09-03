// HUB75出力上の32x32パネルごとに適用するRGBゲイン・輝度補正。
// 設定ファイルは「lane chain red_gain green_gain blue_gain [brightness]」の
// 空白区切り。brightnessはRGB共通倍率で、旧5列形式では1.00倍として扱う。
#pragma once

#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>

namespace panel_calibration {

constexpr int kPanelPixels = 32;
constexpr int kMaxParallel = 3;
constexpr int kMaxChainLength = 8;

struct PanelGain {
  float red = 1.0F;
  float green = 1.0F;
  float blue = 1.0F;
  float brightness = 1.0F;
};

class PanelCalibration {
 public:
  PanelCalibration() { Reset(); }

  // 未記載のパネルは恒等補正のままにする。設定値は0〜2倍に限定する。
  bool Load(const char *path, int parallel, int chain_length) {
    Reset();
    if (parallel < 1 || parallel > kMaxParallel || chain_length < 1 ||
        chain_length > kMaxChainLength) {
      std::fprintf(stderr,
                   "error: panel calibration は parallel=1〜%d, chain=1〜%d のみ対応\n",
                   kMaxParallel, kMaxChainLength);
      return false;
    }

    FILE *file = std::fopen(path, "r");
    if (file == nullptr) {
      std::perror(path);
      return false;
    }

    bool seen[kMaxParallel][kMaxChainLength] = {};
    char line[256];
    int line_number = 0;
    while (std::fgets(line, sizeof(line), file) != nullptr) {
      ++line_number;
      char *cursor = line;
      while (*cursor == ' ' || *cursor == '\t') ++cursor;
      if (*cursor == '\0' || *cursor == '\n' || *cursor == '\r' ||
          *cursor == '#') {
        continue;
      }

      int lane = -1;
      int chain = -1;
      float red = 0.0F;
      float green = 0.0F;
      float blue = 0.0F;
      float brightness = 1.0F;
      int consumed = 0;
      const int fields = std::sscanf(
          cursor, "%d %d %f %f %f %n", &lane, &chain, &red, &green, &blue,
          &consumed);
      char *trailing = cursor + consumed;
      while (*trailing != '\0' &&
             std::isspace(static_cast<unsigned char>(*trailing))) {
        ++trailing;
      }
      int total_fields = fields;
      if (fields == 5 && *trailing != '\0' && *trailing != '#') {
        int brightness_consumed = 0;
        if (std::sscanf(trailing, "%f %n", &brightness,
                        &brightness_consumed) == 1) {
          total_fields = 6;
          trailing += brightness_consumed;
          while (*trailing != '\0' &&
                 std::isspace(static_cast<unsigned char>(*trailing))) {
            ++trailing;
          }
        }
      }
      const bool trailing_ok = *trailing == '\0' || *trailing == '#';
      const bool valid_indices = lane >= 0 && lane < kMaxParallel &&
                                 chain >= 0 && chain < kMaxChainLength;
      const bool valid_gains = IsValidGain(red) && IsValidGain(green) &&
                               IsValidGain(blue) && IsValidGain(brightness);
      const bool outside_runtime_geometry =
          valid_indices && (lane >= parallel || chain >= chain_length);
      const bool identity_gain = red == 1.0F && green == 1.0F && blue == 1.0F &&
                                 brightness == 1.0F;
      if ((total_fields != 5 && total_fields != 6) || !trailing_ok ||
          !valid_indices || !valid_gains ||
          seen[lane][chain] ||
          (outside_runtime_geometry && !identity_gain)) {
        std::fprintf(
            stderr,
            "error: %s:%d は現在のgeometry内の 'lane chain red green blue [brightness]'（倍率0〜2）で指定してください\n",
            path, line_number);
        std::fclose(file);
        return false;
      }
      seen[lane][chain] = true;
      // 将来のchain数向けに恒等値だけは受け入れる。旧6直列クライアントへ
      // 3レーン×8枚の共通設定を配っても、存在しないchainは無効な補正にならない。
      if (!outside_runtime_geometry) {
        gains_[lane][chain] = {red, green, blue, brightness};
      }
    }
    std::fclose(file);
    std::printf("pi-client: panel calibration loaded from %s\n", path);
    return true;
  }

  // x/yは論理画面ではなく、SetPixelへ渡すHUB75キャンバス座標。
  void Apply(int x, int y, std::uint8_t *red, std::uint8_t *green,
             std::uint8_t *blue) const {
    if (red == nullptr || green == nullptr || blue == nullptr) return;
    const int lane = y / kPanelPixels;
    const int chain = x / kPanelPixels;
    if (lane < 0 || lane >= kMaxParallel || chain < 0 ||
        chain >= kMaxChainLength) {
      return;
    }
    const PanelGain &gain = gains_[lane][chain];
    *red = Scale(*red, gain.red * gain.brightness);
    *green = Scale(*green, gain.green * gain.brightness);
    *blue = Scale(*blue, gain.blue * gain.brightness);
  }

 private:
  PanelGain gains_[kMaxParallel][kMaxChainLength];

  static bool IsValidGain(float value) {
    return std::isfinite(value) && value >= 0.0F && value <= 2.0F;
  }

  static std::uint8_t Scale(std::uint8_t value, float gain) {
    const int scaled = static_cast<int>(std::lround(value * gain));
    return static_cast<std::uint8_t>(scaled < 0 ? 0 : (scaled > 255 ? 255 : scaled));
  }

  void Reset() {
    for (int lane = 0; lane < kMaxParallel; ++lane) {
      for (int chain = 0; chain < kMaxChainLength; ++chain) {
        gains_[lane][chain] = {};
      }
    }
  }
};

}  // namespace panel_calibration
