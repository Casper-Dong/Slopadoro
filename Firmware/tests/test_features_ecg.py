from __future__ import annotations

import numpy as np

from slopodoro_acq.features_ecg import compute_ecg_features


def test_ecg_hrv_features_from_rr_intervals() -> None:
    rr = np.asarray([800.0, 820.0, 780.0, 810.0, 790.0, 805.0])
    frame = compute_ecg_features(timestamp=10.0, session_id="test", rr_ms=rr)
    features = frame["features"]
    assert 72.0 < features["ecg.heart_rate"] < 77.0
    assert features["ecg.rmssd_ms"] > 0.0
    assert features["ecg.sdnn"] > 0.0
    assert frame["validity"]["hrv_window_valid"] is True
