// パネル補正ヘッダー単体の自己テスト。rpi-rgb-led-matrixは不要。
#include "panel_calibration.h"

#include <cstdio>
#include <cstdlib>
#include <unistd.h>

namespace {

bool Expect(bool condition, const char *message) {
  if (!condition) std::fprintf(stderr, "ERROR: %s\n", message);
  return condition;
}

}  // namespace

int main() {
  char path[] = "/tmp/panel-calibration-selftest-XXXXXX";
  const int descriptor = mkstemp(path);
  if (descriptor < 0) {
    std::perror("mkstemp");
    return 1;
  }
  FILE *file = fdopen(descriptor, "w");
  if (file == nullptr) {
    std::perror("fdopen");
    close(descriptor);
    std::remove(path);
    return 1;
  }
  std::fputs("# inline comments are allowed\n"
             "0 0 0.50 1.50 2.00 0.50 # panel A0\n"
             "2 7 0.25 0.75 1.25\n",
             file);
  std::fclose(file);

  panel_calibration::PanelCalibration calibration;
  bool ok = calibration.Load(path, 3, 8);
  std::remove(path);
  if (!Expect(ok, "valid calibration file was rejected")) return 1;

  std::uint8_t red = 100;
  std::uint8_t green = 100;
  std::uint8_t blue = 100;
  calibration.Apply(0, 0, &red, &green, &blue);
  if (!Expect(red == 25 && green == 75 && blue == 100,
              "lane 0 chain 0 gain and brightness were not applied")) {
    return 1;
  }

  red = green = blue = 100;
  calibration.Apply(32, 0, &red, &green, &blue);
  if (!Expect(red == 100 && green == 100 && blue == 100,
              "unlisted panel was not left at identity")) {
    return 1;
  }

  red = green = blue = 100;
  calibration.Apply(7 * 32 + 1, 2 * 32 + 1, &red, &green, &blue);
  if (!Expect(red == 25 && green == 75 && blue == 125,
              "lane 2 chain 7 gain was not applied")) {
    return 1;
  }

  std::puts("0 errors");
  return 0;
}
