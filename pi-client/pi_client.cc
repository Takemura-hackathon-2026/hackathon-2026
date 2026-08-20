// RGB LED ゲーム Pi 常駐表示クライアント。
//
// 主機から届く 192x96 のパレットインデックス配列を受信し、固定 LUT で RGB へ
// 変換して HUB75 へ出力するだけの処理に限定する。ゲームロジック、画像生成、
// 圧縮・解凍、動的メモリ確保、外部プロセス起動は行わない。
//
//   UDP受信 → 固定位置へmemcpy → CRC・パレット範囲確認 → 裏表バッファ交換 → HUB75出力
//
// 同期段階Aの実装。READY返送と GPIO 同期待機は後続で追加する。
#include <arpa/inet.h>
#include <netinet/in.h>
#include <signal.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
#include <zlib.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <vector>

#include "led-matrix.h"

namespace {

constexpr int kCanvasWidth = 192;
constexpr int kSliceHeight = 96;
constexpr int kSliceBytes = kCanvasWidth * kSliceHeight;
constexpr std::uint32_t kMagic = 0x524C4544;  // "RLED"
constexpr int kMaxChunks = 64;
// これ以上巻き戻ったら主機の再起動とみなし、新しい frame_id 系列へ追従する。
constexpr std::uint32_t kResyncThreshold = 600;
constexpr int kMaxPacket = 2048;
// 死活報告の送信先ポートと送信間隔。主機の診断表示（TEST2）が受ける。
constexpr int kHealthPort = 5101;
constexpr double kHealthIntervalSec = 1.0;
// パネル全体の既定輝度。個別の --led-brightness 指定があればそちらを優先する。
constexpr int kDefaultBrightnessPercent = 40;

double MonotonicSeconds() {
  timespec ts{};
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return static_cast<double>(ts.tv_sec) + ts.tv_nsec * 1e-9;
}

bool ReadCpuTemperature(double *temperature_c) {
  FILE *file = fopen("/sys/class/thermal/thermal_zone0/temp", "r");
  if (file == nullptr) return false;
  long millidegrees = 0;
  const bool ok = fscanf(file, "%ld", &millidegrees) == 1;
  fclose(file);
  if (!ok || millidegrees < -50000 || millidegrees > 200000) return false;
  *temperature_c = static_cast<double>(millidegrees) / 1000.0;
  return true;
}

// palettes.py と同一。FC6 は 2026-08-06 指定の 52 色。
constexpr int kFc6Count = 52;
const std::uint8_t kFc6[kFc6Count][3] = {
    {171, 0, 19},    {231, 0, 91},    {255, 119, 183}, {255, 199, 219},
    {167, 0, 0},     {219, 43, 0},    {255, 119, 99},  {255, 191, 179},
    {127, 11, 0},    {203, 79, 15},   {255, 155, 59},  {255, 219, 171},
    {67, 47, 0},     {139, 115, 0},   {243, 191, 63},  {255, 231, 163},
    {0, 71, 0},      {0, 151, 0},     {131, 211, 19},  {227, 255, 163},
    {0, 81, 0},      {0, 171, 0},     {79, 223, 75},   {171, 243, 191},
    {0, 63, 23},     {0, 147, 59},    {88, 248, 152},  {179, 255, 207},
    {27, 63, 95},    {0, 131, 139},   {0, 235, 219},   {159, 255, 243},
    {39, 27, 143},   {0, 115, 239},   {63, 191, 255},  {171, 231, 255},
    {0, 0, 171},     {35, 59, 239},   {95, 115, 255},  {199, 215, 255},
    {71, 0, 159},    {131, 0, 243},   {167, 139, 253}, {215, 203, 255},
    {143, 0, 119},   {191, 0, 191},   {247, 123, 255}, {255, 199, 255},
    {0, 0, 0},       {117, 117, 117}, {188, 188, 188}, {255, 255, 255},
};

constexpr int kMsx16Count = 16;
const std::uint8_t kMsx16[kMsx16Count][3] = {
    {0, 0, 0},       // 0x0 透明。主機が背景色へ解決済みのため黒として扱う
    {0, 0, 0},       {62, 184, 73},   {116, 208, 125}, {89, 85, 224},
    {128, 118, 241}, {185, 94, 81},   {101, 219, 239}, {219, 101, 89},
    {255, 137, 125}, {204, 195, 94},  {222, 208, 135}, {58, 162, 65},
    {183, 102, 181}, {204, 204, 204}, {255, 255, 255},
};

#pragma pack(push, 1)
struct FrameChunkHeader {
  std::uint32_t magic;
  std::uint32_t frame_id;
  std::uint8_t target_id;
  std::uint8_t palette_mode;  // 0=FC6, 1=MSX16
  std::uint16_t chunk_id;
  std::uint16_t chunk_count;
  std::uint16_t payload_size;
  std::uint32_t crc32;
};
#pragma pack(pop)

volatile bool g_running = true;
void StopHandler(int) { g_running = false; }

// 受信済みチャンクの管理。動的確保を避けるため固定長で持つ。
//
// 書き込み位置は「最終チャンク以外の大きさ」を刻み幅として求める。最終チャンク
// だけは端数になるため、自身の大きさから位置を逆算してはならない。刻み幅が未確
// 定のうちに最終チャンクが届いた場合は、確定するまで保留する。
struct FrameAssembler {
  std::uint32_t frame_id = 0;
  std::uint8_t palette_mode = 0;
  int chunk_count = 0;
  int stride = 0;  // 最終チャンク以外の payload_size
  bool received[kMaxChunks] = {false};
  int received_count = 0;
  std::uint8_t buffer[kSliceBytes] = {0};
  std::uint8_t pending_tail[kMaxPacket] = {0};
  int pending_tail_size = 0;
  bool active = false;

  void Reset(std::uint32_t id, std::uint8_t mode, int count) {
    frame_id = id;
    palette_mode = mode;
    chunk_count = count;
    stride = 0;
    received_count = 0;
    pending_tail_size = 0;
    memset(received, 0, sizeof(received));
    memset(buffer, 0, sizeof(buffer));
    active = true;
  }
};

}  // namespace

int main(int argc, char *argv[]) {
  rgb_matrix::RGBMatrix::Options matrix_options;
  rgb_matrix::RuntimeOptions runtime_options;
  matrix_options.rows = 32;
  matrix_options.cols = 32;
  matrix_options.chain_length = 6;
  matrix_options.parallel = 3;
  matrix_options.hardware_mapping = "regular";
  matrix_options.brightness = kDefaultBrightnessPercent;

  if (!rgb_matrix::ParseOptionsFromFlags(&argc, &argv, &matrix_options,
                                         &runtime_options)) {
    fprintf(stderr, "usage: %s --target-id N [--port 5000] [led options]\n",
            argv[0]);
    return 1;
  }

  int target_id = -1;
  int port = 5000;
  bool verbose = false;
  bool rotate180 = false;  // パネルを上下逆に取り付けた個体向け
  int health_port = kHealthPort;
  for (int i = 1; i < argc; ++i) {
    if (strcmp(argv[i], "--target-id") == 0 && i + 1 < argc) {
      target_id = atoi(argv[++i]);
    } else if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) {
      port = atoi(argv[++i]);
    } else if (strcmp(argv[i], "--verbose") == 0) {
      verbose = true;
    } else if (strcmp(argv[i], "--rotate180") == 0) {
      rotate180 = true;
    } else if (strcmp(argv[i], "--health-port") == 0 && i + 1 < argc) {
      health_port = atoi(argv[++i]);
    }
  }
  if (target_id < 0 || target_id > 3) {
    fprintf(stderr, "error: --target-id は 0〜3 で指定する\n");
    return 1;
  }

  int sock = socket(AF_INET, SOCK_DGRAM, 0);
  if (sock < 0) {
    perror("socket");
    return 1;
  }
  int rcvbuf = 4 << 20;
  setsockopt(sock, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));
  int reuse = 1;
  setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
  // 受信が途絶えても死活報告と終了判定を回せるよう、待ちに上限を設ける。
  timeval recv_timeout{};
  recv_timeout.tv_usec = 200000;  // 200ms
  setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &recv_timeout, sizeof(recv_timeout));

  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  addr.sin_port = htons(port);
  if (bind(sock, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0) {
    perror("bind");
    return 1;
  }

  rgb_matrix::RGBMatrix *matrix =
      rgb_matrix::RGBMatrix::CreateFromOptions(matrix_options, runtime_options);
  if (matrix == nullptr) {
    fprintf(stderr, "error: LED マトリクスを初期化できない\n");
    return 1;
  }
  rgb_matrix::FrameCanvas *canvas = matrix->CreateFrameCanvas();

  signal(SIGINT, StopHandler);
  signal(SIGTERM, StopHandler);

  printf("pi-client: target_id=%d port=%d %dx%d (chain=%d parallel=%d) rotate180=%s\n",
         target_id, port, kCanvasWidth, kSliceHeight, matrix_options.chain_length,
         matrix_options.parallel, rotate180 ? "yes" : "no");
  fflush(stdout);

  FrameAssembler assembler;
  std::uint8_t packet[kMaxPacket];
  std::uint32_t last_displayed = 0;
  bool has_displayed = false;
  long frames = 0, dropped = 0;

  // 死活報告用。主機のアドレスは受信パケットの送信元から学習する。
  int health_sock = socket(AF_INET, SOCK_DGRAM, 0);
  sockaddr_in host_addr{};
  bool host_known = false;
  const double start_time = MonotonicSeconds();
  double last_health = start_time;
  long frames_at_last_health = 0;

  while (g_running) {
    sockaddr_in from{};
    socklen_t from_len = sizeof(from);
    ssize_t received = recvfrom(sock, packet, sizeof(packet), 0,
                                reinterpret_cast<sockaddr *>(&from), &from_len);

    // 一定間隔で死活を報告する。表示処理より優先度は低いが、受信が途絶えても
    // 送り続けられるよう受信結果の判定より前に置く。
    const double now = MonotonicSeconds();
    if (host_known && now - last_health >= kHealthIntervalSec) {
      const double span = now - last_health;
      const double fps = span > 0 ? (frames - frames_at_last_health) / span : 0.0;
      char line[256];
      double temperature_c = 0.0;
      const int len = ReadCpuTemperature(&temperature_c)
                          ? snprintf(line, sizeof(line),
                                     "PIHEALTH target=%d displayed=%ld dropped=%ld fps=%.1f up=%.0f rot=%d temp_c=%.1f",
                                     target_id, frames, dropped, fps, now - start_time,
                                     rotate180 ? 1 : 0, temperature_c)
                          : snprintf(line, sizeof(line),
                                     "PIHEALTH target=%d displayed=%ld dropped=%ld fps=%.1f up=%.0f rot=%d temp_c=NA",
                                     target_id, frames, dropped, fps, now - start_time,
                                     rotate180 ? 1 : 0);
      if (len > 0) {
        sockaddr_in dest = host_addr;
        dest.sin_port = htons(health_port);
        sendto(health_sock, line, len, 0, reinterpret_cast<sockaddr *>(&dest),
               sizeof(dest));
      }
      last_health = now;
      frames_at_last_health = frames;
    }

    if (received < static_cast<ssize_t>(sizeof(FrameChunkHeader))) continue;
    if (!host_known) {
      host_addr = from;
      host_known = true;
    }

    FrameChunkHeader header;
    memcpy(&header, packet, sizeof(header));
    const std::uint32_t magic = ntohl(header.magic);
    if (magic != kMagic) continue;
    if (header.target_id != target_id) continue;  // 自機宛以外は捨てる

    const std::uint32_t frame_id = ntohl(header.frame_id);
    const std::uint16_t chunk_id = ntohs(header.chunk_id);
    const std::uint16_t chunk_count = ntohs(header.chunk_count);
    const std::uint16_t payload_size = ntohs(header.payload_size);
    const std::uint32_t crc = ntohl(header.crc32);
    const std::uint8_t *payload = packet + sizeof(header);

    if (chunk_count == 0 || chunk_count > kMaxChunks) continue;
    if (chunk_id >= chunk_count) continue;
    if (payload_size == 0 ||
        received != static_cast<ssize_t>(sizeof(header)) + payload_size) {
      continue;
    }
    if (header.palette_mode > 1) continue;
    if (crc32(0L, payload, payload_size) != crc) continue;

    // 新しいフレームが来たら、未完成の古いフレームは捨てて先へ進む。
    if (!assembler.active || assembler.frame_id != frame_id) {
      if (assembler.active && assembler.received_count < assembler.chunk_count) {
        ++dropped;
      }
      assembler.Reset(frame_id, header.palette_mode, chunk_count);
    }
    if (assembler.received[chunk_id]) continue;

    const bool is_tail = (chunk_id == chunk_count - 1);
    if (!is_tail) {
      // 刻み幅は最終チャンク以外の大きさ。全チャンクで等しい前提。
      if (assembler.stride == 0) assembler.stride = payload_size;
      if (assembler.stride != payload_size) continue;  // 混在は不正フレーム
    }

    if (is_tail && assembler.stride == 0) {
      // 刻み幅が未確定。位置を決められないので保留する。
      memcpy(assembler.pending_tail, payload, payload_size);
      assembler.pending_tail_size = payload_size;
    } else {
      const std::size_t offset =
          static_cast<std::size_t>(chunk_id) * assembler.stride;
      if (offset + payload_size > kSliceBytes) continue;
      memcpy(assembler.buffer + offset, payload, payload_size);
    }
    assembler.received[chunk_id] = true;
    ++assembler.received_count;
    if (assembler.received_count != assembler.chunk_count) continue;

    // 保留していた最終チャンクを、確定した刻み幅で正しい位置へ書き込む。
    if (assembler.pending_tail_size > 0) {
      const std::size_t offset =
          static_cast<std::size_t>(assembler.chunk_count - 1) * assembler.stride;
      if (offset + assembler.pending_tail_size > kSliceBytes) {
        assembler.active = false;
        ++dropped;
        continue;
      }
      memcpy(assembler.buffer + offset, assembler.pending_tail,
             assembler.pending_tail_size);
      assembler.pending_tail_size = 0;
    }

    // 受信済みの総バイト数がスライス全体を満たしているか確認する。
    if (static_cast<std::size_t>(assembler.stride) *
                (assembler.chunk_count - 1) +
            payload_size <
        kSliceBytes) {
      assembler.active = false;
      ++dropped;
      continue;
    }

    // ここから下は完成フレームのみ。パレット範囲外を含むフレームは表示しない。
    const int limit = (assembler.palette_mode == 0) ? kFc6Count : kMsx16Count;
    bool valid = true;
    for (int i = 0; i < kSliceBytes; ++i) {
      if (assembler.buffer[i] >= limit) {
        valid = false;
        break;
      }
    }
    assembler.active = false;
    if (!valid) {
      ++dropped;
      continue;
    }
    // 古いフレームは捨てる。ただし主機を再起動すると frame_id が 0 から振り直
    // されるため、大きく巻き戻った場合は新しい配信とみなして追従する。
    if (has_displayed && frame_id <= last_displayed) {
      if (last_displayed - frame_id < kResyncThreshold) continue;
      printf("pi-client: frame_id が巻き戻ったため再同期 (%u -> %u)\n",
             last_displayed, frame_id);
      fflush(stdout);
    }

    const std::uint8_t(*lut)[3] = (assembler.palette_mode == 0) ? kFc6 : kMsx16;
    const std::uint8_t *src = assembler.buffer;
    for (int y = 0; y < kSliceHeight; ++y) {
      for (int x = 0; x < kCanvasWidth; ++x) {
        const std::uint8_t *rgb = lut[*src++];
        // 上下逆に取り付けたパネルは、描画時に点対称へ写す。
        const int dx = rotate180 ? (kCanvasWidth - 1 - x) : x;
        const int dy = rotate180 ? (kSliceHeight - 1 - y) : y;
        canvas->SetPixel(dx, dy, rgb[0], rgb[1], rgb[2]);
      }
    }
    canvas = matrix->SwapOnVSync(canvas);
    last_displayed = frame_id;
    has_displayed = true;
    ++frames;
    if (verbose && (frames % 60) == 0) {
      printf("frame_id=%u displayed=%ld dropped=%ld\n", frame_id, frames,
             dropped);
      fflush(stdout);
    }
  }

  matrix->Clear();
  delete matrix;
  close(sock);
  close(health_sock);
  printf("pi-client: 終了 displayed=%ld dropped=%ld\n", frames, dropped);
  return 0;
}
