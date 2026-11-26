"""Pure maths for ranged combat: ballistic aim with target lead, flight calibration, miss correction and
firing-position sampling. No game access, so every function is unit tested.

Units are game units (1 unit ~ 1.4 cm), seconds and degrees. Headings are clockwise from north (+Y), pitch is
positive upwards. Projectile forms store gravity as a multiple of 9.81 m/s^2, which is GRAVITY_UNITS per 1.0.
"""

import math

GRAVITY_UNITS = 686.7
LATENCY_S = 0.12
MIN_RANGE = 600.0
MAX_RANGE = 1500.0


def distance(a, b):
    return math.dist(a, b)


def heading_deg(origin, point):
    return math.degrees(math.atan2(point[0] - origin[0], point[1] - origin[1])) % 360.0


def wrap180(degrees):
    return (degrees + 180.0) % 360.0 - 180.0


def solve_pitch(flat, rise, speed, gravity):
    """Launch pitch (degrees, low arc) that lands on a point `flat` away horizontally and `rise` above the
    origin, and the flight time. Returns (pitch, time, reachable); out of range gives the 45 degree best effort."""
    flat = max(flat, 1e-6)
    if gravity <= 1e-9:
        pitch = math.atan2(rise, flat)
        return math.degrees(pitch), math.hypot(flat, rise) / speed, True
    v2 = speed * speed
    disc = v2 * v2 - gravity * (gravity * flat * flat + 2.0 * rise * v2)
    if disc < 0:
        pitch = math.radians(45.0)
        return 45.0, flat / (speed * math.cos(pitch)), False
    pitch = math.atan((v2 - math.sqrt(disc)) / (gravity * flat))
    return math.degrees(pitch), flat / (speed * math.cos(pitch)), True


def aim_at(origin, target, speed, gravity, target_velocity=(0.0, 0.0, 0.0), latency=LATENCY_S, iterations=5):
    """Heading, pitch and flight time to hit a target moving at constant velocity.

    The target is advanced by flight time plus `latency` (the delay between choosing the aim and the release)
    and the solve repeated, which converges in a few rounds for any target slower than the projectile.
    Returns a dict with heading, pitch, time, reachable, point (the predicted aim point) and flat range.
    """
    point = tuple(target)
    pitch, tof, ok = 0.0, 0.0, True
    for _ in range(max(1, iterations)):
        flat = math.hypot(point[0] - origin[0], point[1] - origin[1])
        pitch, tof, ok = solve_pitch(flat, point[2] - origin[2], speed, gravity)
        point = tuple(target[i] + target_velocity[i] * (tof + latency) for i in range(3))
    flat = math.hypot(point[0] - origin[0], point[1] - origin[1])
    return {"heading": heading_deg(origin, point), "pitch": pitch, "time": tof, "reachable": ok,
            "point": point, "range": flat}


def estimate_velocity(samples):
    """Velocity from [(t, (x, y, z)), ...]: the displacement over the longest span, which beats pairwise noise."""
    if len(samples) < 2:
        return (0.0, 0.0, 0.0)
    (t0, p0), (t1, p1) = samples[0], samples[-1]
    span = t1 - t0
    if span <= 1e-6:
        return (0.0, 0.0, 0.0)
    return tuple((p1[i] - p0[i]) / span for i in range(3))


def launch_elevation(velocity):
    flat = math.hypot(velocity[0], velocity[1])
    return math.degrees(math.atan2(velocity[2], flat))


def fit_flight(samples):
    """Speed, gravity (units/s^2) and launch elevation from [(t, pos, velocity), ...] of one projectile.

    Speed and elevation come from the first moving sample; gravity from the vertical-velocity slope over the
    samples (needs at least 3 spanning 0.1 s). Missing pieces are None.
    """
    moving = [s for s in samples if math.hypot(*s[2]) > 100.0]
    if not moving:
        return {"speed": None, "gravity": None, "elevation": None}
    first = moving[0]
    out = {"speed": math.hypot(*first[2]), "gravity": None, "elevation": launch_elevation(first[2])}
    if len(moving) >= 3 and moving[-1][0] - moving[0][0] >= 0.1:
        # least squares slope of vz against t
        n = len(moving)
        mt = sum(s[0] for s in moving) / n
        mv = sum(s[2][2] for s in moving) / n
        den = sum((s[0] - mt) ** 2 for s in moving)
        if den > 1e-9:
            slope = sum((s[0] - mt) * (s[2][2] - mv) for s in moving) / den
            if -slope > 0:
                out["gravity"] = -slope
    return out


def miss_at_range(path, origin, aim_point):
    """Where a flown path passed the aim point: (vertical, lateral) error at the aim point's horizontal range from
    the origin, positive = above / to the right of the line of sight. None when the path ended short of it."""
    want = math.hypot(aim_point[0] - origin[0], aim_point[1] - origin[1])
    if want < 1.0 or len(path) < 2:
        return None
    bearing = math.atan2(aim_point[0] - origin[0], aim_point[1] - origin[1])
    right = (math.cos(bearing), -math.sin(bearing))
    prev = None
    for point in path:
        flat = math.hypot(point[0] - origin[0], point[1] - origin[1])
        if prev is not None and prev[0] < want <= flat:
            span = flat - prev[0]
            f = (want - prev[0]) / span if span > 1e-9 else 0.0
            p = tuple(prev[1][i] + (point[i] - prev[1][i]) * f for i in range(3))
            lateral = (p[0] - aim_point[0]) * right[0] + (p[1] - aim_point[1]) * right[1]
            return p[2] - aim_point[2], lateral
        prev = (flat, point)
    return None


def trim_for_miss(vertical, lateral, flat, gain=0.6, limit=6.0):
    """Pitch and heading corrections (degrees to add to the next aim) for a miss of `vertical` above and
    `lateral` to the right of the aim point at range `flat`."""
    flat = max(flat, 1.0)
    clamp = lambda v: max(-limit, min(limit, v))
    return clamp(-math.degrees(math.atan2(vertical, flat)) * gain), clamp(-math.degrees(math.atan2(lateral, flat)) * gain)


def sample_positions(player, target, radius=600.0, rings=(150.0, 300.0, 450.0, 600.0), directions=16,
                     min_range=MIN_RANGE, max_range=MAX_RANGE):
    """Candidate firing positions around the player, best first.

    Points on rings within `radius` of the player, scored by how far the player must walk and by how far the
    resulting range to the target lies outside min_range..max_range (a candidate inside the band costs nothing
    for range). The caller filters them by line of sight and navmesh reachability.
    """
    out = []
    for ring in rings:
        for k in range(directions):
            angle = 2.0 * math.pi * k / directions
            x, y = player[0] + ring * math.sin(angle), player[1] + ring * math.cos(angle)
            flat = math.hypot(target[0] - x, target[1] - y)
            outside = max(min_range - flat, flat - max_range, 0.0)
            out.append({"pos": (x, y, player[2]), "range": flat, "walk": ring, "score": ring / radius + outside / 400.0})
    out.sort(key=lambda c: (c["score"], c["walk"]))
    return out


def default_draw_s(bow_speed):
    """Hold time for a full draw: a little over the animation's length, never under 1.5 s."""
    speed = bow_speed if bow_speed and bow_speed > 0.2 else 1.0
    return max(1.5, 1.1 / speed + 0.5)
