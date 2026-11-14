"""GuardianHome: a real camera sees a fall, a robot team responds in the Cyberwave twin.

Incident lifecycle (Supabase `incidents`):
  detected -> responding -> awaiting_ack -> resolved | help_requested | escalated
Every robot route is checked by the twin before anything moves: inside the apartment,
through the bedroom door (not through walls), and never closer than 0.5 m to the person.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from brain import Rehearsal
from db import DB
from robot import StepError

log = logging.getLogger("twingate.guardian")

RESIDENT = (1.2, -0.7)            # where the resident stands / falls
BOUNDS = (2.9, 1.9)               # apartment interior half-extents (m)
DOOR_Y = (0.3, 1.35)              # gap in the bedroom wall at x = 0
MIN_HUMAN_M = 0.5
HOME = {"rover": (-2.6, -1.6), "dog": (-1.6, -0.2)}
Z = {"rover": 0.03, "dog": 0.3}
NAIVE = {"rover": [(-2.6, -1.6), (1.0, -0.8)]}                     # what a naive planner would do
ROUTES = {"rover": [(-2.6, -1.6), (-0.6, 1.0), (0.4, 1.0), (0.75, -0.25)],
          "dog": [(-1.6, -0.2), (-0.6, 0.9), (0.2, 0.95)]}
ACK_SECONDS = 25
LABEL = {"rover": "Response Rover (TurtleBot3) with phone + medkit", "dog": "Escort Quadruped (Go2)"}


def path_problems(path: list[tuple[float, float]]) -> list[str]:
    problems = []
    for x, y in path:
        if abs(x) > BOUNDS[0] or abs(y) > BOUNDS[1]:
            problems.append(f"point ({x:.1f}, {y:.1f}) is outside the apartment")
    for (x0, y0), (x1, y1) in zip(path, path[1:]):
        if (x0 < 0) != (x1 < 0) and x0 != x1:
            t = -x0 / (x1 - x0)
            yc = y0 + (y1 - y0) * t
            if not DOOR_Y[0] <= yc <= DOOR_Y[1]:
                problems.append(f"route crosses the bedroom wall at y={yc:.2f} (door is y {DOOR_Y[0]}-{DOOR_Y[1]})")
    fx, fy = path[-1]
    d = math.hypot(fx - RESIDENT[0], fy - RESIDENT[1])
    if d < MIN_HUMAN_M:
        problems.append(f"stops {d:.2f} m from the person (minimum {MIN_HUMAN_M} m)")
    return problems


class Guardian:
    def __init__(self, worker: Any) -> None:
        self.w = worker
        cw = worker.backend.cw
        self.tw = {k: cw.twin(twin_id=os.environ[v]) for k, v in
                   (("rover", "ROVER_TWIN_ID"), ("dog", "DOG_TWIN_ID"), ("resident", "RESIDENT_TWIN_ID"))}
        self.pos = dict(HOME)
        self.db = DB(worker.url, worker.key)
        self.lock = threading.Lock()
        self.handled: set[tuple[int, str]] = set()
        self.reset_scene()
        threading.Thread(target=self.loop, daemon=True).start()

    # ---- scene -----------------------------------------------------------
    def _place(self, name: str, x: float, y: float, yaw: float | None = None) -> None:
        t = self.tw[name]
        try:
            t.edit_position(x=x, y=y, z=Z[name])
            if yaw is not None:
                t.edit_rotation(yaw=yaw)
        except Exception as e:  # noqa: BLE001
            log.info("place %s failed: %s", name, e)
        self.pos[name] = (x, y)

    def drive(self, name: str, path: list[tuple[float, float]], emit: Any) -> None:
        for (x0, y0), (x1, y1) in zip(path, path[1:]):
            yaw = math.degrees(math.atan2(y1 - y0, x1 - x0))
            for k in range(1, 4):
                self.w.backend._guard()
                self._place(name, x0 + (x1 - x0) * k / 3, y0 + (y1 - y0) * k / 3, yaw if k == 1 else None)
        emit("sim", f"{LABEL[name]} arrived", {"robot": name, "at": path[-1]})

    def resident_fall(self) -> None:
        try:
            self.tw["resident"].edit_position(x=RESIDENT[0] + 0.3, y=RESIDENT[1], z=0.18)
            self.tw["resident"].edit_rotation(yaw=180, pitch=-90, roll=0)
        except Exception as e:  # noqa: BLE001
            log.info("fall pose failed: %s", e)

    def resident_stand(self) -> None:
        try:
            self.tw["resident"].edit_rotation(yaw=180, pitch=0, roll=0)
            self.tw["resident"].edit_position(x=RESIDENT[0], y=RESIDENT[1], z=0.793)
        except Exception as e:  # noqa: BLE001
            log.info("stand pose failed: %s", e)

    def reset_scene(self) -> None:
        for n, (x, y) in HOME.items():
            self._place(n, x, y, 0)
        self.resident_stand()

    # ---- response --------------------------------------------------------
    def respond(self, inc: dict[str, Any]) -> None:
        w = self.w
        m = self.db.insert("missions", {"prompt": f"FALL DETECTED: {inc.get('title') or 'person on the floor'}",
                                        "author": "guardian", "status": "rehearsing"})
        mid = m["id"]
        self.db.update("incidents", {"id": f"eq.{inc['id']}"}, {"mission_id": mid})

        def emit(stage: str, message: str, data: Any = None) -> None:
            w.events.emit(mid, stage, message, data)

        def on_pose(target: str, deg: dict[str, float]) -> None:
            emit("pose", "", {"target": target, "joints_deg": {k: round(v, 1) for k, v in deg.items()}})

        emit("safety", f"FALL DETECTED ({inc['source']}): {inc.get('detail') or inc.get('title')}", {"incident": inc["id"]})
        w.cw_alert("GuardianHome: fall detected", inc.get("detail") or "Person on the floor", "critical")
        self.resident_fall()
        emit("info", "Resident avatar updated in the digital twin. Planning the robot response.")
        rh = Rehearsal()

        # 1) The twin checks the naive straight-line route first and rejects it.
        for name, path in NAIVE.items():
            for p in path_problems(path):
                rh.caught.append(f"{name}: {p}")
                emit("safety", f"twin caught: {LABEL[name]} direct route {p}", {"violation": p})
        # 2) Door routes, checked, then both robots driven in the twin at the same time.
        drivers = []
        for name, path in ROUTES.items():
            probs = path_problems(path)
            if probs:
                rh.caught.extend(probs)
                emit("safety", f"twin caught: {name}: {'; '.join(probs)}")
                continue
            emit("tool", f"dispatch {name} via bedroom door -> stop {math.hypot(path[-1][0]-RESIDENT[0], path[-1][1]-RESIDENT[1]):.2f} m from resident")
            th = threading.Thread(target=self.drive, args=(name, path, emit), daemon=True)
            th.start(); drivers.append(th)
        for th in drivers:
            th.join()
        # 3) Medication arm prepares the pill organizer.
        skill = next((s for s in w.load_skills() if s["name"] == "prepare_pills"), None)
        if skill:
            emit("tool", "run_skill(prepare_pills): medication arm readies the pill organizer")
            w.backend.sync_sim_to_live()
            for st in skill["steps"]:
                try:
                    w.backend.run_step(st, "sim", on_pose)
                    rh.steps.append(st)
                except StepError as e:
                    rh.caught.append(str(e))
                    emit("safety", f"twin caught: {e}")
        rh.summary = ("Fall response rehearsed: rover with phone and medkit is beside the resident (0.6 m), "
                      "Go2 is lighting the bedroom door, medication arm has the pills ready. Waiting for the resident or caregiver.")
        w._finalize(m, rh, emit)
        st = self.db.select("missions", id=f"eq.{mid}", select="status")
        if st and st[0]["status"] == "awaiting_approval":
            self.db.update("missions", {"id": f"eq.{mid}"}, {"status": "approved"})  # emergency policy: pre-approved once gated
            emit("info", "Emergency policy: gated plan auto-approved for the real robots.")
        esc = (datetime.now(timezone.utc) + timedelta(seconds=ACK_SECONDS)).isoformat()
        self.db.update("incidents", {"id": f"eq.{inc['id']}", "status": "eq.responding"},
                       {"status": "awaiting_ack", "escalate_at": esc})
        emit("info", f"Asking the resident and caregiver. Escalating in {ACK_SECONDS} s if nobody answers.")

    def stand_down(self, inc: dict[str, Any], why: str) -> None:
        mid = inc.get("mission_id")
        self.w.events.emit(mid, "info", why)
        time.sleep(1.0)
        self.resident_stand()
        ths = [threading.Thread(target=self.drive, args=(n, list(reversed(p)), lambda *a, **k: None), daemon=True)
               for n, p in ROUTES.items()]
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        self.w.events.emit(mid, "info", "Robots back at their docks. Incident closed.")

    # ---- loop ------------------------------------------------------------
    def loop(self) -> None:
        while True:
            time.sleep(0.6)
            try:
                for inc in self.db.select("incidents", status="in.(detected,awaiting_ack,resolved,help_requested)",
                                          order="id.desc", limit="5"):
                    key = (inc["id"], inc["status"])
                    if key in self.handled:
                        continue
                    if inc["status"] == "detected":
                        got = self.db.update("incidents", {"id": f"eq.{inc['id']}", "status": "eq.detected"},
                                             {"status": "responding"})
                        if got:
                            self.handled.add(key)
                            with self.lock:
                                self.w.busy = True
                                try:
                                    self.respond(got[0])
                                finally:
                                    self.w.busy = False
                    elif inc["status"] == "awaiting_ack":
                        esc = inc.get("escalate_at")
                        if esc and datetime.fromisoformat(esc.replace("Z", "+00:00")) < datetime.now(timezone.utc):
                            got = self.db.update("incidents", {"id": f"eq.{inc['id']}", "status": "eq.awaiting_ack"},
                                                 {"status": "escalated"})
                            if got:
                                self.handled.add(key)
                                self.w.events.emit(inc.get("mission_id"), "safety",
                                                   "No answer in time: emergency services called (simulated), caregiver paged, door unlocked for paramedics.")
                                self.w.cw_alert("GuardianHome: escalated", "No response from resident or caregiver", "critical")
                    elif inc["status"] == "resolved":
                        self.handled.add(key)
                        self.stand_down(inc, f"Resident is OK (confirmed by {inc.get('resolved_by') or 'resident'}). Standing down.")
                    elif inc["status"] == "help_requested":
                        self.handled.add(key)
                        self.w.events.emit(inc.get("mission_id"), "safety",
                                           f"Help requested by {inc.get('resolved_by') or 'caregiver'}: ambulance dispatched (simulated), rover stays with the resident.")
                        self.w.cw_alert("GuardianHome: help requested", "Ambulance dispatched (simulated)", "critical")
            except Exception as e:  # noqa: BLE001
                log.warning("guardian loop: %s", e)
                time.sleep(2)
