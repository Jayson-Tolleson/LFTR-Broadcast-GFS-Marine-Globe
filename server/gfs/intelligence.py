from __future__ import annotations

from typing import Any

from .waterbody import classify_waterbody_geometry


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    try:
        n = float(value)
    except Exception:
        return lo
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


def _joined_text(loc: dict[str, Any], extra_reports: list[str] | None = None) -> str:
    chunks: list[str] = []
    for part in [loc.get("name"), *(loc.get("all_reports") or []), *(extra_reports or [])]:
        text = str(part or "").strip()
        if text:
            chunks.append(text.lower())
    return " \n ".join(chunks)


HABITAT_PROFILES: dict[str, dict[str, Any]] = {
    "freshwater": {
        "label": "Inland freshwater",
        "summary": "Freshwater profile active — trout / bass / catfish logic should lead this node.",
        "species": [
            {"key": "trout", "label": "Trout", "temp_center_f": 57, "temp_half_span_f": 12, "structure_bias": 0.18, "current_bias": 0.06, "bait_bias": 0.22, "report_terms": ["trout", "stocked", "powerbait", "salmon egg"]},
            {"key": "largemouth_bass", "label": "Largemouth", "temp_center_f": 67, "temp_half_span_f": 14, "structure_bias": 0.28, "current_bias": 0.04, "bait_bias": 0.24, "report_terms": ["largemouth", "bucketmouth", "bass"]},
            {"key": "catfish", "label": "Catfish", "temp_center_f": 72, "temp_half_span_f": 16, "structure_bias": 0.16, "current_bias": 0.02, "bait_bias": 0.20, "report_terms": ["catfish", "channel cat", "bullhead"]},
            {"key": "striper", "label": "Striper", "temp_center_f": 64, "temp_half_span_f": 12, "structure_bias": 0.18, "current_bias": 0.16, "bait_bias": 0.24, "report_terms": ["striper", "striped bass"]},
        ],
        "tackle": [
            "Trout bait / mini jig / slip float at low light",
            "Senko / swimbait / live bait around points and rock",
            "Cut bait / stink bait / night soak for cats",
        ],
    },
    "surf": {
        "label": "Surf / shoreline",
        "summary": "Shoreline salt profile active — surf species and wash-zone structure matter here.",
        "species": [
            {"key": "corbina", "label": "Corbina", "temp_center_f": 66, "temp_half_span_f": 10, "structure_bias": 0.18, "current_bias": 0.10, "bait_bias": 0.22, "report_terms": ["corbina"]},
            {"key": "halibut", "label": "Halibut", "temp_center_f": 62, "temp_half_span_f": 10, "structure_bias": 0.28, "current_bias": 0.08, "bait_bias": 0.26, "report_terms": ["halibut"]},
            {"key": "surfperch", "label": "Surfperch", "temp_center_f": 61, "temp_half_span_f": 12, "structure_bias": 0.14, "current_bias": 0.08, "bait_bias": 0.20, "report_terms": ["perch", "surfperch"]},
            {"key": "shark", "label": "Shark", "temp_center_f": 64, "temp_half_span_f": 14, "structure_bias": 0.12, "current_bias": 0.10, "bait_bias": 0.24, "report_terms": ["shark", "ray", "leopard"]},
        ],
        "tackle": [
            "Carolina rig / sand crab / gulp in the trough",
            "Swimbait or live bait on the edge for halibut",
            "Heavier cut bait only if shark sign is active",
        ],
    },
    "pier_bay": {
        "label": "Pier / bay / harbor",
        "summary": "Bay-pier profile active — mixed inshore predators, bait schools, and structure edges matter here.",
        "species": [
            {"key": "mackerel", "label": "Mackerel", "temp_center_f": 63, "temp_half_span_f": 12, "structure_bias": 0.10, "current_bias": 0.12, "bait_bias": 0.28, "report_terms": ["mackerel", "mack", "smelt", "sardine", "anchovy"]},
            {"key": "bass", "label": "Bass", "temp_center_f": 62, "temp_half_span_f": 11, "structure_bias": 0.26, "current_bias": 0.08, "bait_bias": 0.22, "report_terms": ["bass", "calico", "spotted bay", "sand bass", "kelp bass"]},
            {"key": "barracuda", "label": "Barracuda", "temp_center_f": 66, "temp_half_span_f": 10, "structure_bias": 0.16, "current_bias": 0.12, "bait_bias": 0.24, "report_terms": ["barracuda", "cuda"]},
            {"key": "halibut", "label": "Halibut", "temp_center_f": 62, "temp_half_span_f": 9, "structure_bias": 0.28, "current_bias": 0.06, "bait_bias": 0.22, "report_terms": ["halibut"]},
        ],
        "tackle": [
            "Sabiki / small bait rig when bait is flashing",
            "Live bait or small jig around pilings and seams",
            "Carolina rig or swimbait for halibut lanes",
        ],
    },
    "offshore": {
        "label": "Offshore bluewater",
        "summary": "Offshore profile active — current seams, temp breaks, and pelagic bait lanes drive the node.",
        "species": [
            {"key": "tuna", "label": "Tuna", "temp_center_f": 66, "temp_half_span_f": 8, "structure_bias": 0.08, "current_bias": 0.18, "bait_bias": 0.30, "report_terms": ["tuna", "bluefin", "yellowfin"]},
            {"key": "yellowtail", "label": "Yellowtail", "temp_center_f": 64, "temp_half_span_f": 9, "structure_bias": 0.18, "current_bias": 0.14, "bait_bias": 0.26, "report_terms": ["yellowtail", "yt"]},
            {"key": "dorado", "label": "Dorado", "temp_center_f": 71, "temp_half_span_f": 8, "structure_bias": 0.08, "current_bias": 0.16, "bait_bias": 0.26, "report_terms": ["dorado", "mahi"]},
            {"key": "shark", "label": "Shark", "temp_center_f": 65, "temp_half_span_f": 13, "structure_bias": 0.10, "current_bias": 0.10, "bait_bias": 0.24, "report_terms": ["shark", "mako", "thresher"]},
        ],
        "tackle": [
            "Flylined sardine / sinker rig / knife jig",
            "Surface iron or colt sniper if breezers show",
            "Heavier leader only when shark sign or teeth show",
        ],
    },
    "coastal_general": {
        "label": "Coastal saltwater",
        "summary": "Coastal profile active — mixed nearshore salt species are weighted instead of inland fish.",
        "species": [
            {"key": "mackerel", "label": "Mackerel", "temp_center_f": 63, "temp_half_span_f": 12, "structure_bias": 0.10, "current_bias": 0.12, "bait_bias": 0.28, "report_terms": ["mackerel", "mack"]},
            {"key": "bass", "label": "Bass", "temp_center_f": 62, "temp_half_span_f": 10, "structure_bias": 0.26, "current_bias": 0.08, "bait_bias": 0.22, "report_terms": ["bass", "calico", "kelp bass", "sand bass"]},
            {"key": "barracuda", "label": "Barracuda", "temp_center_f": 66, "temp_half_span_f": 10, "structure_bias": 0.14, "current_bias": 0.12, "bait_bias": 0.24, "report_terms": ["barracuda", "cuda"]},
            {"key": "halibut", "label": "Halibut", "temp_center_f": 62, "temp_half_span_f": 9, "structure_bias": 0.28, "current_bias": 0.06, "bait_bias": 0.22, "report_terms": ["halibut"]},
            {"key": "corbina", "label": "Corbina", "temp_center_f": 66, "temp_half_span_f": 10, "structure_bias": 0.16, "current_bias": 0.08, "bait_bias": 0.20, "report_terms": ["corbina"]},
            {"key": "shark", "label": "Shark", "temp_center_f": 64, "temp_half_span_f": 14, "structure_bias": 0.10, "current_bias": 0.10, "bait_bias": 0.24, "report_terms": ["shark", "ray", "leopard"]},
        ],
        "tackle": [
            "Sabiki or small bait rig if bait is present",
            "Swimbait / Carolina rig for bass and halibut lanes",
            "Upgrade leader when cuda or shark sign shows",
        ],
    },
}


def classify_habitat(loc: dict[str, Any], extra_reports: list[str] | None = None) -> dict[str, Any]:
    text = _joined_text(loc, extra_reports)
    geometry = classify_waterbody_geometry(loc)

    freshwater_terms = ["lake", "river", "reservoir", "lagoon", "creek", "trout", "catfish", "largemouth", "smallmouth", "striper"]
    surf_terms = ["beach", "surf", "shore", "strand", "corbina", "sand crab"]
    pier_terms = ["pier", "dock", "harbor", "bay", "jetty", "marina", "wharf"]
    offshore_terms = ["bank", "island", "offshore", "bluefin", "yellowfin", "paddy", "kelp paddy", "tuna"]

    if any(term in text for term in freshwater_terms):
        geometry.update({
            "habitat_key": "freshwater",
            "waterbody": HABITAT_PROFILES["freshwater"]["label"],
            "method": "text_override",
            "classification_reason": "Freshwater keywords in the location/report text override the geometric fallback.",
        })
    elif any(term in text for term in offshore_terms):
        geometry.update({
            "habitat_key": "offshore",
            "waterbody": HABITAT_PROFILES["offshore"]["label"],
            "method": "text_override",
            "classification_reason": "Offshore/pelagic keywords in the location/report text override the geometric fallback.",
        })
    elif any(term in text for term in surf_terms) and geometry.get("habitat_key") != "pier_bay":
        geometry.update({
            "habitat_key": "surf",
            "waterbody": HABITAT_PROFILES["surf"]["label"],
            "method": "text_override",
            "classification_reason": "Surf-zone keywords in the location/report text override the geometric fallback.",
        })
    elif any(term in text for term in pier_terms):
        geometry.update({
            "habitat_key": "pier_bay",
            "waterbody": HABITAT_PROFILES["pier_bay"]["label"],
            "method": "text_override",
            "classification_reason": "Pier/harbor keywords in the location/report text override the geometric fallback.",
        })
    return geometry


KEYWORD_SPECIES_MAP = {
    "trout": "trout",
    "catfish": "catfish",
    "largemouth": "largemouth_bass",
    "smallmouth": "largemouth_bass",
    "striper": "striper",
    "mackerel": "mackerel",
    "mack": "mackerel",
    "barracuda": "barracuda",
    "cuda": "barracuda",
    "halibut": "halibut",
    "corbina": "corbina",
    "shark": "shark",
    "ray": "shark",
    "bass": "bass",
    "calico": "bass",
    "kelp bass": "bass",
    "sand bass": "bass",
    "tuna": "tuna",
    "bluefin": "tuna",
    "yellowfin": "tuna",
    "yellowtail": "yellowtail",
    "dorado": "dorado",
    "mahi": "dorado",
}


def build_location_profile(loc: dict[str, Any], extra_reports: list[str] | None = None) -> dict[str, Any]:
    classification = classify_habitat(loc, extra_reports)
    habitat_key = classification.get("habitat_key", "coastal_general")
    profile = HABITAT_PROFILES.get(habitat_key, HABITAT_PROFILES["coastal_general"])
    text = _joined_text(loc, extra_reports)

    evidence: dict[str, int] = {}
    for token, species_key in KEYWORD_SPECIES_MAP.items():
        if token in text:
            evidence[species_key] = evidence.get(species_key, 0) + text.count(token)

    species = []
    for idx, spec in enumerate(profile["species"]):
        hint_boost = min(16, evidence.get(spec["key"], 0) * 6)
        report_hit = any(term in text for term in spec.get("report_terms", []))
        species.append({
            **spec,
            "rank": idx + 1,
            "hint_boost": hint_boost,
            "report_hit": report_hit,
        })

    headline = ", ".join(s["label"] for s in species[:4])
    return {
        "location_id": loc.get("id"),
        "habitat_key": habitat_key,
        "waterbody": profile["label"],
        "summary": profile["summary"],
        "species": species,
        "headline_species": headline,
        "tackle_hints": profile["tackle"],
        "report_evidence": evidence,
        "classification_method": classification.get("method"),
        "classification_reason": classification.get("classification_reason"),
        "matched_zone": classification.get("matched_zone"),
        "coast_distance_deg": classification.get("coast_distance_deg"),
    }
