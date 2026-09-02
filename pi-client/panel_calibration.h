// HUB75出力上の各32x32パネルへ適用する、RGBゲイン形式の色補正。
// 設定ファイルは「lane chain red_gain green_gain blue_gain」の空白区切り。
#pragma once

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>

namespace panel_calibration {

constexpr int kPanelPixels = 32;
constexpr int kMaxParallel = 3;
constexpr int kMaxChainLength = 8;

struct PanelGain {
  float red = 1.0F;
  float green = 1.0F;
  float blue = 1.0F;
};

class PanelCalibration {
 public:
  PanelCalibration() { Reset(); }

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

    char line[256];
    int line_number = 0;
    while (std::fgets(line, sizeof(line), file) != nullptr) {
      ++line_number;
      char *cursor = line;
      while (*cursor == ' ' || *cursor == '\t') ++cursor;
      if (*cursor == '\0' || *cursor == '\n' || *cursor == '#') continue;

      int lane = -1;
      int chain = -1;
      float red = 0.0F;
      float green = 0.0F;
      float blue = 0.0F;
      char trailing = '\0';
      if (std::sscanf(cursor, "%d %d %f %f %f %c", &lane, &chain, &red,
                      &green, &blue, &trailing) != 5 || lane < 0 ||
          lane >= parallel || chain < 0 || chain >= chain_length ||
          !IsValidGain(red) || !IsValidGain(green) || !IsValidGain(blue)) {
        std::fprintf(stderr,
                     "error: %s:%d は 'lane chain red green blue'（倍率0〜2）で指定してください\n",
                     path, line_number);
        std::fclose(file);
        return false;
      }
      gains_[lane][chain] = {red, green, blue};
    }
    std::fclose(file);
    std::printf("pi-client: panel calibration loaded from %s\n", path);
    return true;
  }

  void Apply(int x, int y, std::uint8_t *red, std::uint8_t *green,
             std::uint8_t *blue) const {
    const int lane = y / kPanelPixels;
    const int chain = x / kPanelPixels;
    if (lane < 0 || lane >= kMaxParallel || chain < 0 ||
        chain >= kMaxChainLength) {
      return;
    }
    const PanelGain &gain = gains_[lane][chain];
    *red = Scale(*red, gain.red);
    *green = Scale(*green, gain.green);
    *blue = Scale(*blue, gain.blue);
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
