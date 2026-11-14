"""Robot layer: one twin, two worlds.

Every motion step runs against a target:
  "sim"  -> Cyberwave digital twin, simulation state (source_type="sim_tele")
  "live" -> the real robot behind the same (or a paired) twin (source_type="tele")

Steps are plain JSON so a sim rehearsal can be stored in Supabase and replayed on
the real robot byte for byte:
  {"type": "joints",  "targets_deg": {"<joint name or index>": deg}, "duration": s}
  {"type": "gripper", "action": "open" | "close"}
  {"type": "wait",    "seconds": s}
  {"type": "base",    "forward_m": m, "turn_deg": deg}
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("twingate.robot")

RATE_HZ = 12            # interpolation rate for joint moves
MAX_DEG_PER_S = 120.0   # safety: max commanded joint speed
DEFAULT_LIMIT_DEG = float(os.getenv("JOINT_LIMIT_DEG", "110"))
GEOFENCE_M = float(os.getenv("GEOFENCE_M", "1.5"))

PoseCallback = Callable[[str, dict[str, float]], None]

# Ward runner routes in the pharmacy cell (x, y on the floor, from build_scene.py waypoints).
RUNNER_HOME = (-0.95, 0.55)
RUNNER_ROUTES = {"ward_3": [(-1.9, 0.55), (-2.55, 0.55)]}

# SO-101 ships joints as _1.._6; give the planner meaningful names.
SO101_ALIASES = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]


class StepError(Exception):
    """A step the twin refused (limit, speed, unknown joint...)."""


@dataclass
class Backend:
    """Common joint bookkeeping. Subclasses publish to a real transport."""

    names: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)  # alias -> real joint name
    limits: dict[str, tuple[float, float]] = field(default_factory=dict)  # degrees
    has_gripper: bool = False
    has_base: bool = False
    asset: str = "robot arm"
    state: dict[str, dict[str, float]] = field(default_factory=dict)      # target -> joint -> deg
    base_pose: dict[str, dict[str, float]] = field(default_factory=dict)  # target -> x,y,yaw
    stop_event: threading.Event = field(default_factory=threading.Event)   # E-STOP from Supabase

    # --- transport hooks -------------------------------------------------
    def _publish_joints(self, target: str, deg: dict[str, float]) -> None: ...
    def _publish_gripper(self, target: str, action: str) -> None: ...
    def _publish_base(self, target: str, forward_m: float, turn_deg: float) -> None: ...
    def frame_jpeg(self) -> Optional[bytes]:
        return None

    def _publish_runner(self, target: str, to: str) -> None:
        time.sleep(1.0)

    # --- helpers ---------------------------------------------------------
    def resolve(self, key: str) -> str:
        if key in self.names:
            return key
        if str(key).lstrip("-").isdigit() and 0 <= int(key) < len(self.names):
            return self.names[int(key)]
        low = {n.lower(): n for n in self.names}
        if str(key).lower() in low:
            return low[str(key).lower()]
        if str(key).lower() in self.aliases:
            return self.aliases[str(key).lower()]
        raise StepError(f"unknown joint {key!r}; joints are {self.names}")

    def label(self, name: str) -> str:
        return next((a for a, real in self.aliases.items() if real == name), name)

    def current(self, target: str) -> dict[str, float]:
        return self.state.setdefault(target, {n: 0.0 for n in self.names})

    def describe(self) -> str:
        rows = []
        for i, n in enumerate(self.names):
            lo, hi = self.limits.get(n, (-DEFAULT_LIMIT_DEG, DEFAULT_LIMIT_DEG))
            alias = next((a for a, real in self.aliases.items() if real == n), None)
            label = f"{alias} (id {n})" if alias else n
            rows.append(f"  [{i}] {label}: limits {lo:.0f}..{hi:.0f} deg, now {self.current('sim').get(n, 0):.0f} deg")
        extra = []
        if self.has_gripper:
            extra.append("gripper: open/close")
        if self.has_base:
            extra.append(f"mobile base: forward/turn, geofence {GEOFENCE_M} m radius")
        if getattr(self, "has_runner", False):
            extra.append(f"ward runner robot (TurtleBot3): destinations {sorted(RUNNER_ROUTES)}")
        return f"Robot: {self.asset}\nJoints:\n" + "\n".join(rows) + ("\n" + "\n".join(extra) if extra else "")

    # --- the one executor used by both rehearsal and live -----------------
    def check_step(self, step: dict[str, Any], target: str) -> list[str]:
        """Return a list of violations for a step without executing it."""
        problems: list[str] = []
        kind = step.get("type")
        if kind == "joints":
            dur = float(step.get("duration", 1.0))
            cur = self.current(target)
            for k, v in (step.get("targets_deg") or {}).items():
                try:
                    name = self.resolve(k)
                except StepError as e:
                    problems.append(str(e))
                    continue
                lo, hi = self.limits.get(name, (-DEFAULT_LIMIT_DEG, DEFAULT_LIMIT_DEG))
                if not (lo <= float(v) <= hi):
                    problems.append(f"{self.label(name)} -> {float(v):.0f} deg is outside its limits {lo:.0f}..{hi:.0f}")
                speed = abs(float(v) - cur.get(name, 0.0)) / max(dur, 0.05)
                if speed > MAX_DEG_PER_S:
                    problems.append(
                        f"{self.label(name)} would move at {speed:.0f} deg/s (max {MAX_DEG_PER_S:.0f}); use a longer duration"
                    )
        elif kind == "base":
            if not self.has_base:
                problems.append("this robot has no mobile base")
            pose = dict(self.base_pose.get(target, {"x": 0.0, "y": 0.0, "yaw": 0.0}))
            yaw = math.radians(pose["yaw"] + float(step.get("turn_deg", 0)))
            x = pose["x"] + math.cos(yaw) * float(step.get("forward_m", 0))
            y = pose["y"] + math.sin(yaw) * float(step.get("forward_m", 0))
            if math.hypot(x, y) > GEOFENCE_M:
                problems.append(f"base would leave the {GEOFENCE_M} m geofence (to {x:.2f}, {y:.2f})")
        elif kind == "runner":
            if not getattr(self, "has_runner", False):
                problems.append("no ward runner in this cell")
            elif step.get("to") not in RUNNER_ROUTES:
                problems.append(f"unknown destination {step.get('to')!r}; known: {sorted(RUNNER_ROUTES)}")
        elif kind == "gripper":
            if step.get("action") not in ("open", "close"):
                problems.append("gripper action must be open or close")
        elif kind == "wait":
            if not 0 <= float(step.get("seconds", 0)) <= 5:
                problems.append("wait must be 0..5 s")
        else:
            problems.append(f"unknown step type {kind!r}")
        return problems

    def _guard(self) -> None:
        if self.stop_event.is_set():
            raise StepError("E-STOP engaged: motion halted")

    def run_step(self, step: dict[str, Any], target: str, on_pose: Optional[PoseCallback] = None) -> None:
        self._guard()
        problems = self.check_step(step, target)
        if problems:
            raise StepError("; ".join(problems))
        kind = step["type"]
        if kind == "joints":
            cur = self.current(target)
            goal = dict(cur)
            for k, v in step["targets_deg"].items():
                goal[self.resolve(k)] = float(v)
            dur = max(0.1, float(step.get("duration", 1.0)))
            n = max(1, int(dur * RATE_HZ))
            start = dict(cur)
            emit_every = max(1, n // 4)
            for i in range(1, n + 1):
                self._guard()
                t = i / n
                s = t * t * (3 - 2 * t)  # smoothstep
                frame = {j: start[j] + (goal[j] - start[j]) * s for j in goal}
                self._publish_joints(target, frame)
                self.state[target] = frame
                if on_pose and (i % emit_every == 0 or i == n):
                    on_pose(target, frame)
                time.sleep(dur / n)
        elif kind == "gripper":
            if "gripper" in self.aliases or "gripper" in self.names:
                # Gripper is a joint (SO-101): animate it like any other joint.
                deg = float(os.getenv("GRIPPER_OPEN_DEG", "60")) if step["action"] == "open" else 0.0
                self.run_step({"type": "joints", "targets_deg": {"gripper": deg}, "duration": 0.6}, target, on_pose)
            else:
                self._publish_gripper(target, step["action"])
                time.sleep(0.4)
        elif kind == "wait":
            time.sleep(float(step.get("seconds", 0.5)))
        elif kind == "runner":
            self._publish_runner(target, step["to"])
        elif kind == "base":
            fwd, turn = float(step.get("forward_m", 0)), float(step.get("turn_deg", 0))
            pose = self.base_pose.setdefault(target, {"x": 0.0, "y": 0.0, "yaw": 0.0})
            pose["yaw"] += turn
            pose["x"] += math.cos(math.radians(pose["yaw"])) * fwd
            pose["y"] += math.sin(math.radians(pose["yaw"])) * fwd
            self._publish_base(target, fwd, turn)
            if on_pose:
                on_pose(target, self.current(target))

    def sync_sim_to_live(self) -> None:
        """Start each rehearsal from where the real robot is believed to be."""
        if "live" in self.state:
            self.state["sim"] = dict(self.state["live"])
            frame = self.state["sim"]
            try:
                self._publish_joints("sim", frame)
            except Exception:  # noqa: BLE001
                log.exception("sim sync failed")


class OfflineBackend(Backend):
    """No network. Lets the UI and the agent loop run when Cyberwave is unreachable."""

    def __init__(self) -> None:
        super().__init__(
            names=["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"],
            limits={
                "shoulder_pan": (-110, 110), "shoulder_lift": (-100, 100), "elbow_flex": (-97, 97),
                "wrist_flex": (-95, 95), "wrist_roll": (-157, 163), "gripper": (-10, 100),
            },
            has_gripper=True,
            asset="SO-101 arm (offline mock)",
        )

    def _publish_joints(self, target, deg):  # noqa: D401
        pass

    def _publish_gripper(self, target, action):
        pass

    def _publish_base(self, target, forward_m, turn_deg):
        pass

    def frame_jpeg(self):
        return phone_eyes_jpeg() or webcam_jpeg()


class CyberwaveBackend(Backend):
    def __init__(self) -> None:
        super().__init__()
        from cyberwave import Cyberwave

        kwargs: dict[str, Any] = {}
        if os.getenv("CYBERWAVE_WORKSPACE_ID"):
            kwargs["workspace_id"] = os.getenv("CYBERWAVE_WORKSPACE_ID")
        if os.getenv("CYBERWAVE_ENVIRONMENT_ID"):
            kwargs["environment_id"] = os.getenv("CYBERWAVE_ENVIRONMENT_ID")
        self.cw = Cyberwave(**kwargs)
        self._lock = threading.Lock()

        sim_id = os.getenv("SIM_TWIN_ID") or None
        live_id = os.getenv("LIVE_TWIN_ID") or sim_id
        if sim_id:
            self.sim = self.cw.twin(twin_id=sim_id)
        else:
            asset = os.getenv("SIM_ASSET", "the-robot-studio/so101")
            log.warning("SIM_TWIN_ID empty: creating a twin from %s", asset)
            self.sim = self.cw.twin(asset, environment_id=os.getenv("CYBERWAVE_ENVIRONMENT_ID") or None)
            log.warning("Created sim twin %s - put it in SIM_TWIN_ID", self.sim.uuid)
        self.live = self.sim if (not live_id or live_id == sim_id) else self.cw.twin(twin_id=live_id)
        runner_id = os.getenv("RUNNER_TWIN_ID")
        self.runner = None
        if runner_id:
            try:
                self.runner = self.cw.twin(twin_id=runner_id)
            except Exception as e:  # noqa: BLE001
                log.warning("runner twin unavailable: %s", e)
        self.has_runner = self.runner is not None
        cam_id = os.getenv("CAMERA_TWIN_ID")
        self.camera = self.cw.twin(twin_id=cam_id) if cam_id else None

        twin = self.sim
        try:
            self.asset = f"{twin.name} ({twin.slug})"
        except Exception:  # noqa: BLE001
            self.asset = "robot"
        self.names = list(twin.joints.list()) if hasattr(twin, "joints") else []
        if self.names == [f"_{i}" for i in range(1, 7)]:
            self.aliases = dict(zip(SO101_ALIASES, self.names))
        self.has_gripper = hasattr(twin, "grip")
        self.has_base = hasattr(twin, "move_forward")
        self.limits = self._read_limits(twin)
        for target in ("sim", "live"):
            self.state[target] = {n: 0.0 for n in self.names}
        self._seed_from_twin()
        log.info("twin ready: %s joints=%s gripper=%s base=%s", self.asset, self.names, self.has_gripper, self.has_base)

    def _seed_from_twin(self) -> None:
        for target, twin in (("live", self.live), ("sim", self.sim)):
            try:
                view = twin.get_joints(timeout=2.0)
                vals = {k: math.degrees(float(v)) for k, v in dict(view).items() if k in self.names}
                if vals:
                    self.state[target].update(vals)
            except Exception as e:  # noqa: BLE001
                log.info("no initial joint state for %s: %s", target, e)

    def _read_limits(self, twin) -> dict[str, tuple[float, float]]:
        """Best effort: dig joint limits (radians) out of the twin's universal schema."""
        limits: dict[str, tuple[float, float]] = {}
        try:
            schema = twin.get_schema("")
        except Exception as e:  # noqa: BLE001
            log.info("schema unavailable (%s); using default limits", e)
            schema = None

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                name = node.get("name")
                lim = node.get("limit") or node.get("limits") or node
                lo = lim.get("lower") if isinstance(lim, dict) else None
                hi = lim.get("upper") if isinstance(lim, dict) else None
                if name in self.names and lo is not None and hi is not None:
                    try:
                        lo_d, hi_d = math.degrees(float(lo)), math.degrees(float(hi))
                        if hi_d > lo_d:
                            limits[name] = (lo_d, hi_d)
                    except (TypeError, ValueError):
                        pass
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(schema)
        for n in self.names:
            limits.setdefault(n, (-DEFAULT_LIMIT_DEG, DEFAULT_LIMIT_DEG))
        return limits

    @staticmethod
    def _source(target: str) -> str:
        return "sim_tele" if target == "sim" else "tele"

    def _twin(self, target: str):
        return self.sim if target == "sim" else self.live

    def _publish_joints(self, target, deg):
        rad = {k: math.radians(v) for k, v in deg.items()}
        with self._lock:
            self._twin(target).set_joints(rad, source_type=self._source(target))

    def _publish_gripper(self, target, action):
        twin = self._twin(target)
        with self._lock:
            handle = getattr(twin, "gripper", None)
            if handle is not None and hasattr(handle, "grip"):
                (handle.grip if action == "close" else handle.release)(source_type=self._source(target))
            else:
                (twin.grip if action == "close" else twin.release)()

    def _publish_base(self, target, forward_m, turn_deg):
        if target == "sim":
            return  # Cyberwave locomotion is live-only; sim uses the kinematic model above
        twin = self.live
        with self._lock:
            if abs(turn_deg) > 1:
                fn = twin.turn_left if turn_deg > 0 else twin.turn_right
                fn(math.radians(abs(turn_deg)), duration=1.0)
            if abs(forward_m) > 0.01:
                fn = twin.move_forward if forward_m > 0 else twin.move_backward
                fn(abs(forward_m), duration=max(1.0, abs(forward_m) / 0.3))

    def _publish_runner(self, target: str, to: str) -> None:
        """Drive the ward runner along its route in the Cyberwave scene, drop off, come back."""
        if self.runner is None:
            return
        path = [RUNNER_HOME] + RUNNER_ROUTES[to]
        legs = list(zip(path, path[1:])) + list(zip(path[::-1], path[::-1][1:]))
        for i, ((x0, y0), (x1, y1)) in enumerate(legs):
            if i == len(legs) // 2:
                time.sleep(0.8)  # hand-off at the ward
            try:
                self.runner.edit_rotation(yaw=180 if x1 < x0 else 0)
            except Exception:  # noqa: BLE001
                pass
            for k in range(1, 4):
                self._guard()
                t = k / 3
                try:
                    self.runner.edit_position(x=x0 + (x1 - x0) * t, y=y0 + (y1 - y0) * t, z=0)
                except Exception as e:  # noqa: BLE001
                    log.info("runner move failed: %s", e)
                time.sleep(0.12)

    def frame_jpeg(self) -> Optional[bytes]:
        eyes = phone_eyes_jpeg()
        if eyes:
            return eyes
        result: list[Optional[bytes]] = [None]

        def grab():
            for twin in [t for t in (self.camera, self.live, self.sim) if t is not None]:
                try:
                    if hasattr(twin, "get_frame"):
                        data = twin.get_frame("bytes")
                        if data:
                            result[0] = data
                            return
                except Exception as e:  # noqa: BLE001
                    log.info("frame from %s failed: %s", getattr(twin, "uuid", "?"), e)

        th = threading.Thread(target=grab, daemon=True)
        th.start()
        th.join(timeout=8)
        if result[0] is None:
            result[0] = webcam_jpeg()
        return result[0]


def phone_eyes_jpeg(max_age_s: float = 30.0) -> Optional[bytes]:
    """Latest frame a phone running eyes.html pushed to Supabase Storage."""
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return None
    try:
        import email.utils

        import httpx

        r = httpx.get(f"{url.rstrip('/')}/storage/v1/object/frames/eyes/latest.jpg",
                      headers={"Authorization": f"Bearer {key}", "apikey": key}, timeout=6)
        if r.status_code != 200:
            return None
        lm = r.headers.get("last-modified")
        if lm:
            age = time.time() - email.utils.parsedate_to_datetime(lm).timestamp()
            if age > max_age_s:
                log.info("phone eyes frame is stale (%.0fs)", age)
                return None
        return r.content
    except Exception as e:  # noqa: BLE001
        log.info("phone eyes failed: %s", e)
        return None


def webcam_jpeg() -> Optional[bytes]:
    """Fallback eyes: the laptop webcam pointed at the table (needs opencv)."""
    if os.getenv("WEBCAM_INDEX") is None:
        return None
    try:
        import cv2  # type: ignore

        cap = cv2.VideoCapture(int(os.getenv("WEBCAM_INDEX", "0")))
        for _ in range(5):
            ok, img = cap.read()
        cap.release()
        if not ok:
            return None
        img = cv2.resize(img, (640, int(640 * img.shape[0] / img.shape[1])))
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return buf.tobytes() if ok else None
    except Exception as e:  # noqa: BLE001
        log.info("webcam failed: %s", e)
        return None


def make_backend() -> Backend:
    if os.getenv("OFFLINE_ROBOT", "0") == "1":
        log.warning("OFFLINE_ROBOT=1: using mock robot")
        return OfflineBackend()
    return CyberwaveBackend()
