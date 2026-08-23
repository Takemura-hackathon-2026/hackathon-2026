// STRUCTURE Sensorの深度フレームをOpenNI2対応OpenCVから標準出力へ流す。
// Pythonの配布版cv2にOpenNI2がなくても、oyaki側のC++ OpenCVを利用する。

#include <opencv2/opencv.hpp>

#include <chrono>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <thread>
#include <csignal>
#include <unistd.h>

namespace {

volatile std::sig_atomic_t g_running = 1;

struct Options {
  int width = 640;
  int height = 480;
  double fps = 30.0;
  int frames = 0;
  double seconds = 0.0;
};

void stop_handler(int) { g_running = 0; }

[[noreturn]] void usage_error(const std::string& message) {
  throw std::invalid_argument(message +
                              "\n使い方: structure_depth_capture [options]\n"
                              "  --width N       深度幅（既定640）\n"
                              "  --height N      深度高さ（既定480）\n"
                              "  --fps N         取得FPS（既定30）\n"
                              "  --frames N      フレーム数（既定0=無期限）\n"
                              "  --seconds N     取得秒数（既定0=無期限）\n"
                              "  --help          このヘルプを表示");
}

int parse_int(const std::string& text, const char* option) {
  char* end = nullptr;
  const long value = std::strtol(text.c_str(), &end, 10);
  if (end == text.c_str() || *end != '\0' || value <= 0 ||
      value > std::numeric_limits<int>::max()) {
    usage_error(std::string(option) + "は正の整数で指定してください: " + text);
  }
  return static_cast<int>(value);
}

int parse_nonnegative_int(const std::string& text, const char* option) {
  char* end = nullptr;
  const long value = std::strtol(text.c_str(), &end, 10);
  if (end == text.c_str() || *end != '\0' || value < 0 ||
      value > std::numeric_limits<int>::max()) {
    usage_error(std::string(option) + "は0以上の整数で指定してください: " + text);
  }
  return static_cast<int>(value);
}

double parse_double(const std::string& text, const char* option) {
  char* end = nullptr;
  const double value = std::strtod(text.c_str(), &end);
  if (end == text.c_str() || *end != '\0' || !(value > 0.0)) {
    usage_error(std::string(option) + "は正の数値で指定してください: " + text);
  }
  return value;
}

Options parse_options(int argc, char** argv) {
  Options options;
  for (int index = 1; index < argc; ++index) {
    const std::string argument = argv[index];
    auto next_value = [&](const char* option) -> std::string {
      if (index + 1 >= argc) usage_error(std::string(option) + "の値がない");
      return argv[++index];
    };
    if (argument == "--help" || argument == "-h") {
      std::cout << "STRUCTURE Sensor深度取得（OpenNI2/C++）\n"
                << "  --width N       深度幅（既定640）\n"
                << "  --height N      深度高さ（既定480）\n"
                << "  --fps N         取得FPS（既定30）\n"
                << "  --frames N      フレーム数（既定0=無期限）\n"
                << "  --seconds N     取得秒数（既定0=無期限）\n";
      std::exit(0);
    }
    if (argument == "--width") {
      options.width = parse_int(next_value("--width"), "--width");
    } else if (argument == "--height") {
      options.height = parse_int(next_value("--height"), "--height");
    } else if (argument == "--fps") {
      options.fps = parse_double(next_value("--fps"), "--fps");
    } else if (argument == "--frames") {
      options.frames = parse_nonnegative_int(next_value("--frames"), "--frames");
    } else if (argument == "--seconds") {
      options.seconds = parse_double(next_value("--seconds"), "--seconds");
    } else {
      usage_error("不明な引数: " + argument);
    }
  }
  if (options.fps > 120.0) usage_error("--fpsは120以下");
  return options;
}

void write_u32(std::uint32_t value) {
  std::uint8_t bytes[4]{
      static_cast<std::uint8_t>(value & 0xFFU),
      static_cast<std::uint8_t>((value >> 8U) & 0xFFU),
      static_cast<std::uint8_t>((value >> 16U) & 0xFFU),
      static_cast<std::uint8_t>((value >> 24U) & 0xFFU),
  };
  if (std::fwrite(bytes, sizeof(bytes), 1, stdout) != 1) {
    throw std::runtime_error("深度フレームヘッダーを書けない");
  }
}

void write_frame(std::uint32_t frame_id, const cv::Mat& depth) {
  if (depth.type() != CV_16UC1 || depth.empty()) {
    throw std::runtime_error("OpenNI2の深度マップがCV_16UC1ではない");
  }
  const std::uint32_t width = static_cast<std::uint32_t>(depth.cols);
  const std::uint32_t height = static_cast<std::uint32_t>(depth.rows);
  const std::uint64_t payload_size = static_cast<std::uint64_t>(depth.total()) * sizeof(std::uint16_t);
  if (payload_size > std::numeric_limits<std::uint32_t>::max()) {
    throw std::runtime_error("深度フレームが大きすぎる");
  }

  // Python側のFRAME_HEADER: <4sIIII（magic, frame_id, width, height, bytes）。
  const char magic[4] = {'S', 'D', 'P', '1'};
  if (std::fwrite(magic, sizeof(magic), 1, stdout) != 1) throw std::runtime_error("深度フレームを書けない");
  write_u32(frame_id);
  write_u32(width);
  write_u32(height);
  write_u32(static_cast<std::uint32_t>(payload_size));
  for (int row = 0; row < depth.rows; ++row) {
    const auto* data = reinterpret_cast<const std::uint8_t*>(depth.ptr<std::uint16_t>(row));
    const std::size_t row_size = static_cast<std::size_t>(depth.cols) * sizeof(std::uint16_t);
    if (std::fwrite(data, row_size, 1, stdout) != 1) throw std::runtime_error("深度フレームを書けない");
  }
  if (std::fflush(stdout) != 0) throw std::runtime_error("深度フレームをflushできない");
}

}  // namespace

int main(int argc, char** argv) {
  try {
    const Options options = parse_options(argc, argv);
    std::signal(SIGINT, stop_handler);
    std::signal(SIGTERM, stop_handler);
    std::signal(SIGPIPE, stop_handler);

    // OpenNI2の一部ログはstdoutへ直接出るため、Python向けバイナリを壊さない。
    const int saved_stdout = ::dup(STDOUT_FILENO);
    if (saved_stdout < 0) throw std::runtime_error("標準出力を退避できない");
    std::cout.flush();
    std::fflush(stdout);
    if (::dup2(STDERR_FILENO, STDOUT_FILENO) < 0) {
      ::close(saved_stdout);
      throw std::runtime_error("標準出力を切り替えられない");
    }
    cv::VideoCapture capture(cv::CAP_OPENNI2_ASUS);
    std::cout.flush();
    std::fflush(stdout);
    if (::dup2(saved_stdout, STDOUT_FILENO) < 0) {
      ::close(saved_stdout);
      throw std::runtime_error("標準出力を復元できない");
    }
    ::close(saved_stdout);
    if (!capture.isOpened()) {
      throw std::runtime_error("STRUCTURE SensorをOpenNI2で開けない。USB接続とudev権限を確認");
    }
    capture.set(cv::CAP_PROP_FRAME_WIDTH, options.width);
    capture.set(cv::CAP_PROP_FRAME_HEIGHT, options.height);

    const auto started = std::chrono::steady_clock::now();
    const auto period = std::chrono::duration<double>(1.0 / options.fps);
    auto deadline = started;
    int frame_count = 0;
    while (g_running && (options.frames == 0 || frame_count < options.frames)) {
      if (options.seconds > 0.0 &&
          std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count() >= options.seconds) {
        break;
      }
      if (!capture.grab()) throw std::runtime_error("STRUCTURE Sensorのフレームをgrabできない");
      cv::Mat depth;
      if (!capture.retrieve(depth, cv::CAP_OPENNI_DEPTH_MAP)) {
        throw std::runtime_error("STRUCTURE Sensorの深度マップをretrieveできない");
      }
      write_frame(static_cast<std::uint32_t>(frame_count), depth);
      ++frame_count;
      deadline += std::chrono::duration_cast<std::chrono::steady_clock::duration>(period);
      std::this_thread::sleep_until(deadline);
    }
    std::cerr << "structure_depth_capture: frames=" << frame_count << "\n";
    return 0;
  } catch (const cv::Exception& error) {
    std::cerr << "structure_depth_capture: OpenCV error: " << error.what() << "\n";
    return 2;
  } catch (const std::exception& error) {
    std::cerr << "structure_depth_capture: " << error.what() << "\n";
    return 2;
  }
}
