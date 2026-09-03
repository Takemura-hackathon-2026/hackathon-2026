// HUB75の1出力へ32x32パネル8枚を直列接続した時の、低輝度・番号付き確認用。
// 通常のUDPクライアントや他の2出力は使わない。
#include <signal.h>

#include <array>
#include <cstdint>
#include <cstdio>

#include "led-matrix.h"

namespace {

constexpr int kPanelSize = 32;
constexpr int kChainLength = 8;
constexpr int kDefaultBrightnessPercent = 10;

volatile bool g_running = true;

void StopHandler(int) { g_running = false; }

void FillRect(rgb_matrix::Canvas *canvas, int x, int y, int width, int height,
              std::uint8_t red, std::uint8_t green, std::uint8_t blue) {
  for (int row = y; row < y + height; ++row) {
    for (int column = x; column < x + width; ++column) {
      canvas->SetPixel(column, row, red, green, blue);
    }
  }
}

void DrawDigit(rgb_matrix::Canvas *canvas, int x, int y, int digit) {
  // a,b,c,d,e,f,g の7セグメント。1〜8だけを表示する。
  constexpr std::array<std::array<bool, 7>, 9> kSegments = {{
      {{false, false, false, false, false, false, false}},
      {{false, true, true, false, false, false, false}},
      {{true, true, false, true, true, false, true}},
      {{true, true, true, true, false, false, true}},
      {{false, true, true, false, false, true, true}},
      {{true, false, true, true, false, true, true}},
      {{true, false, true, true, true, true, true}},
      {{true, true, true, false, false, false, false}},
      {{true, true, true, true, true, true, true}},
  }};
  const auto &segments = kSegments[static_cast<std::size_t>(digit)];
  constexpr std::array<std::array<int, 4>, 7> kRects = {{
      {{8, 4, 16, 3}},   // a
      {{22, 7, 3, 9}},   // b
      {{22, 17, 3, 9}},  // c
      {{8, 25, 16, 3}},  // d
      {{5, 17, 3, 9}},   // e
      {{5, 7, 3, 9}},    // f
      {{8, 15, 16, 3}},  // g
  }};
  for (std::size_t index = 0; index < segments.size(); ++index) {
    if (!segments[index]) continue;
    const auto &rect = kRects[index];
    FillRect(canvas, x + rect[0], y + rect[1], rect[2], rect[3], 255, 255, 255);
  }
}

void DrawTestPattern(rgb_matrix::Canvas *canvas) {
  constexpr std::array<std::array<std::uint8_t, 3>, kChainLength> kColors = {{
      {{180, 0, 0}}, {{180, 80, 0}}, {{150, 150, 0}}, {{0, 140, 0}},
      {{0, 110, 150}}, {{0, 0, 180}}, {{110, 0, 150}}, {{160, 0, 80}},
  }};
  canvas->Clear();
  for (int panel = 0; panel < kChainLength; ++panel) {
    const int x = panel * kPanelSize;
    const auto &color = kColors[static_cast<std::size_t>(panel)];
    FillRect(canvas, x, 0, kPanelSize, kPanelSize, color[0], color[1], color[2]);
    // パネル境界を黒で残し、番号の見落としを防ぐ。
    FillRect(canvas, x, 0, 1, kPanelSize, 0, 0, 0);
    FillRect(canvas, x + kPanelSize - 1, 0, 1, kPanelSize, 0, 0, 0);
    DrawDigit(canvas, x, 0, panel + 1);
  }
}

}  // namespace

int main(int argc, char *argv[]) {
  rgb_matrix::RGBMatrix::Options matrix_options;
  rgb_matrix::RuntimeOptions runtime_options;
  matrix_options.rows = kPanelSize;
  matrix_options.cols = kPanelSize;
  matrix_options.chain_length = kChainLength;
  matrix_options.parallel = 1;  // P0だけを使う。P1/P2は試験対象外。
  matrix_options.hardware_mapping = "regular";
  matrix_options.brightness = kDefaultBrightnessPercent;
  if (!rgb_matrix::ParseOptionsFromFlags(&argc, &argv, &matrix_options,
                                         &runtime_options)) {
    std::fprintf(stderr, "usage: %s [--led-* options]\n", argv[0]);
    return 2;
  }
  if (matrix_options.chain_length != kChainLength || matrix_options.parallel != 1) {
    std::fprintf(stderr, "error: chain8_test は --led-chain=8 --led-parallel=1 で実行してください\n");
    return 2;
  }
  rgb_matrix::RGBMatrix *matrix =
      rgb_matrix::RGBMatrix::CreateFromOptions(matrix_options, runtime_options);
  if (matrix == nullptr) {
    std::fprintf(stderr, "error: LED matrix を初期化できません\n");
    return 1;
  }
  signal(SIGINT, StopHandler);
  signal(SIGTERM, StopHandler);
  std::printf("chain8 test: P0 only, 8 panels, brightness=%d%%; Ctrl-C to stop\n",
              matrix_options.brightness);
  rgb_matrix::FrameCanvas *canvas = matrix->CreateFrameCanvas();
  while (g_running) {
    DrawTestPattern(canvas);
    canvas = matrix->SwapOnVSync(canvas);
  }
  delete matrix;
  return 0;
}
