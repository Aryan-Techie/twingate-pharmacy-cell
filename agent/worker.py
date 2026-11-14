"""TwinGate worker: Supabase queue -> Claude rehearsal on the twin -> safety gate -> real robot.

Run:  .venv/bin/python worker.py
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import threading  # noqa: E402

import anthropic  # noqa: E402

from brain import Brain, Rehearsal, inspect_frame  # noqa: E402
from db import DB, EventSink  # noqa: E402
from robot import MAX_DEG_PER_S, Backend, StepError, make_backend, phone_eyes_jpeg  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
for noisy in ("httpx", "httpcore", "cyberwave", "paho"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("twingate")

AUTO_PROMOTE = os.getenv("AUTO_PROMOTE", "0") == "1"
POLL_S = 0.7


def need(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        sys.exit(f"Missing {name} in .env")
    return v


class Worker:
    def __init__(self) -> None:
        self.url = need("SUPABASE_URL")
        self.key = need("SUPABASE_SERVICE_ROLE_KEY")
        need("ANTHROPIC_API_KEY")
        self.db = DB(self.url, self.key)
        self.events = EventSink(self.db)
        self.backend: Backend = make_backend()
        self.brain = Brain(self.backend, self.load_skills, self.save_skill, self.save_frame, self.match_skill,
                           self.author_history)
        self.quorum = 3
        self.busy = False
        self._ensure_bucket()
        threading.Thread(target=self.teach_loop, daemon=True).start()
        threading.Thread(target=self.watch_loop, daemon=True).start()
        threading.Thread(target=self.control_loop, daemon=True).start()
        self.guardian = None
        if os.getenv("GUARDIAN") == "1" and getattr(self.backend, "cw", None) is not None:
            from guardian import Guardian
            self.guardian = Guardian(self)
        self.events.emit(None, "info", f"worker online: {self.backend.asset}",
                         {"joints": self.backend.names, "limits": self.backend.limits,
                          "auto_promote": AUTO_PROMOTE})

    # ---- Supabase helpers ------------------------------------------------
    def load_skills(self) -> list[dict[str, Any]]:
        return self.db.select("skills", select="name,description,steps", order="uses.desc,id.asc", limit="40")

    def save_skill(self, name: str, description: str, steps: list[dict[str, Any]], author: str) -> None:
        self.db.upsert("skills", {"name": name, "description": description, "steps": steps, "author": author},
                       on_conflict="name")

    def match_skill(self, prompt: str) -> Optional[tuple[str, float]]:
        try:
            r = self.db.http.post(f"{self.db.base}/rpc/match_skill", json={"q": prompt})
            r.raise_for_status()
            rows = r.json()
            return (rows[0]["name"], float(rows[0]["score"])) if rows else None
        except Exception as e:  # noqa: BLE001
            log.info("match_skill failed: %s", e)
            return None

    def author_history(self, author: str) -> str:
        if not author or author in ("anon", "sentry"):
            return ""
        try:
            rows = self.db.select("missions", author=f"eq.{author}", select="prompt,status,skill_used",
                                  status="not.in.(queued,planning,rehearsing)", order="id.desc", limit="3")
            return "\n".join(f"- \"{r['prompt']}\" -> {r['status']}" + (f" (skills: {r['skill_used']})" if r.get("skill_used") else "")
                             for r in rows)
        except Exception:  # noqa: BLE001
            return ""

    def cw_alert(self, name: str, description: str, severity: str = "warning") -> None:
        """Mirror important events as alerts on the Cyberwave twin (visible in the Cyberwave dashboard)."""
        cw = getattr(self.backend, "cw", None)
        twin = getattr(self.backend, "sim", None)
        if cw is None or twin is None:
            return
        def send() -> None:
            try:
                cw.publish_alert(twin.uuid, name=name[:120], description=description[:500], severity=severity)
            except Exception as e:  # noqa: BLE001
                log.info("cyberwave alert failed: %s", e)
        threading.Thread(target=send, daemon=True).start()

    def control_loop(self) -> None:
        """E-STOP + crowd quorum, polled fast so a stop lands within ~0.3 s."""
        db = DB(self.url, self.key)
        was = False
        while True:
            try:
                row = db.select("control", id="eq.1")[0]
                self.quorum = int(row.get("crowd_quorum") or 3)
                if row["estop"]:
                    self.backend.stop_event.set()
                    if not was:
                        self.events.emit(None, "safety", "E-STOP engaged from the operator console", None)
                        self.cw_alert("TwinGate E-STOP", "Operator engaged the emergency stop", "critical")
                else:
                    self.backend.stop_event.clear()
                was = bool(row["estop"])
            except Exception as e:  # noqa: BLE001
                log.debug("control poll: %s", e)
            time.sleep(0.25)

    def teach_loop(self) -> None:
        """Teach mode: stream the phone's slider pose to the simulated twin while the robot is idle."""
        db = DB(self.url, self.key)
        last_seen = None
        while True:
            time.sleep(0.12)
            try:
                row = db.select("teach", id="eq.1")[0]
                if not row.get("owner") or row["updated_at"] == last_seen or self.busy:
                    continue
                last_seen = row["updated_at"]
                b = self.backend
                pose = dict(b.current("sim"))
                for k, v in (row.get("joints_deg") or {}).items():
                    try:
                        name = b.resolve(k)
                    except StepError:
                        continue
                    lo, hi = b.limits.get(name, (-110.0, 110.0))
                    pose[name] = max(lo, min(hi, float(v)))
                b._publish_joints("sim", pose)
                b.state["sim"] = pose
            except Exception as e:  # noqa: BLE001
                log.debug("teach loop: %s", e)
                time.sleep(1)

    def teach_mission(self, sub: dict[str, Any]) -> None:
        """A skill taught by hand: rehearse the keyframes on the twin, auto-slow if too fast, then gate."""
        name, author = sub["name"], sub["author"] or "anon"
        m = self.db.insert("missions", {"prompt": f"Taught by hand: {name}", "author": author, "status": "rehearsing"})
        mid = m["id"]
        self.db.update("teach_submissions", {"id": f"eq.{sub['id']}"}, {"status": "rehearsing", "mission_id": mid})

        def emit(stage: str, message: str, data: Any) -> None:
            self.events.emit(mid, stage, message, data)

        def on_pose(target: str, deg: dict[str, float]) -> None:
            emit("pose", "", {"target": target, "joints_deg": {k: round(v, 1) for k, v in deg.items()}})

        emit("info", f"{author} taught '{name}' by hand: {len(sub['keyframes'])} keyframes. Rehearsing on the twin.", None)
        self.backend.sync_sim_to_live()
        rh = Rehearsal()
        for i, kf in enumerate(sub["keyframes"]):
            step = {"type": "joints", "targets_deg": dict(kf.get("targets_deg") or {}),
                    "duration": max(0.3, min(4.0, float(kf.get("duration", 1.0))))}
            for attempt in range(4):
                problems = self.backend.check_step(step, "sim")
                if not problems:
                    break
                if all("deg/s" in p for p in problems) and attempt < 3:
                    msg = f"keyframe {i + 1}: {problems[0]}; twin slowed it to {step['duration'] * 1.6:.1f}s"
                    rh.caught.append(msg)
                    emit("safety", f"twin caught: {msg}", {"violation": msg})
                    step["duration"] = round(step["duration"] * 1.6, 2)
                else:
                    break
            try:
                self.backend.run_step(step, "sim", on_pose)
                rh.steps.append(step)
                emit("sim", f"keyframe {i + 1} rehearsed", {"step": step})
            except StepError as e:
                rh.caught.append(f"keyframe {i + 1}: {e}")
                emit("safety", f"twin caught: keyframe {i + 1}: {e} (dropped)", {"violation": str(e)})
        rh.summary = f"{author} taught the robot '{name}' by hand ({len(rh.steps)} keyframes)."
        if rh.steps:
            self.save_skill(name, f"Taught by hand by {author}", list(rh.steps), author)
            rh.saved_skill = name
            emit("info", f"new skill learned: {name}", {"skill": name})
        self._finalize(m, rh, emit)
        self.db.update("teach_submissions", {"id": f"eq.{sub['id']}"}, {"status": "done"})

    def tally_votes(self) -> None:
        pending = self.db.select("missions", status="eq.awaiting_approval", select="id")
        if not pending:
            return
        ids = ",".join(str(m["id"]) for m in pending)
        votes = self.db.select("votes", mission_id=f"in.({ids})", select="mission_id,approve")
        for m in pending:
            mv = [v["approve"] for v in votes if v["mission_id"] == m["id"]]
            yes, no = sum(mv), len(mv) - sum(mv)
            decision = None
            if yes >= self.quorum and yes > no:
                decision = "approved"
            elif no >= self.quorum and no >= yes:
                decision = "rejected"
            if decision:
                got = self.db.update("missions", {"id": f"eq.{m['id']}", "status": "eq.awaiting_approval"},
                                     {"status": decision})
                if got:
                    self.events.emit(m["id"], "info", f"crowd vote: {yes} yes / {no} no -> {decision}", None)

    def _ensure_bucket(self) -> None:
        try:
            httpx.post(f"{self.url}/storage/v1/bucket", json={"id": "frames", "name": "frames", "public": True},
                       headers={"Authorization": f"Bearer {self.key}", "apikey": self.key}, timeout=10)
        except Exception as e:  # noqa: BLE001
            log.info("bucket create skipped: %s", e)

    def save_frame(self, jpeg: bytes) -> Optional[str]:
        path = f"{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}.jpg"
        try:
            r = httpx.post(f"{self.url}/storage/v1/object/frames/{path}", content=jpeg,
                           headers={"Authorization": f"Bearer {self.key}", "apikey": self.key,
                                    "Content-Type": "image/jpeg"}, timeout=15)
            r.raise_for_status()
            return f"{self.url}/storage/v1/object/public/frames/{path}"
        except Exception as e:  # noqa: BLE001
            log.warning("frame upload failed: %s", e)
            return None

    def set_status(self, mid: int, status: str, **extra: Any) -> None:
        self.db.update("missions", {"id": f"eq.{mid}"}, {"status": status, **extra})

    def claim(self, from_status: str, to_status: str) -> Optional[dict[str, Any]]:
        rows = self.db.select("missions", status=f"eq.{from_status}", order="id.asc", limit="1")
        if not rows:
            return None
        got = self.db.update("missions", {"id": f"eq.{rows[0]['id']}", "status": f"eq.{from_status}"},
                             {"status": to_status})
        return got[0] if got else None

    # ---- sentry mode -----------------------------------------------------
    def watch_loop(self) -> None:
        """Monitoring / inspection: judge robot-eyes frames against the operator's rule."""
        db = DB(self.url, self.key)
        client = anthropic.Anthropic()
        last_t, last_nofeed, previous = 0.0, 0.0, ""
        last_alert: tuple[str, float] = ("", 0.0)
        while True:
            time.sleep(1.0)
            try:
                rows = db.select("watch", id="eq.1")
                if not rows or not rows[0]["enabled"]:
                    continue
                w = rows[0]
                if time.time() - last_t < w["interval_s"]:
                    continue
                last_t = time.time()
                jpeg = phone_eyes_jpeg(max_age_s=20)
                if not jpeg:
                    if time.time() - last_nofeed > 60:
                        last_nofeed = time.time()
                        db.insert("reports", {"alert": False, "title": "No camera feed",
                                              "detail": "Open eyes.html on a phone or laptop pointed at the robot.",
                                              "rule": w["rule"]})
                    continue
                url = self.save_frame(jpeg)
                r = inspect_frame(client, jpeg, w["rule"], previous)
                previous = f"{r['title']}: {r['detail']}"
                mission_id = None
                if r["alert"] and self.guardian is None and (r.get("robot_reaction") or "").strip():
                    key = (r["title"] or "").lower()
                    pending = db.select("missions", author="eq.sentry", select="id",
                                        status="in.(queued,planning,rehearsing,awaiting_approval,approved,executing)")
                    fresh = not (key == last_alert[0] and time.time() - last_alert[1] < 45)
                    if fresh and not pending:
                        m = db.insert("missions", {"prompt": r["robot_reaction"].strip()[:300], "author": "sentry"})
                        mission_id = m["id"] if m else None
                    last_alert = (key, time.time())
                db.insert("reports", {"image_url": url, "alert": bool(r["alert"]), "title": r["title"],
                                      "detail": r["detail"], "rule": w["rule"], "mission_id": mission_id})
                if r["alert"] and self.guardian is not None:
                    open_inc = db.select("incidents", select="id",
                                         status="in.(detected,responding,awaiting_ack)")
                    if not open_inc:
                        db.insert("incidents", {"source": "camera", "title": r["title"], "detail": r["detail"],
                                                "image_url": url})
                    r["robot_reaction"] = ""  # guardian runs the response, not a free-form mission
                if r["alert"]:
                    self.cw_alert(f"Sentry: {r['title']}", f"{r['detail']} (rule: {w['rule']})")
                log.info("sentry: %s %s", "ALERT" if r["alert"] else "ok", r["title"])
            except Exception as e:  # noqa: BLE001
                log.warning("sentry loop: %s", e)
                time.sleep(3)

    # ---- safety gate -----------------------------------------------------
    def safety_review(self, rh: Rehearsal) -> dict[str, Any]:
        """Replay the plan against a scratch copy of the live state, step by step."""
        b = self.backend
        saved_state = {k: dict(v) for k, v in b.state.items()}
        saved_base = {k: dict(v) for k, v in b.base_pose.items()}
        b.state["check"] = dict(b.state.get("live", {}))
        b.base_pose["check"] = dict(b.base_pose.get("live", {"x": 0.0, "y": 0.0, "yaw": 0.0}))
        violations: list[str] = []
        total_s = 0.0
        for i, st in enumerate(rh.steps):
            for p in b.check_step(st, "check"):
                violations.append(f"step {i + 1}: {p}")
            # advance the scratch state without publishing anything
            if st.get("type") == "joints":
                for k, v in (st.get("targets_deg") or {}).items():
                    try:
                        b.state["check"][b.resolve(k)] = float(v)
                    except StepError:
                        pass
                total_s += float(st.get("duration", 1.0))
            elif st.get("type") == "wait":
                total_s += float(st.get("seconds", 0))
            elif st.get("type") == "base":
                total_s += 1.5
            else:
                total_s += 0.4
        b.state, b.base_pose = saved_state, saved_base
        checks = [
            {"name": "Plan rehearsed in digital twin", "ok": bool(rh.steps), "detail": f"{len(rh.steps)} steps"},
            {"name": "Joint limits", "ok": not any("limits" in v for v in violations),
             "detail": "all targets inside URDF limits"},
            {"name": f"Speed <= {MAX_DEG_PER_S:.0f} deg/s", "ok": not any("deg/s" in v for v in violations),
             "detail": "from the real robot's current pose"},
            {"name": "Geofence", "ok": not any("geofence" in v for v in violations), "detail": "base stays in bounds"},
            {"name": "Duration <= 25 s", "ok": total_s <= 25, "detail": f"{total_s:.1f} s"},
            {"name": "Intent check", "ok": not rh.refused, "detail": "planner accepted the request"},
        ]
        passed = all(c["ok"] for c in checks) and not violations
        score = round(100 * sum(c["ok"] for c in checks) / len(checks))
        return {"passed": passed, "score": score, "checks": checks, "violations": violations,
                "caught_in_rehearsal": rh.caught, "duration_s": round(total_s, 1)}

    # ---- mission phases --------------------------------------------------
    def plan_mission(self, m: dict[str, Any]) -> None:
        mid = m["id"]

        def emit(stage: str, message: str, data: Any) -> None:
            self.events.emit(mid, stage, message, data)

        emit("info", f"mission #{mid} from {m['author']}: \"{m['prompt']}\"", None)
        self.backend.sync_sim_to_live()
        self.set_status(mid, "rehearsing")
        try:
            rh = self.brain.rehearse(m["prompt"], m["author"], emit)
        except Exception as e:  # noqa: BLE001
            log.exception("rehearsal failed")
            self.set_status(mid, "failed", error=str(e)[:500])
            emit("error", f"planner error: {e}", None)
            return

        self._finalize(m, rh, emit)

    def _finalize(self, m: dict[str, Any], rh: Rehearsal, emit: Any) -> None:
        mid = m["id"]
        safety = self.safety_review(rh)
        emit("safety", f"safety gate {'PASSED' if safety['passed'] else 'BLOCKED'} ({safety['score']}/100)", safety)
        patch = {"plan": rh.steps, "safety": safety, "summary": rh.summary,
                 "skill_used": ", ".join(rh.skills_used + ([f"+{rh.saved_skill}"] if rh.saved_skill else [])) or None}
        if rh.refused:
            self.set_status(mid, "rejected", **patch)
        elif not safety["passed"]:
            self.set_status(mid, "blocked", **patch)
            self.cw_alert("TwinGate blocked a plan", f"Mission #{mid} \"{m['prompt']}\": {(safety['violations'] or ['failed checks'])[0]}")
        elif AUTO_PROMOTE:
            self.set_status(mid, "approved", **patch)
        else:
            self.set_status(mid, "awaiting_approval", **patch)
            emit("info", "waiting for operator: Promote to real robot", None)

    def execute_mission(self, m: dict[str, Any]) -> None:
        mid = m["id"]
        steps = m.get("plan") or []
        self.events.emit(mid, "live", f"executing on the REAL robot: {len(steps)} steps", None)

        def on_pose(target: str, deg: dict[str, float]) -> None:
            self.events.emit(mid, "pose", "", {"target": target, "joints_deg": {k: round(v, 1) for k, v in deg.items()}})

        try:
            for i, st in enumerate(steps):
                self.backend.run_step(st, "live", on_pose)
                self.events.emit(mid, "live", f"step {i + 1}/{len(steps)} done", {"step": st})
            self.set_status(mid, "done")
            self.events.emit(mid, "live", "real-world run complete", None)
            for name in (m.get("skill_used") or "").replace("+", "").split(", "):
                if name:
                    rows = self.db.select("skills", name=f"eq.{name}", select="uses")
                    if rows:
                        self.db.update("skills", {"name": f"eq.{name}"}, {"uses": rows[0]["uses"] + 1})
        except StepError as e:
            self.set_status(mid, "blocked", error=f"live check: {e}")
            if "E-STOP" in str(e):
                self.cw_alert("TwinGate halted a live run", f"Mission #{mid} stopped by E-STOP", "critical")
            self.events.emit(mid, "safety", f"live guard stopped the run: {e}", None)
        except Exception as e:  # noqa: BLE001
            log.exception("live run failed")
            self.set_status(mid, "failed", error=str(e)[:500])
            self.events.emit(mid, "error", f"live error: {e}", None)

    def loop(self) -> None:
        log.info("TwinGate worker running (auto_promote=%s). Ctrl+C to stop.", AUTO_PROMOTE)
        while True:
            try:
                if self.backend.stop_event.is_set():
                    time.sleep(POLL_S)
                    continue
                self.tally_votes()
                m = self.claim("approved", "executing")
                if m:
                    self.busy = True
                    try:
                        self.execute_mission(m)
                    finally:
                        self.busy = False
                    continue
                subs = self.db.select("teach_submissions", status="eq.new", order="id.asc", limit="1")
                if subs:
                    got = self.db.update("teach_submissions", {"id": f"eq.{subs[0]['id']}", "status": "eq.new"},
                                         {"status": "claimed"})
                    if got:
                        self.busy = True
                        try:
                            self.teach_mission(got[0])
                        finally:
                            self.busy = False
                        continue
                m = self.claim("queued", "planning")
                if m:
                    self.busy = True
                    try:
                        self.plan_mission(m)
                    finally:
                        self.busy = False
                    continue
            except httpx.HTTPError as e:
                log.warning("supabase hiccup: %s", e)
                time.sleep(2)
            time.sleep(POLL_S)


if __name__ == "__main__":
    Worker().loop()
