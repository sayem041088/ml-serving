"""Staged load profiles: lists of (duration_seconds, users, spawn_rate).

Kept separate from the locustfile so scripts can read stage boundaries without
importing Locust.
"""

PROFILES: dict[str, list[tuple[int, int, float]]] = {
    # Quick end-to-end check after a deploy.
    "smoke": [
        (60, 5, 5),
    ],
    # Section 14 of the project plan: baseline -> stress -> recovery.
    "staged": [
        (120, 10, 2),  # baseline
        (180, 50, 5),  # low
        (300, 100, 10),  # medium
        (300, 300, 20),  # high
        (300, 500, 25),  # stress
        (300, 50, 50),  # recovery: watch HPA and cluster autoscaler scale down
    ],
    # Section 24: step to saturation to find the capacity limit.
    "performance": [
        (180, 100, 10),
        (300, 300, 20),
        (300, 500, 25),
        (300, 1000, 50),
    ],
    # Single burst, for comparing HPA targets / pod sizes (section 25).
    "spike": [
        (60, 10, 10),
        (300, 300, 100),
        (240, 10, 100),
    ],
}
