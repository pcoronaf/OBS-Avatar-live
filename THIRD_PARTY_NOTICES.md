# Third-party notices

OBS Avatar Live is licensed under GPL-2.0-or-later (see `LICENSE`). It uses or is built with the following
third-party components. Licenses must be re-verified for every release; see `docs/LICENSE_REVIEW.md`.

| Component | Use | License | Source |
|---|---|---|---|
| OBS Studio / libobs, obs-frontend-api | Plugin host API (dynamic link) | GPL-2.0-or-later | https://github.com/obsproject/obs-studio |
| obs-plugintemplate (cmake/, build-aux/, .github/) | Build system | GPL-2.0 | https://github.com/obsproject/obs-plugintemplate |
| Qt 6 (via obs-deps) | Optional dock UI | LGPL-3.0 | https://www.qt.io |
| ONNX Runtime | Inference | MIT | https://github.com/microsoft/onnxruntime |
| ONNX Runtime OpenVINO EP / OpenVINO | Optional inference (Intel) | MIT / Apache-2.0 | https://github.com/openvinotoolkit/openvino |
| ONNX Runtime DirectML / DirectML | Optional inference (iGPU) | MIT / Microsoft redistribution terms (verify) | https://github.com/microsoft/DirectML |
| OpenCV (opencv-python-headless) | Capture, image ops, face detection | Apache-2.0 | https://github.com/opencv/opencv-python |
| OpenCV Haar cascades | Fallback face detector | Intel License Agreement (BSD-style, in XML header) | https://github.com/opencv/opencv/tree/4.x/data/haarcascades |
| NumPy | Numerics | BSD-3-Clause | https://numpy.org |
| python-sounddevice / PortAudio | Microphone capture | MIT / MIT-style | https://github.com/spatialaudio/python-sounddevice |
| PyInstaller | Engine packaging | GPL-2.0 with bootloader exception | https://pyinstaller.org |

## Models (not bundled)

No model weights are distributed with this project. Models the user may supply are listed with their declared
licenses in `models/MODEL_MANIFEST.json`. InsightFace-derived weights (recognition, `inswapper`) are declared by their
authors for **non-commercial research use only**.

## Reference projects (no code copied)

- Deep-Live-Cam (AGPL-3.0), https://github.com/hacksider/Deep-Live-Cam — architecture analysis only.
- InsightFace, https://github.com/deepinsight/insightface — model format reference only.
