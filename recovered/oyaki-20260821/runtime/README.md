# 親機実行時情報

取得日: 2026-08-21

現在の親機では、姿勢推定ゲームが次の引数で実行されていた。

```text
.venv/bin/python host/pose_game.py --camera /dev/v4l/by-id/usb-e-con_systems_See3CAM_130_311CC209-video-index0 --pose-calibration pose_calibration.json --exposure 1/312/2 --position-gain 0.92 --position-deadzone 1.0 --no-preview --send --pi 192.168.10.101:5000 --pi 192.168.10.102:5000 --pi 192.168.10.103:5000 --pi 192.168.10.104:5000
```

取得時のプロセスは親プロセスPID 13854、ゲームプロセスPID 13855。ゲームプロセスのCPU使用率は約235%だった。

## 取得した主要ファイルのSHA-256

```text
host/block_breaker.py f5a8a9d79fb48734deae3869c08997efeda811626a5cea24bb16f097658f16e4
host/block_breaker_selftest.py fd777209471f75b6b242bf2f24cade1a4d47bd2ab1a1a7ae66993e2d93baa19b
host/camera_calibrate.py b7b91f9d28fac66172eed3641695bf4191bb419aab6eda0aa220d936b95c9c80
host/pose_calibrate.py 3261f43e1ef6541da9eac3d230d63b550337c49399c2b642fa1f59544ada3b91
host/pose_input.py f6013c903113f68a97a2af38153f14a78b2b103f8d8d666ea6b3b724562f5bd0
host/pose_game.py a1e17916bbac423e9365817b3ba944823c84e9dd9f57d6b8fc0e01dd2e3c9a7c
host/pose_runtime.py 31b58e86088786d15cca93373ec5525e1a17d8a5e3bfd2802326ba2e0abd2120
pose_calibration.json 687487623f857acc7bfdf0256365f39695d75ed0528b749680d611c6d3164d00
camera_calibration.json 7e13165fbb975e7a0060c4fd130def12d36136f2ca25ac31240e1ec5afbf4f91
host/models/person_detection.onnx 47fd5599d6fa17608f03e0eb0ae230baa6e597d7e8a2c8199fe00abea55a701f
host/models/pose_estimation.onnx 9d89c599319a18fb7d2e28451a883476164543182bafca5f09eb2cf767ed2f3f
```
