"""HYCOM ocean provider facade.

HYCOM is the first-class ocean provider for LFTR GFS ocean intelligence.

This module wraps the historical ``RtofsProvider`` implementation without
duplicating it. The old class name remains available for compatibility, but
new code should import ``HycomProvider``.

Provider contract:
    HycomProvider.subset(...)
        -> finite HYCOM SST / SSS / SSU / SSV grid
        -> oceanAnalysisPoints for boats, shark, HUD, current squares
        -> advancedBaitRows for dense bait probability contours
"""

from __future__ import annotations

from server.gfs.providers.rtofs import RtofsProvider as _LegacyRtofsProvider


class HycomProvider(_LegacyRtofsProvider):
    """First-class HYCOM provider facade around the legacy implementation."""

    provider_name = "hycom"
    provider_contract = "hycom_first_class_ocean_provider_sst_sss_ssu_ssv"


# Backwards-compatible aliases during the naming transition.
RtofsProvider = HycomProvider
OceanProvider = HycomProvider
