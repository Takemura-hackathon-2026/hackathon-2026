#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
OYAKI_TARGET="${OYAKI_TARGET:-oyaki}"
OYAKI_HOSTNAME="${OYAKI_HOSTNAME:-fe80::f56f:3e9f:fbb:3a85%%en9}"
OYAKI_REPO="${OYAKI_REPO:-/home/th1/hackathon-2026}"
CALIBRATION_MAX_SECONDS="${CALIBRATION_MAX_SECONDS:-360}"
REMOTE_PID="${REMOTE_PID:-/tmp/camera_calibrate.pid}"
REMOTE_LOG="${REMOTE_LOG:-/tmp/camera_calibrate_run.log}"
REMOTE_OUTPUT="${REMOTE_OUTPUT:-$OYAKI_REPO/camera_calibration.json}"
REMOTE_STANDBY_PID="${REMOTE_STANDBY_PID:-/tmp/led_standby.pid}"
REMOTE_STANDBY_LOG="${REMOTE_STANDBY_LOG:-/tmp/led_standby_run.log}"
REMOTE_DEPTH_VIEW_PID="${REMOTE_DEPTH_VIEW_PID:-/tmp/structure_depth_view.pid}"
REMOTE_DEPTH_VIEW_LOG="${REMOTE_DEPTH_VIEW_LOG:-/tmp/structure_depth_view.log}"
PI_SSH_USER="${PI_SSH_USER:-takemuralab}"
PI_STAGE="${PI_STAGE:-/tmp/hackathon-2026-pi-client}"
PI_RGB_LIB_DISTRIBUTION="${PI_RGB_LIB_DISTRIBUTION:-}"

ssh_args=(-o ConnectTimeout=10 -o HostKeyAlias=192.168.20.1 -o "HostName=$OYAKI_HOSTNAME" "$OYAKI_TARGET")
ssh_remote() {
  ssh "${ssh_args[@]}" "$@"
}

usage() {
  cat <<'EOF'
MacからoyakiのSTRUCTURE SensorキャリブレーションとPi表示クライアントを操作する。

使い方:
  host/oyaki_camera_calibrate.sh check
  host/oyaki_camera_calibrate.sh deploy
  host/oyaki_camera_calibrate.sh pi-deploy
  host/oyaki_camera_calibrate.sh pi-status
  host/oyaki_camera_calibrate.sh pi-start
  host/oyaki_camera_calibrate.sh pi-stop
  host/oyaki_camera_calibrate.sh display-test [秒]
  host/oyaki_camera_calibrate.sh standby-start
  host/oyaki_camera_calibrate.sh standby-foreground
  host/oyaki_camera_calibrate.sh standby-status
  host/oyaki_camera_calibrate.sh standby-stop
  host/oyaki_camera_calibrate.sh depth-view-build
  host/oyaki_camera_calibrate.sh depth-view-start
  host/oyaki_camera_calibrate.sh depth-view-foreground
  host/oyaki_camera_calibrate.sh depth-view-status
  host/oyaki_camera_calibrate.sh depth-view-stop
  host/oyaki_camera_calibrate.sh start
  host/oyaki_camera_calibrate.sh foreground
  host/oyaki_camera_calibrate.sh status
  host/oyaki_camera_calibrate.sh logs [行数]
  host/oyaki_camera_calibrate.sh stop
  host/oyaki_camera_calibrate.sh result
  host/oyaki_camera_calibrate.sh fetch 出力先ディレクトリ

環境変数: OYAKI_TARGET OYAKI_HOSTNAME OYAKI_REPO CALIBRATION_MAX_SECONDS REMOTE_STANDBY_PID REMOTE_STANDBY_LOG
          REMOTE_DEPTH_VIEW_PID REMOTE_DEPTH_VIEW_LOG
          PI_SSH_USER PI_STAGE PI_RGB_LIB_DISTRIBUTION
EOF
}

calibration_args=(
  --rotation none
  --send
  --pi 192.168.10.101:5000
  --pi 192.168.10.102:5000
  --pi 192.168.10.103:5000
  --pi 192.168.10.104:5000
  --output "$REMOTE_OUTPUT"
)

standby_args=(
  --mode status
  --send
  --pi 192.168.10.101:5000
  --pi 192.168.10.102:5000
  --pi 192.168.10.103:5000
  --pi 192.168.10.104:5000
)

sensor_detection_view_args=(
  --background-seconds 2.0
  --send
  --pi 192.168.10.101:5000
  --pi 192.168.10.102:5000
  --pi 192.168.10.103:5000
  --pi 192.168.10.104:5000
)

pi_specs=(
  192.168.10.101:0
  192.168.10.102:1
  192.168.10.103:2
  192.168.10.104:3
)

remote_command() {
  printf '%q ' "$@"
}

require_pi_ssh_user() {
  if [ -z "$PI_SSH_USER" ]; then
    printf 'PI_SSH_USERを指定してください（例: PI_SSH_USER=takemuralab）\n' >&2
    return 2
  fi
}

cmd_check() {
  ssh_remote "set -u
    hostname
    test -x '$OYAKI_REPO/.venv/bin/python'
    test -f '$OYAKI_REPO/host/frame_source.py'
    test -f '$OYAKI_REPO/host/structure_depth_view.cpp'
    test -f '$OYAKI_REPO/host/structure_depth_capture.cpp'
    test -f '$OYAKI_REPO/host/sensor_detection_view.py'
    test -f '$OYAKI_REPO/host/camera_calibrate.py'
    test -f '$OYAKI_REPO/host/block_breaker_selftest.py'
    test -f '$OYAKI_REPO/host/standby.py'
    test -f '$OYAKI_REPO/host/test_mode/single-eye-catch_2800x1040.png'
    printf 'sensor_processes='; pgrep -af camera_calibrate.py || true
    for ip in 192.168.10.101 192.168.10.102 192.168.10.103 192.168.10.104; do
      if ping -c 1 -W 1 \"\$ip\" >/dev/null 2>&1; then printf 'pi=%s reachable\\n' \"\$ip\"; else printf 'pi=%s unreachable\\n' \"\$ip\"; fi
    done"
}

cmd_deploy() {
  local stamp
  stamp="$(date +%Y%m%d%H%M%S)"
  ssh_remote "set -u
    for f in '$OYAKI_REPO/host/frame_source.py' '$OYAKI_REPO/host/structure_depth_view.cpp' '$OYAKI_REPO/host/structure_depth_capture.cpp' '$OYAKI_REPO/host/sensor_detection_view.py' '$OYAKI_REPO/host/camera_calibrate.py' '$OYAKI_REPO/host/camera_calibrate_selftest.py' '$OYAKI_REPO/host/block_breaker.py' '$OYAKI_REPO/host/block_breaker_selftest.py' '$OYAKI_REPO/host/jump_detector.py' '$OYAKI_REPO/host/standby.py' '$OYAKI_REPO/host/test_mode/test_mode.py' '$OYAKI_REPO/host/test_mode/single-eye-catch_2800x1040.png'; do
      if test -e \"\$f\"; then cp -p \"\$f\" \"\$f.bak.$stamp\"; fi
    done
    mkdir -p '$OYAKI_REPO/host/test_mode'"
  scp -p -o ConnectTimeout=10 -o HostKeyAlias=192.168.20.1 -o "HostName=$OYAKI_HOSTNAME" \
    "$SCRIPT_DIR/frame_source.py" "$SCRIPT_DIR/structure_depth_view.cpp" "$SCRIPT_DIR/structure_depth_capture.cpp" "$SCRIPT_DIR/sensor_detection_view.py" "$SCRIPT_DIR/camera_calibrate.py" "$SCRIPT_DIR/camera_calibrate_selftest.py" \
    "$SCRIPT_DIR/block_breaker.py" "$SCRIPT_DIR/block_breaker_selftest.py" "$SCRIPT_DIR/jump_detector.py" "$SCRIPT_DIR/standby.py" \
    "$OYAKI_TARGET:$OYAKI_REPO/host/"
  scp -p -o ConnectTimeout=10 -o HostKeyAlias=192.168.20.1 -o "HostName=$OYAKI_HOSTNAME" \
    "$SCRIPT_DIR/test_mode/test_mode.py" "$SCRIPT_DIR/test_mode/single-eye-catch_2800x1040.png" \
    "$OYAKI_TARGET:$OYAKI_REPO/host/test_mode/"
  ssh_remote "'$OYAKI_REPO/.venv/bin/python' -m py_compile '$OYAKI_REPO/host/frame_source.py' '$OYAKI_REPO/host/sensor_detection_view.py' '$OYAKI_REPO/host/camera_calibrate.py' '$OYAKI_REPO/host/camera_calibrate_selftest.py' '$OYAKI_REPO/host/block_breaker.py' '$OYAKI_REPO/host/block_breaker_selftest.py' '$OYAKI_REPO/host/jump_detector.py' '$OYAKI_REPO/host/standby.py' '$OYAKI_REPO/host/test_mode/test_mode.py'
    g++ -std=c++17 -O2 '$OYAKI_REPO/host/structure_depth_view.cpp' -o '$OYAKI_REPO/host/structure_depth_view' \$(pkg-config --cflags --libs opencv4)
    g++ -std=c++17 -O2 '$OYAKI_REPO/host/structure_depth_capture.cpp' -o '$OYAKI_REPO/host/structure_depth_capture' \$(pkg-config --cflags --libs opencv4)
    test -x '$OYAKI_REPO/host/structure_depth_view'
    test -x '$OYAKI_REPO/host/structure_depth_capture'"
  printf 'deployed: %s\n' "$OYAKI_REPO/host"
}

cmd_pi_deploy() {
  require_pi_ssh_user
  local pi_user_q
  pi_user_q="$(printf '%q' "$PI_SSH_USER")"

  ssh_remote "mkdir -p '$PI_STAGE'"
  scp -p -o ConnectTimeout=10 -o HostKeyAlias=192.168.20.1 -o "HostName=$OYAKI_HOSTNAME" \
    "$SCRIPT_DIR/../pi-client/Makefile" "$SCRIPT_DIR/../pi-client/pi_client.cc" \
    "$SCRIPT_DIR/../pi-client/install.sh" "$SCRIPT_DIR/../pi-client/pi-client@.service" \
    "$OYAKI_TARGET:$PI_STAGE/"

  ssh_remote "set -e
    for spec in ${pi_specs[*]}; do
      ip=\${spec%:*}
      target=\${spec##*:}
      printf 'deploy pi=%s target=%s\\n' \"\$ip\" \"\$target\"
      ssh -o BatchMode=yes -o ConnectTimeout=5 ${pi_user_q}@\"\$ip\" \"mkdir -p '$PI_STAGE' && sudo -n true\"
      scp -p -o BatchMode=yes -o ConnectTimeout=5 \
        '$PI_STAGE/Makefile' '$PI_STAGE/pi_client.cc' '$PI_STAGE/install.sh' '$PI_STAGE/pi-client@.service' \
        ${pi_user_q}@\"\$ip:$PI_STAGE/\"
      ssh -o BatchMode=yes -o ConnectTimeout=5 ${pi_user_q}@\"\$ip\" \
        \"RGB_LIB_DISTRIBUTION='$PI_RGB_LIB_DISTRIBUTION' bash '$PI_STAGE/install.sh' \$target\"
    done"
}

cmd_pi_status() {
  require_pi_ssh_user
  local pi_user_q
  pi_user_q="$(printf '%q' "$PI_SSH_USER")"
  ssh_remote "set +e
    rc=0
    for spec in ${pi_specs[*]}; do
      ip=\${spec%:*}
      target=\${spec##*:}
      state=\$(ssh -o BatchMode=yes -o ConnectTimeout=5 ${pi_user_q}@\"\$ip\" \"sudo -n systemctl is-active pi-client@\$target.service\" 2>&1)
      result=\$?
      printf 'pi=%s target=%s state=%s\\n' \"\$ip\" \"\$target\" \"\$state\"
      if [ \$result -ne 0 ]; then rc=1; fi
    done
    exit \$rc"
}

cmd_pi_start() {
  require_pi_ssh_user
  local pi_user_q
  pi_user_q="$(printf '%q' "$PI_SSH_USER")"
  ssh_remote "set +e
    rc=0
    for spec in ${pi_specs[*]}; do
      ip=\${spec%:*}
      target=\${spec##*:}
      if ssh -o BatchMode=yes -o ConnectTimeout=5 ${pi_user_q}@\"\$ip\" \"sudo -n systemctl start pi-client@\$target.service\"; then
        printf 'started pi=%s target=%s\\n' \"\$ip\" \"\$target\"
      else
        printf 'failed pi=%s target=%s\\n' \"\$ip\" \"\$target\" >&2
        rc=1
      fi
    done
    exit \$rc"
}

cmd_pi_stop() {
  require_pi_ssh_user
  local pi_user_q
  pi_user_q="$(printf '%q' "$PI_SSH_USER")"
  ssh_remote "set +e
    rc=0
    for spec in ${pi_specs[*]}; do
      ip=\${spec%:*}
      target=\${spec##*:}
      if ssh -o BatchMode=yes -o ConnectTimeout=5 ${pi_user_q}@\"\$ip\" \"sudo -n systemctl stop pi-client@\$target.service\"; then
        printf 'stopped pi=%s target=%s\\n' \"\$ip\" \"\$target\"
      else
        printf 'failed pi=%s target=%s\\n' \"\$ip\" \"\$target\" >&2
        rc=1
      fi
    done
    exit \$rc"
}

cmd_display_test() {
  local seconds="${1:-5}"
  case "$seconds" in
    ''|*[!0-9]*) printf '秒数は整数で指定してください\n' >&2; return 2 ;;
  esac
  ssh_remote "cd '$OYAKI_REPO' && timeout --signal=TERM --kill-after=3 $((seconds + 5)) '$OYAKI_REPO/.venv/bin/python' host/standby.py --health-port 5102 --send --pi 192.168.10.101:5000 --pi 192.168.10.102:5000 --pi 192.168.10.103:5000 --pi 192.168.10.104:5000 --seconds $seconds"
}

cmd_standby_start() {
  local args
  args="$(printf '%q ' "${standby_args[@]}")"
  ssh_remote "set -e
    if test -f '$REMOTE_STANDBY_PID'; then
      pid=\$(cat '$REMOTE_STANDBY_PID')
      if test -r /proc/\$pid/cmdline && tr '\0' ' ' </proc/\$pid/cmdline | grep -q 'host/standby.py'; then
        echo 'already running'; exit 1
      fi
      rm -f '$REMOTE_STANDBY_PID'
    fi
    nohup '$OYAKI_REPO/.venv/bin/python' '$OYAKI_REPO/host/standby.py' $args >'$REMOTE_STANDBY_LOG' 2>&1 </dev/null &
    echo \$! >'$REMOTE_STANDBY_PID'
    echo started pid=\$(cat '$REMOTE_STANDBY_PID')"
}

cmd_standby_foreground() {
  local args
  args="$(printf '%q ' "${standby_args[@]}")"
  ssh_remote "cd '$OYAKI_REPO' && '$OYAKI_REPO/.venv/bin/python' '$OYAKI_REPO/host/standby.py' $args"
}

cmd_standby_status() {
  ssh_remote "set -u
    if test -f '$REMOTE_STANDBY_PID'; then
      pid=\$(cat '$REMOTE_STANDBY_PID')
      if test -r /proc/\$pid/cmdline && tr '\0' ' ' </proc/\$pid/cmdline | grep -q 'host/standby.py'; then
        echo \"running pid=\$pid\"
      else
        echo 'not running (stale pid)'
      fi
    else
      echo 'not running'
    fi
    tail -n 5 '$REMOTE_STANDBY_LOG' 2>/dev/null || true"
}

cmd_standby_stop() {
  ssh_remote "set -e
    test -f '$REMOTE_STANDBY_PID' || { echo 'not running'; exit 0; }
    pid=\$(cat '$REMOTE_STANDBY_PID')
    if test -r /proc/\$pid/cmdline && tr '\0' ' ' </proc/\$pid/cmdline | grep -q 'host/standby.py'; then
      kill -TERM \"\$pid\"
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        test -r /proc/\$pid/cmdline || break
        sleep 1
      done
    fi
    rm -f '$REMOTE_STANDBY_PID'
    echo stopped"
}

cmd_depth_view_build() {
  ssh_remote "set -e
    test -f '$OYAKI_REPO/host/structure_depth_view.cpp'
    test -f '$OYAKI_REPO/host/structure_depth_capture.cpp'
    test -f '$OYAKI_REPO/host/sensor_detection_view.py'
    '$OYAKI_REPO/.venv/bin/python' -m py_compile '$OYAKI_REPO/host/sensor_detection_view.py' '$OYAKI_REPO/host/block_breaker.py'
    g++ -std=c++17 -O2 '$OYAKI_REPO/host/structure_depth_view.cpp' -o '$OYAKI_REPO/host/structure_depth_view' \$(pkg-config --cflags --libs opencv4)
    g++ -std=c++17 -O2 '$OYAKI_REPO/host/structure_depth_capture.cpp' -o '$OYAKI_REPO/host/structure_depth_capture' \$(pkg-config --cflags --libs opencv4)
    test -x '$OYAKI_REPO/host/structure_depth_view'
    test -x '$OYAKI_REPO/host/structure_depth_capture'
    echo 'built: sensor_detection_view.py + $OYAKI_REPO/host/structure_depth_view'"
}

cmd_depth_view_start() {
  local args
  args="$(printf '%q ' "${sensor_detection_view_args[@]}")"
  ssh_remote "set -e
    test -x '$OYAKI_REPO/.venv/bin/python'
    test -f '$OYAKI_REPO/host/sensor_detection_view.py'
    if test -f '$REMOTE_DEPTH_VIEW_PID'; then
      pid=\$(cat '$REMOTE_DEPTH_VIEW_PID')
      if test -r /proc/\$pid/cmdline && tr '\0' ' ' </proc/\$pid/cmdline | grep -q 'sensor_detection_view.py'; then
        echo 'already running'; exit 1
      fi
      if test -r /proc/\$pid/cmdline && tr '\0' ' ' </proc/\$pid/cmdline | grep -q 'structure_depth_view'; then
        kill -TERM "\$pid" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
          test -r /proc/\$pid/cmdline || break
          sleep 1
        done
      fi
      rm -f '$REMOTE_DEPTH_VIEW_PID'
    fi
    nohup env PYTHONUNBUFFERED=1 '$OYAKI_REPO/.venv/bin/python' '$OYAKI_REPO/host/sensor_detection_view.py' $args >'$REMOTE_DEPTH_VIEW_LOG' 2>&1 </dev/null &
    echo \$! >'$REMOTE_DEPTH_VIEW_PID'
    echo started pid=\$(cat '$REMOTE_DEPTH_VIEW_PID')"
}

cmd_depth_view_foreground() {
  local args
  args="$(printf '%q ' "${sensor_detection_view_args[@]}")"
  ssh_remote "cd '$OYAKI_REPO' && env PYTHONUNBUFFERED=1 '$OYAKI_REPO/.venv/bin/python' '$OYAKI_REPO/host/sensor_detection_view.py' $args"
}

cmd_depth_view_status() {
  ssh_remote "set -u
    if test -f '$REMOTE_DEPTH_VIEW_PID'; then
      pid=\$(cat '$REMOTE_DEPTH_VIEW_PID')
      if test -r /proc/\$pid/cmdline && tr '\0' ' ' </proc/\$pid/cmdline | grep -Eq 'sensor_detection_view\.py|structure_depth_view'; then
        echo \"running pid=\$pid\"
      else
        echo 'not running (stale pid)'
      fi
    else
      echo 'not running'
    fi
    tail -n 5 '$REMOTE_DEPTH_VIEW_LOG' 2>/dev/null || true"
}

cmd_depth_view_stop() {
  ssh_remote "set -e
    test -f '$REMOTE_DEPTH_VIEW_PID' || { echo 'not running'; exit 0; }
    pid=\$(cat '$REMOTE_DEPTH_VIEW_PID')
    if test -r /proc/\$pid/cmdline && tr '\0' ' ' </proc/\$pid/cmdline | grep -Eq 'sensor_detection_view\.py|structure_depth_view'; then
      kill -TERM \"\$pid\" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        test -r /proc/\$pid/cmdline || break
        sleep 1
      done
    fi
    rm -f '$REMOTE_DEPTH_VIEW_PID'
    echo stopped"
}

cmd_start() {
  local args
  args="$(printf '%q ' "${calibration_args[@]}")"
  ssh_remote "set -e
    if test -f '$REMOTE_PID'; then
      pid=\$(cat '$REMOTE_PID')
      if test -r /proc/\$pid/cmdline && tr '\0' ' ' </proc/\$pid/cmdline | grep -q camera_calibrate.py; then
        echo 'already running'; exit 1
      fi
      rm -f '$REMOTE_PID'
    fi
    nohup env PYTHONUNBUFFERED=1 timeout --signal=TERM --kill-after=10 '$CALIBRATION_MAX_SECONDS' '$OYAKI_REPO/.venv/bin/python' '$OYAKI_REPO/host/camera_calibrate.py' $args >'$REMOTE_LOG' 2>&1 </dev/null &
    echo \$! >'$REMOTE_PID'
    echo started pid=\$(cat '$REMOTE_PID')"
}

cmd_foreground() {
  local args
  args="$(printf '%q ' "${calibration_args[@]}")"
  ssh_remote "cd '$OYAKI_REPO' && timeout --signal=TERM --kill-after=10 '$CALIBRATION_MAX_SECONDS' '$OYAKI_REPO/.venv/bin/python' '$OYAKI_REPO/host/camera_calibrate.py' $args"
}

cmd_status() {
  ssh_remote "set -u
    if test -f '$REMOTE_PID'; then
      pid=\$(cat '$REMOTE_PID')
      if test -r /proc/\$pid/cmdline && tr '\0' ' ' </proc/\$pid/cmdline | grep -q camera_calibrate.py; then
        echo \"running pid=\$pid\"
      else
        echo 'not running (stale pid)'
      fi
    else
      echo 'not running'
    fi
    if test -f '$REMOTE_OUTPUT'; then echo 'valid_json=present'; elif test -f '${REMOTE_OUTPUT%.json}.invalid.json'; then echo 'invalid_json=present'; else echo 'result_json=absent'; fi
    tail -n 5 '$REMOTE_LOG' 2>/dev/null || true"
}

cmd_logs() {
  local lines="${1:-80}"
  case "$lines" in
    ''|*[!0-9]*) printf '行数は整数で指定してください\n' >&2; return 2 ;;
  esac
  ssh_remote "tail -n $lines '$REMOTE_LOG'"
}

cmd_stop() {
  ssh_remote "set -e
    test -f '$REMOTE_PID' || { echo 'not running'; exit 0; }
    pid=\$(cat '$REMOTE_PID')
    if test -r /proc/\$pid/cmdline && tr '\0' ' ' </proc/\$pid/cmdline | grep -q camera_calibrate.py; then
      kill -TERM \"\$pid\"
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        test -r /proc/\$pid/cmdline || break
        sleep 1
      done
    fi
    rm -f '$REMOTE_PID'
    echo stopped"
}

cmd_result() {
  ssh_remote "'$OYAKI_REPO/.venv/bin/python' -c 'import json, pathlib; p=pathlib.Path(\"$REMOTE_OUTPUT\"); q=pathlib.Path(\"${REMOTE_OUTPUT%.json}.invalid.json\"); p=q if not p.exists() else p; o=json.loads(p.read_text(encoding=\"utf-8\")); json.dumps(o, allow_nan=False); print(json.dumps({k:o.get(k) for k in (\"valid\",\"status\",\"sample_counts\",\"quality\",\"sensor\",\"thresholds\")}, ensure_ascii=False, sort_keys=True))'"
}

cmd_fetch() {
  local destination="${1:-}"
  test -n "$destination" || { printf 'fetchには出力先ディレクトリが必要です\n' >&2; return 2; }
  mkdir -p "$destination"
  for name in camera_calibration.json camera_calibration.invalid.json camera_calibrate_run.log; do
    if test -e "$destination/$name"; then printf '既存ファイルを上書きしない: %s\n' "$destination/$name" >&2; return 1; fi
  done
  scp -p -o ConnectTimeout=10 -o HostKeyAlias=192.168.20.1 -o "HostName=$OYAKI_HOSTNAME" \
    "$OYAKI_TARGET:$OYAKI_REPO/camera_calibration.json" \
    "$OYAKI_TARGET:$OYAKI_REPO/camera_calibration.invalid.json" \
    "$OYAKI_TARGET:$REMOTE_LOG" "$destination/" 2>/dev/null || true
  printf 'fetched: %s\n' "$destination"
}

command="${1:-help}"
shift || true
case "$command" in
  help|-h|--help) usage ;;
  check) cmd_check "$@" ;;
  deploy) cmd_deploy "$@" ;;
  pi-deploy) cmd_pi_deploy "$@" ;;
  pi-status) cmd_pi_status "$@" ;;
  pi-start) cmd_pi_start "$@" ;;
  pi-stop) cmd_pi_stop "$@" ;;
  display-test) cmd_display_test "$@" ;;
  standby-start) cmd_standby_start "$@" ;;
  standby-foreground) cmd_standby_foreground "$@" ;;
  standby-status) cmd_standby_status "$@" ;;
  standby-stop) cmd_standby_stop "$@" ;;
  depth-view-build) cmd_depth_view_build "$@" ;;
  depth-view-start) cmd_depth_view_start "$@" ;;
  depth-view-foreground) cmd_depth_view_foreground "$@" ;;
  depth-view-status) cmd_depth_view_status "$@" ;;
  depth-view-stop) cmd_depth_view_stop "$@" ;;
  start) cmd_start "$@" ;;
  foreground) cmd_foreground "$@" ;;
  status) cmd_status "$@" ;;
  logs) cmd_logs "$@" ;;
  stop) cmd_stop "$@" ;;
  result) cmd_result "$@" ;;
  fetch) cmd_fetch "$@" ;;
  *) usage >&2; exit 2 ;;
esac
