// STRUCTURE Sensorの深度フレームをOpenNI2から標準出力へ流す。
// 配布版OpenCVのOpenNI2対応有無に依存せず、OpenNI2 APIを直接利用する。

#include <OpenNI.h>

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
#include <vector>
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
  int decimate = 1;
};

void stop_handler(int) { g_running = 0; }

[[noreturn]] void usage_error(const std::string& message) {
  throw std::invalid_argument(message +
                              "\n使い方: structure_depth_capture [options]\n"
                              "  --width N       深度幅（既定640）\n"
                              "  --height N      深度高さ（既定480）\n"
                              "  --fps N         取得FPS（既定30）\n"
                              "  --decimate N    N画素おきに間引いて送る（既定1=間引かない）\n"
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
                << "  --decimate N    N画素おきに間引いて送る（既定1=間引かない）\n"
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
    } else if (argument == "--decimate") {
      options.decimate = parse_int(next_value("--decimate"), "--decimate");
    } else if (argument == "--frames") {
      options.frames = parse_nonnegative_int(next_value("--frames"), "--frames");
    } else if (argument == "--seconds") {
      options.seconds = parse_double(next_value("--seconds"), "--seconds");
    } else {
      usage_error("不明な引数: " + argument);
    }
  }
  if (options.fps > 120.0) usage_error("--fpsは120以下");
  if (options.decimate > 16) usage_error("--decimateは16以下");
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

void check_openni(openni::Status status, const std::string& operation) {
  if (status == openni::STATUS_OK) return;
  throw std::runtime_error(operation + ": " + openni::OpenNI::getExtendedError());
}

// 1行ぶんをN画素おきに詰める。最近傍縮小と同じ結果で、転送量を1/Nにする。
void decimate_row(const std::uint16_t* source, std::uint16_t* destination, int destination_width, int step) {
  for (int index = 0; index < destination_width; ++index) {
    destination[index] = source[static_cast<std::size_t>(index) * step];
  }
}

void write_frame(std::uint32_t frame_id, const openni::VideoFrameRef& frame, int decimate) {
  if (!frame.isValid() || frame.getData() == nullptr || frame.getWidth() <= 0 ||
      frame.getHeight() <= 0 || frame.getStrideInBytes() < frame.getWidth() * 2) {
    throw std::runtime_error("OpenNI2の深度フレームが不正");
  }
  if (decimate < 1) throw std::runtime_error("間引き幅が不正");
  const int step = decimate;
  // 端数は切り捨てる。Python側は届いた幅高さをそのまま使う。
  const std::uint32_t width = static_cast<std::uint32_t>(frame.getWidth() / step);
  const std::uint32_t height = static_cast<std::uint32_t>(frame.getHeight() / step);
  if (width == 0 || height == 0) throw std::runtime_error("間引き後の深度フレームが空");
  const std::uint64_t payload_size = static_cast<std::uint64_t>(width) * height * sizeof(std::uint16_t);
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
  const auto* frame_data = static_cast<const std::uint8_t*>(frame.getData());
  const std::size_t row_size = static_cast<std::size_t>(width) * sizeof(std::uint16_t);
  std::vector<std::uint16_t> row_buffer(step == 1 ? 0 : width);
  for (std::uint32_t row = 0; row < height; ++row) {
    const auto* data = frame_data + static_cast<std::size_t>(row) * step * frame.getStrideInBytes();
    if (step == 1) {
      if (std::fwrite(data, row_size, 1, stdout) != 1) throw std::runtime_error("深度フレームを書けない");
      continue;
    }
    decimate_row(reinterpret_cast<const std::uint16_t*>(data), row_buffer.data(),
                 static_cast<int>(width), step);
    if (std::fwrite(row_buffer.data(), row_size, 1, stdout) != 1) {
      throw std::runtime_error("深度フレームを書けない");
    }
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
    check_openni(openni::OpenNI::initialize(), "OpenNI2を初期化できない");
    openni::Device device;
    check_openni(device.open(openni::ANY_DEVICE), "STRUCTURE Sensorを開けない");
    openni::VideoStream stream;
    check_openni(stream.create(device, openni::SENSOR_DEPTH), "深度ストリームを作成できない");

    const openni::SensorInfo* sensor_info = device.getSensorInfo(openni::SENSOR_DEPTH);
    if (sensor_info == nullptr) throw std::runtime_error("深度センサー情報を取得できない");
    const auto& modes = sensor_info->getSupportedVideoModes();
    const int requested_fps = static_cast<int>(options.fps + 0.5);
    int selected_index = -1;
    int best_fps_difference = std::numeric_limits<int>::max();
    for (int index = 0; index < modes.getSize(); ++index) {
      const openni::VideoMode& mode = modes[index];
      if (mode.getResolutionX() != options.width || mode.getResolutionY() != options.height ||
          mode.getPixelFormat() != openni::PIXEL_FORMAT_DEPTH_1_MM) {
        continue;
      }
      const int fps_difference = std::abs(mode.getFps() - requested_fps);
      if (fps_difference < best_fps_difference) {
        selected_index = index;
        best_fps_difference = fps_difference;
      }
    }
    if (selected_index < 0) {
      throw std::runtime_error("指定解像度でPIXEL_FORMAT_DEPTH_1_MMを利用できない");
    }
    check_openni(stream.setVideoMode(modes[selected_index]), "深度ビデオモードを設定できない");
    check_openni(stream.start(), "深度ストリームを開始できない");
    std::cout.flush();
    std::fflush(stdout);
    if (::dup2(saved_stdout, STDOUT_FILENO) < 0) {
      ::close(saved_stdout);
      throw std::runtime_error("標準出力を復元できない");
    }
    ::close(saved_stdout);
    const auto started = std::chrono::steady_clock::now();
    int frame_count = 0;
    while (g_running && (options.frames == 0 || frame_count < options.frames)) {
      if (options.seconds > 0.0 &&
          std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count() >= options.seconds) {
        break;
      }
      openni::VideoFrameRef frame;
      check_openni(stream.readFrame(&frame), "STRUCTURE Sensorのフレームを取得できない");
      if (frame.getVideoMode().getPixelFormat() != openni::PIXEL_FORMAT_DEPTH_1_MM) {
        throw std::runtime_error("深度フレームの単位がmmではない");
      }
      write_frame(static_cast<std::uint32_t>(frame_count), frame, options.decimate);
      ++frame_count;
    }
    stream.stop();
    stream.destroy();
    device.close();
    openni::OpenNI::shutdown();
    std::cerr << "structure_depth_capture: frames=" << frame_count << "\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "structure_depth_capture: " << error.what() << "\n";
    return 2;
  }
}
