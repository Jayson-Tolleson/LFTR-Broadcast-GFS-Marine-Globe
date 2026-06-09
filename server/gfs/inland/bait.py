"""Inland bait helpers.

Inland bait intentionally follows the same contour/marching-square idea as the
ocean bait layer, but clips to ready lake/river shoreline vertices.
"""
from __future__ import annotations

BAIT_CONTRACT = "marching_square_bait_over_ready_inland_water_vertices"
