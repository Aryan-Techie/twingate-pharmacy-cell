"""Build the TwinGate Safe Pharmacy Fulfillment Cell in Cyberwave from catalog assets."""
import json
from dotenv import load_dotenv
load_dotenv("/Users/aryan/Workspace/Hackathons/winwin/mastersunion-cyberwave/.env")
from cyberwave import Cyberwave

cw = Cyberwave()
EID = "da865d0f-7e80-4628-830e-e6924ce4f9cf"
Z = 1.283  # Generic Table top (measured from Cyberwave's Milan lab template)

# name, asset, position (x, y, z), yaw/pitch/roll degrees, scale
SCENE = [
    ("Pick Table", "cyberwave/generictable", (0, 0, 0), (0, 0, 0), (0.5, 0.5, 1.0)),
    ("SO-101 Picker", "the-robot-studio/so101", (0, -0.22, Z), (90, 0, 0), None),
    ("Bin A - Paracetamol", "cyberwave/generic-box", (-0.17, 0.06, Z), (0, 0, 0), (0.12, 0.12, 0.08)),
    ("Bin B - Insulin", "cyberwave/generic-box", (0.0, 0.10, Z), (0, 0, 0), (0.12, 0.12, 0.08)),
    ("Bin C - Amoxicillin", "cyberwave/generic-box", (0.17, 0.06, Z), (0, 0, 0), (0.12, 0.12, 0.08)),
    ("Dispatch Tray", "cyberwave/generic-box", (0.24, -0.24, Z), (0, 0, 0), (0.14, 0.10, 0.04)),
    ("Pack - Paracetamol", "cyberwave/generic_cube", (-0.17, 0.06, Z + 0.01), (0, 0, 0), (0.04, 0.03, 0.02)),
    ("Pack - Insulin", "cyberwave/generic_cube", (0.0, 0.10, Z + 0.01), (15, 0, 0), (0.03, 0.03, 0.05)),
    ("Pack - Amoxicillin", "cyberwave/generic_cube", (0.17, 0.06, Z + 0.01), (-10, 0, 0), (0.04, 0.03, 0.02)),
    ("Overhead Verifier (RealSense D455)", "intel/realsensed455", (0.0, -0.02, Z + 0.70), (0, 90, 0), None),
    ("Front Camera (Logitech C275)", "logitech/c275", (0.65, -0.05, Z + 0.25), (180, 15, 0), None),
    ("Ward Runner (TurtleBot3)", "robotis/turtlebot3-waffle", (-0.95, 0.55, 0), (180, 0, 0), None),
    ("Ward 3 Handoff Table", "cyberwave/generictable", (-3.0, 0.55, 0), (0, 0, 0), (0.25, 0.3, 0.7)),
    ("Ward 3 Inbox", "cyberwave/generic-box", (-3.0, 0.55, Z * 0.7), (0, 0, 0), (0.18, 0.14, 0.06)),
]

out = {"environment": EID, "twins": {}}
for name, asset, pos, rot, scale in SCENE:
    t = cw.twin(asset, environment_id=EID, name=name)
    t.edit_position(x=pos[0], y=pos[1], z=pos[2])
    if any(rot):
        t.edit_rotation(yaw=rot[0], pitch=rot[1], roll=rot[2])
    if scale:
        t.edit_scale(x=scale[0], y=scale[1], z=scale[2])
    out["twins"][name] = {"uuid": t.uuid, "asset": asset, "position": pos}
    print(f"{name:38s} {t.uuid}")

for wp, (x, y) in {"pick_station": (-0.75, 0.55), "corridor": (-1.9, 0.55), "ward_3": (-2.55, 0.55)}.items():
    try:
        cw.environments.create_waypoint(EID, name=wp, position={"x": x, "y": y, "z": 0})
    except Exception as e:
        print("waypoint", wp, "failed:", e)
json.dump(out, open("/Users/aryan/Workspace/Hackathons/winwin/mastersunion-cyberwave/scene.json", "w"), indent=1)
print("done")
