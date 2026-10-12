"""Check the ADX implementation on a controlled series."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ts.indicators import adx, atr, true_range  # noqa: E402

n = 60
# clean monotone uptrend: a textbook strong trend, ADX should be high
up = np.linspace(100, 160, n)
idx = pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC")
frame = pd.DataFrame({
    "open": up,
    "high": up * 1.001,
    "low": up * 0.999,
    "close": up,
    "volume": np.ones(n),
}, index=idx)

a = adx(frame, 14)
print("monotone uptrend ADX tail:")
print(a.tail(10).to_string())
print("non-nan count:", int(a.notna().sum()), "of", n)

print("\natr tail:")
print(atr(frame, 14).tail(4).to_string())

print("\ntrue_range tail:")
print(true_range(frame).tail(4).to_string())
