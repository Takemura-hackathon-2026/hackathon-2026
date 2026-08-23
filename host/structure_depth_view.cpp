// STRUCTURE Sensorの深度画像をFC6パレットへ変換してLED全体へ送る診断表示。
//
// oyakiのOpenCV Python環境がOpenNI2を含まない場合でも、OpenNI2対応の
// C++ OpenCV（pkg-config opencv4）だけでセンサー確認ができるようにする。

#include <arpa/inet.h>
#include <netdb.h>
#include <opencv2/opencv.hpp>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <exception>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <csignal>
#include <thread>
#include <utility>
#include <vector>

namespace {

constexpr int kCanvasWidth = 192;
constexpr int kCanvasHeight = 384;
constexpr int kPiCount = 4;
constexpr int kPiHeight = 96;
constexpr std::uint32_t kMagic = 0x524C4544;  // RLED
constexpr std::uint8_t kPaletteModeFc6 = 0;
constexpr int kFc6RampSize = 48;  // 0x00〜0x2F。0x30〜0x33は黒〜白の固定色。

volatile std::sig_atomic_t g_running = 1;

struct Destination {
  sockaddr_in address{};
  std::string label;
};

struct Options {
  std::vector<std::string> pi_values{
      "192.168.10.101:5000",
      "192.168.10.102:5000",
      "192.168.10.103:5000",
      "192.168.10.104:5000",
  };
  bool custom_pi = false;
  bool send = true;
  double fps = 30.0;
  double seconds = 0.0;
  int chunk_size = 1200;
  std::string rotation = "ccw";
  double near_mm = 0.0;
  double far_mm = 0.0;
};

void stop_handler(int) { g_running = 0; }

[[noreturn]] void usage_error(const std::string& message) {
  throw std::invalid_argument(message + "\n使い方: structure_depth_view [options]\n"
                               "  --pi HOST:PORT       4台分指定（省略時は192.168.10.101〜104:5000）\n"
                               "  --fps N              送信FPS（既定30）\n"
                               "  --seconds N          終了までの秒数（既定0=無期限）\n"
                               "  --chunk-size N       UDPチャンクサイズ（256〜1400、既定1200）\n"
                               "  --rotation MODE      none/cw/ccw/180（既定ccw）\n"
                               "  --near-mm N          表示範囲の近端。0=フレームから自動\n"
                               "  --far-mm N           表示範囲の遠端。0=フレームから自動\n"
                               "  --no-send            LEDへ送らずセンサー取得だけ行う\n"
                               "  --help               このヘルプを表示");
}

double parse_double(const std::string& text, const char* option) {
  char* end = nullptr;
  const double value = std::strtod(text.c_str(), &end);
  if (end == text.c_str() || *end != '\0' || !std::isfinite(value)) {
    usage_error(std::string(option) + "は有限な数値で指定してください: " + text);
  }
  return value;
}

int parse_int(const std::string& text, const char* option) {
  char* end = nullptr;
  const long value = std::strtol(text.c_str(), &end, 10);
  if (end == text.c_str() || *end != '\0' || value < std::numeric_limits<int>::min() ||
      value > std::numeric_limits<int>::max()) {
    usage_error(std::string(option) + "は整数で指定してください: " + text);
  }
  return static_cast<int>(value);
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
      std::cout << "STRUCTURE Sensor深度ビュー（FC6/UDP）\n"
                << "  --pi HOST:PORT       4台分指定（省略時は192.168.10.101〜104:5000）\n"
                << "  --fps N              送信FPS（既定30）\n"
                << "  --seconds N          終了までの秒数（既定0=無期限）\n"
                << "  --chunk-size N       UDPチャンクサイズ（256〜1400、既定1200）\n"
                << "  --rotation MODE      none/cw/ccw/180（既定ccw）\n"
                << "  --near-mm N          表示範囲の近端。0=フレームから自動\n"
                << "  --far-mm N           表示範囲の遠端。0=フレームから自動\n"
                << "  --no-send            LEDへ送らずセンサー取得だけ行う\n";
      std::exit(0);
    }
    if (argument == "--pi") {
      if (!options.custom_pi) {
        options.pi_values.clear();
        options.custom_pi = true;
      }
      options.pi_values.push_back(next_value("--pi"));
    } else if (argument == "--fps") {
      options.fps = parse_double(next_value("--fps"), "--fps");
    } else if (argument == "--seconds") {
      options.seconds = parse_double(next_value("--seconds"), "--seconds");
    } else if (argument == "--chunk-size") {
      options.chunk_size = parse_int(next_value("--chunk-size"), "--chunk-size");
    } else if (argument == "--rotation") {
      options.rotation = next_value("--rotation");
    } else if (argument == "--near-mm") {
      options.near_mm = parse_double(next_value("--near-mm"), "--near-mm");
    } else if (argument == "--far-mm") {
      options.far_mm = parse_double(next_value("--far-mm"), "--far-mm");
    } else if (argument == "--no-send") {
      options.send = false;
    } else {
      usage_error("不明な引数: " + argument);
    }
  }
  if (options.pi_values.size() != kPiCount) usage_error("--piは4個指定してください");
  if (!(options.fps > 0.0) || options.fps > 120.0) usage_error("--fpsは0より大きく120以下");
  if (options.seconds < 0.0) usage_error("--secondsは0以上");
  if (options.chunk_size < 256 || options.chunk_size > 1400) usage_error("--chunk-sizeは256〜1400");
  if (options.rotation != "none" && options.rotation != "cw" && options.rotation != "ccw" &&
      options.rotation != "180") {
    usage_error("--rotationはnone/cw/ccw/180");
  }
  if (options.near_mm < 0.0 || options.far_mm < 0.0) usage_error("深度範囲は0以上");
  if (options.near_mm > 0.0 && options.far_mm > 0.0 && options.far_mm <= options.near_mm) {
    usage_error("--far-mmは--near-mmより大きくしてください");
  }
  return options;
}

Destination resolve_destination(const std::string& value) {
  const std::size_t separator = value.rfind(':');
  if (separator == std::string::npos || separator == 0 || separator + 1 >= value.size()) {
    throw std::invalid_argument("Pi宛先はHOST:PORTで指定してください: " + value);
  }
  const std::string host = value.substr(0, separator);
  const std::string port = value.substr(separator + 1);
  addrinfo hints{};
  hints.ai_family = AF_INET;
  hints.ai_socktype = SOCK_DGRAM;
  addrinfo* result = nullptr;
  const int status = getaddrinfo(host.c_str(), port.c_str(), &hints, &result);
  if (status != 0 || result == nullptr) {
    throw std::runtime_error("Pi宛先を解決できない: " + value + " (" + gai_strerror(status) + ")");
  }
  Destination destination;
  std::memcpy(&destination.address, result->ai_addr, sizeof(sockaddr_in));
  destination.label = value;
  freeaddrinfo(result);
  return destination;
}

std::uint32_t crc32(const std::uint8_t* data, std::size_t length) {
  std::uint32_t crc = 0xFFFFFFFFU;
  for (std::size_t index = 0; index < length; ++index) {
    crc ^= data[index];
    for (int bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1U) ^ (0xEDB88320U & (0U - (crc & 1U)));
    }
  }
  return ~crc;
}

void put_u16(std::uint8_t* output, std::uint16_t value) {
  output[0] = static_cast<std::uint8_t>((value >> 8U) & 0xFFU);
  output[1] = static_cast<std::uint8_t>(value & 0xFFU);
}

void put_u32(std::uint8_t* output, std::uint32_t value) {
  output[0] = static_cast<std::uint8_t>((value >> 24U) & 0xFFU);
  output[1] = static_cast<std::uint8_t>((value >> 16U) & 0xFFU);
  output[2] = static_cast<std::uint8_t>((value >> 8U) & 0xFFU);
  output[3] = static_cast<std::uint8_t>(value & 0xFFU);
}

void send_frame(int socket_fd, const std::vector<Destination>& destinations, std::uint32_t frame_id,
                const cv::Mat& indexed, int chunk_size) {
  constexpr std::size_t kHeaderSize = 20;
  constexpr std::size_t kSliceSize = static_cast<std::size_t>(kPiHeight) * kCanvasWidth;
  for (int target = 0; target < kPiCount; ++target) {
    const auto* payload = indexed.ptr<std::uint8_t>(target * kPiHeight);
    const std::size_t chunk_count = (kSliceSize + static_cast<std::size_t>(chunk_size) - 1U) /
                                    static_cast<std::size_t>(chunk_size);
    for (std::size_t chunk_id = 0; chunk_id < chunk_count; ++chunk_id) {
      const std::size_t offset = chunk_id * static_cast<std::size_t>(chunk_size);
      const std::size_t size = std::min(static_cast<std::size_t>(chunk_size), kSliceSize - offset);
      std::vector<std::uint8_t> packet(kHeaderSize + size);
      put_u32(packet.data() + 0, kMagic);
      put_u32(packet.data() + 4, frame_id);
      packet[8] = static_cast<std::uint8_t>(target);
      packet[9] = kPaletteModeFc6;
      put_u16(packet.data() + 10, static_cast<std::uint16_t>(chunk_id));
      put_u16(packet.data() + 12, static_cast<std::uint16_t>(chunk_count));
      put_u16(packet.data() + 14, static_cast<std::uint16_t>(size));
      put_u32(packet.data() + 16, crc32(payload + offset, size));
      std::memcpy(packet.data() + kHeaderSize, payload + offset, size);
      const ssize_t sent = sendto(socket_fd, packet.data(), packet.size(), 0,
                                  reinterpret_cast<const sockaddr*>(&destinations[target].address),
                                  sizeof(destinations[target].address));
      if (sent < 0) {
        throw std::runtime_error("UDP送信に失敗 target=" + std::to_string(target) + ": " +
                                 std::strerror(errno));
      }
    }
  }
}

double percentile(std::vector<std::uint16_t>& values, double ratio) {
  if (values.empty()) return 0.0;
  const std::size_t position = static_cast<std::size_t>(ratio * static_cast<double>(values.size() - 1));
  std::nth_element(values.begin(), values.begin() + position, values.end());
  return static_cast<double>(values[position]);
}

std::pair<double, double> choose_range(const cv::Mat& depth, const Options& options) {
  std::vector<std::uint16_t> values;
  values.reserve(static_cast<std::size_t>(depth.total() / 16U));
  for (int y = 0; y < depth.rows; y += 4) {
    const auto* row = depth.ptr<std::uint16_t>(y);
    for (int x = 0; x < depth.cols; x += 4) {
      if (row[x] > 0) values.push_back(row[x]);
    }
  }
  if (values.empty()) return {0.0, 1.0};
  const double near_mm = options.near_mm > 0.0 ? options.near_mm : percentile(values, 0.02);
  const double far_mm = options.far_mm > 0.0 ? options.far_mm : percentile(values, 0.98);
  return {near_mm, std::max(far_mm, near_mm + 1.0)};
}

cv::Mat rotate_depth(const cv::Mat& depth, const std::string& rotation) {
  cv::Mat result;
  if (rotation == "none") {
    result = depth;
  } else if (rotation == "cw") {
    cv::rotate(depth, result, cv::ROTATE_90_CLOCKWISE);
  } else if (rotation == "ccw") {
    cv::rotate(depth, result, cv::ROTATE_90_COUNTERCLOCKWISE);
  } else {
    cv::rotate(depth, result, cv::ROTATE_180);
  }
  return result;
}

cv::Mat to_indexed(const cv::Mat& depth, double near_mm, double far_mm) {
  cv::Mat indexed(depth.rows, depth.cols, CV_8UC1, cv::Scalar(0x30));
  const double scale = static_cast<double>(kFc6RampSize - 1) / (far_mm - near_mm);
  for (int y = 0; y < depth.rows; ++y) {
    const auto* input = depth.ptr<std::uint16_t>(y);
    auto* output = indexed.ptr<std::uint8_t>(y);
    for (int x = 0; x < depth.cols; ++x) {
      const std::uint16_t value = input[x];
      if (value == 0) {
        output[x] = 0x30;  // 無効深度は黒。
        continue;
      }
      const double intensity = (far_mm - static_cast<double>(value)) * scale;
      const int color = std::clamp(static_cast<int>(std::lround(intensity)), 0, kFc6RampSize - 1);
      output[x] = static_cast<std::uint8_t>(color);
    }
  }
  return indexed;
}

}  // namespace

int main(int argc, char** argv) {
  try {
    const Options options = parse_options(argc, argv);
    std::vector<Destination> destinations;
    destinations.reserve(options.pi_values.size());
    for (const auto& value : options.pi_values) destinations.push_back(resolve_destination(value));

    std::signal(SIGINT, stop_handler);
    std::signal(SIGTERM, stop_handler);

    cv::VideoCapture capture(cv::CAP_OPENNI2_ASUS);
    if (!capture.isOpened()) {
      throw std::runtime_error("STRUCTURE SensorをOpenNI2で開けない。USB再接続を確認");
    }
    capture.set(cv::CAP_PROP_FRAME_WIDTH, 640);
    capture.set(cv::CAP_PROP_FRAME_HEIGHT, 480);

    int socket_fd = -1;
    if (options.send) {
      socket_fd = socket(AF_INET, SOCK_DGRAM, 0);
      if (socket_fd < 0) throw std::runtime_error("UDPソケットを開けない: " + std::string(std::strerror(errno)));
    }

    std::cout << "structure depth view: OpenNI2 depth -> FC6 -> "
              << (options.send ? "UDP" : "no-send") << " rotation=" << options.rotation << "\n";
    std::cout << "depth範囲: --near-mm/--far-mm が0なら各フレームの2〜98 percentile\n";

    const auto started = std::chrono::steady_clock::now();
    auto deadline = started;
    std::uint32_t frame_id = 0;
    std::size_t frame_count = 0;
    while (g_running) {
      if (options.seconds > 0.0) {
        const double elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
        if (elapsed >= options.seconds) break;
      }

      if (!capture.grab()) throw std::runtime_error("STRUCTURE Sensorのフレームをgrabできない");
      cv::Mat depth;
      if (!capture.retrieve(depth, cv::CAP_OPENNI_DEPTH_MAP) || depth.empty()) {
        throw std::runtime_error("STRUCTURE Sensorの深度フレームをretrieveできない");
      }
      if (depth.type() != CV_16UC1) {
        throw std::runtime_error("深度マップがCV_16UC1ではない: type=" + std::to_string(depth.type()));
      }

      const auto range = choose_range(depth, options);
      const cv::Mat rotated = rotate_depth(depth, options.rotation);
      cv::Mat indexed;
      cv::resize(to_indexed(rotated, range.first, range.second), indexed,
                 cv::Size(kCanvasWidth, kCanvasHeight), 0.0, 0.0, cv::INTER_NEAREST);
      if (options.send) send_frame(socket_fd, destinations, frame_id++, indexed, options.chunk_size);
      ++frame_count;
      if (frame_count == 1 || frame_count % 30 == 0) {
        std::cout << "\rframes=" << frame_count << " range=" << range.first << ".." << range.second
                  << "mm" << std::flush;
      }

      deadline += std::chrono::duration_cast<std::chrono::steady_clock::duration>(
          std::chrono::duration<double>(1.0 / options.fps));
      const auto now = std::chrono::steady_clock::now();
      if (deadline > now) std::this_thread::sleep_for(deadline - now);
      if (deadline + std::chrono::seconds(1) < std::chrono::steady_clock::now()) {
        deadline = std::chrono::steady_clock::now();
      }
    }
    std::cout << "\n終了 frames=" << frame_count << "\n";
    if (socket_fd >= 0) close(socket_fd);
    capture.release();
    return 0;
  } catch (const cv::Exception& error) {
    std::cerr << "error: OpenCV: " << error.what() << "\n";
    return 2;
  } catch (const std::exception& error) {
    std::cerr << "error: " << error.what() << "\n";
    return 2;
  }
}
