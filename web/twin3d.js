// 3D digital twin built from the twin's own universal schema (URDF joints + link meshes).
// Two copies of the robot share one scene: a translucent "ghost" for the simulation
// rehearsal and a solid one for the real robot. control.html calls Twin3D.setPose().
import * as THREE from "three";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const host = document.getElementById("three");
const data = await (await fetch("so101.json")).json();

const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(devicePixelRatio);
host.appendChild(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(38, 1, 0.01, 20);
camera.position.set(0.42, 0.36, 0.48);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0.16, 0);
controls.enableDamping = true;
controls.autoRotate = true;
controls.autoRotateSpeed = 0.6;

scene.add(new THREE.HemisphereLight(0xdfefff, 0x0b0f14, 1.1));
const key = new THREE.DirectionalLight(0xffffff, 1.6);
key.position.set(1, 2, 1.5);
scene.add(key);
const grid = new THREE.GridHelper(1.2, 24, 0x1f2a38, 0x121821);
scene.add(grid);

const loader = new STLLoader();
const files = [...new Set(data.links.flatMap((l) => l.visuals.map((v) => v.file)).filter(Boolean))];
const geoms = Object.fromEntries(await Promise.all(files.map(async (f) => {
  const g = await loader.loadAsync(`assets/${f}.stl`);
  g.computeVertexNormals();
  return [f, g];
})));

function buildRobot(material) {
  const links = {};
  for (const l of data.links) {
    const g = new THREE.Group();
    for (const v of l.visuals) {
      if (!geoms[v.file]) continue;
      const m = new THREE.Mesh(geoms[v.file], material);
      const p = v.pose.position, o = v.pose.orientation;
      m.position.set(p.x, p.y, p.z);
      m.quaternion.set(o.x, o.y, o.z, o.w);
      g.add(m);
    }
    links[l.name] = g;
  }
  const joints = {};
  const children = new Set();
  for (const j of data.joints) {
    const origin = new THREE.Group();
    const p = j.pose.position, o = j.pose.orientation;
    origin.position.set(p.x, p.y, p.z);
    origin.quaternion.set(o.x, o.y, o.z, o.w);
    const rot = new THREE.Group();
    origin.add(rot);
    rot.add(links[j.child]);
    links[j.parent].add(origin);
    children.add(j.child);
    joints[j.name] = { rot, axis: new THREE.Vector3(j.axis.x, j.axis.y, j.axis.z).normalize(), cur: 0, goal: 0 };
  }
  const rootName = data.links.map((l) => l.name).find((n) => !children.has(n));
  const root = new THREE.Group();
  root.rotation.x = -Math.PI / 2; // URDF is Z-up, three.js is Y-up
  root.add(links[rootName]);
  scene.add(root);
  return { root, joints };
}

const ghostMat = new THREE.MeshStandardMaterial({
  color: 0x39d0ff, emissive: 0x0a6f99, emissiveIntensity: 0.6,
  transparent: true, opacity: 0.3, depthWrite: false, metalness: 0.1, roughness: 0.4,
});
const realMat = new THREE.MeshStandardMaterial({ color: 0xffb020, metalness: 0.25, roughness: 0.45 });
const robots = { live: buildRobot(realMat), sim: buildRobot(ghostMat) };
robots.sim.root.renderOrder = 2;

// Frame the camera on the robot's real size.
const box = new THREE.Box3().setFromObject(robots.live.root);
const size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
const dim = Math.max(size.x, size.y, size.z);
grid.position.y = box.min.y;
controls.target.copy(center);
camera.position.copy(center).add(new THREE.Vector3(1.0, 0.75, 1.15).multiplyScalar(dim * 1.15));

function resize() {
  const r = host.getBoundingClientRect();
  renderer.setSize(r.width, r.height, false);
  camera.aspect = r.width / Math.max(1, r.height);
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(host);
resize();

let lastPoseAt = 0;
function tick() {
  for (const r of Object.values(robots)) {
    for (const j of Object.values(r.joints)) {
      j.cur += (j.goal - j.cur) * 0.2;
      j.rot.quaternion.setFromAxisAngle(j.axis, j.cur);
    }
  }
  // Ghost is only visible while the rehearsal differs from the real robot.
  let diff = 0;
  for (const [n, j] of Object.entries(robots.sim.joints)) diff = Math.max(diff, Math.abs(j.cur - (robots.live.joints[n]?.cur ?? 0)));
  const target = Math.min(0.42, 0.06 + diff * 3);
  ghostMat.opacity += (target - ghostMat.opacity) * 0.15;
  controls.autoRotate = performance.now() - lastPoseAt > 6000;
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(tick);
}
tick();

window.Twin3D = {
  ready: true,
  setPose(target, jointsDeg) {
    const r = robots[target];
    if (!r) return;
    lastPoseAt = performance.now();
    for (const [name, deg] of Object.entries(jointsDeg)) {
      if (r.joints[name]) r.joints[name].goal = (deg * Math.PI) / 180;
    }
  },
};
window.dispatchEvent(new Event("twin3d-ready"));
