"""OBS Avatar Live engine (out-of-process; OBS never runs inference)."""

import os as _os

# OpenCV's Media Foundation backend uses hardware transforms by default, which makes opening some
# laptop/USB cameras fail or hang on Windows. Must be set before cv2 is first imported.
_os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

__version__ = "0.1.4"
PROTOCOL_VERSION = 1
