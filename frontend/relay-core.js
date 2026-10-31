import * as THREE from 'three';

export function createRelayCore(canvas) {
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(34, 1, .1, 100);
  camera.position.z = 6;
  const core = new THREE.Group();
  scene.add(core);
  const cyan = new THREE.MeshBasicMaterial({ color: 0x8ee8e0, wireframe: true, transparent: true, opacity: .7 });
  const warm = new THREE.MeshBasicMaterial({ color: 0xf29a72, wireframe: true, transparent: true, opacity: .6 });
  core.add(new THREE.Mesh(new THREE.IcosahedronGeometry(1.12, 2), cyan));
  const shell = new THREE.Mesh(new THREE.IcosahedronGeometry(1.38, 1), warm);
  core.add(shell);
  const ring = new THREE.Mesh(new THREE.TorusGeometry(1.7, .012, 8, 80), cyan);
  ring.rotation.set(.8, -.25, -.2);
  core.add(ring);
  const draw = () => renderer.render(scene, camera);
  const resize = new ResizeObserver(() => {
    const width = canvas.clientWidth || 230;
    const height = canvas.clientHeight || 190;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
    draw();
  });
  resize.observe(canvas);
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  renderer.setAnimationLoop((time) => {
    if (document.hidden || reduced.matches) return;
    core.rotation.y = time * .0003;
    shell.rotation.z = time * .0002;
    draw();
  });
}
