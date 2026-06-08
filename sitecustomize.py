from __future__ import annotations

import builtins
import os

# Compatibility guard for legacy split-cache warmers.
# Strict false: no synthetic/mock bait, boater, shark, or ocean fallback.
builtins.ALLOW_SYNTHETIC_FALLBACK = False

os.environ.setdefault("ALLOW_SYNTHETIC_FALLBACK", "0")
os.environ.setdefault("GFS_ALLOW_SYNTHETIC_FALLBACK", "0")
os.environ.setdefault("GFS_ALLOW_OISST_SST_FALLBACK", "false")

# Small-host netCDF/HDF5 stability.
os.environ.setdefault("HDF5_USE_FILE_LOCKING", "FALSE")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
