import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js';

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const chartSets = {
  '24h': {
    fetched: [32, 49, 42, 71, 55, 78, 64, 95, 82, 106, 91, 119, 108, 139, 123, 151],
    copied: [25, 37, 32, 55, 45, 63, 53, 75, 66, 85, 77, 96, 89, 111, 98, 118],
    total: '892',
  },
  '7d': {
    fetched: [55, 61, 78, 58, 91, 104, 82, 110, 122, 101, 137, 145, 130, 158, 149, 180],
    copied: [42, 48, 63, 46, 73, 86, 69, 89, 98, 80, 108, 115, 103, 124, 117, 140],
    total: '6.4k',
  },
  '30d': {
    fetched: [82, 94, 76, 120, 111, 133, 145, 127, 165, 178, 160, 190, 207, 198, 221, 240],
    copied: [63, 75, 61, 96, 89, 104, 116, 102, 130, 143, 126, 151, 166, 158, 178, 194],
    total: '27.8k',
  },
};

function pointsFor(values, width = 800, height = 208) {
  const max = 250;
  return values.map((value, index) => ({
    x: (index / (values.length - 1)) * width,
    y: height - (value / max) * height,
  }));
}

function pathFrom(points) {
  return points.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(1)},${point.y.toFixed(1) + 20}`).join(' ');
}

function areaFrom(points, height = 228) {
  return `${pathFrom(points)} L${points.at(-1).x.toFixed(1)},${height} L0,${height} Z`;
}

function renderChart(range = '24h') {
  const data = chartSets[range];
  const fetched = pointsFor(data.fetched);
  const copied = pointsFor(data.copied);
  $('#cyan-path').setAttribute('d', pathFrom(fetched));
  $('#peach-path').setAttribute('d', pathFrom(copied));
  $('#cyan-area').setAttribute('d', areaFrom(fetched));
  $('#peach-area').setAttribute('d', areaFrom(copied));
  $('#chart-total').textContent = data.total;
  const point = fetched[11];
  $('#chart-crosshair').setAttribute('x1', point.x);
  $('#chart-crosshair').setAttribute('x2', point.x);
  $('#chart-point').setAttribute('cx', point.x);
  $('#chart-point').setAttribute('cy', point.y + 20);
}

function setupChart() {
  renderChart();
  $$('.range').forEach((button) => {
    button.addEventListener('click', () => {
      $$('.range').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      renderChart(button.dataset.range);
    });
  });
}

function setupNavigation() {
  const sections = ['overview', 'sources', 'activity', 'policies'].map((id) => document.getElementById(id));
  const links = $$('.nav-item');
  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    links.forEach((link) => link.classList.toggle('active', link.getAttribute('href') === `#${visible.target.id}`));
  }, { rootMargin: '-20% 0px -65% 0px', threshold: [0, .2, .5] });
  sections.forEach((section) => observer.observe(section));
}

function showToast(message, success = true) {
  const toast = $('#toast');
  $('#toast-text').textContent = message;
  $('.toast-icon').textContent = success ? '✓' : '!';
  toast.classList.add('show');
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove('show'), 3400);
}

function setNumber(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = String(value).padStart(id === 'scrubbed-value' ? 2 : 1, '0');
}

function animateRun(result = {}) {
  const fetched = Number(result.fetched ?? 148);
  const copied = Number(result.copied ?? 118);
  const held = Number(result.skipped_invalid ?? 24);
  const scrubbed = Number(result.filtered_links ?? 6);
  setNumber('fetched-value', fetched);
  setNumber('copied-value', copied);
  setNumber('held-value', held);
  setNumber('scrubbed-value', scrubbed);
  $('#run-state').textContent = result.halted ? 'Run paused safely' : result.source_errors_count ? 'Completed with holds' : 'Completed cleanly';
  $('#last-pulse').textContent = new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) + ' IST';
}

async function triggerSync() {
  const button = $('#run-button');
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = '<span class="button-orbit spinning">✦</span><span>Syncing the signal…</span>';
  const endpoint = window.RELAYROOM_CONFIG?.functionUrl;
  try {
    if (endpoint) {
      const response = await fetch(endpoint, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source: 'relayroom' }) });
      const result = await response.json();
      if (!response.ok || result.ok === false) throw new Error(result.error || 'The sync function returned an error.');
      animateRun(result);
      showToast(`Sync complete · ${result.copied ?? 0} messages copied.`);
    } else {
      await new Promise((resolve) => window.setTimeout(resolve, 1100));
      const bump = Math.floor(Math.random() * 9);
      animateRun({ fetched: 148 + bump, copied: 118 + bump, skipped_invalid: 24, filtered_links: 6 });
      showToast('Sync complete · 118 messages copied.');
    }
  } catch (error) {
    showToast(error.message || 'Sync could not be completed.', false);
  } finally {
    button.disabled = false;
    button.innerHTML = original;
  }
}

function setupCountdown() {
  let seconds = 522;
  const element = $('#countdown');
  const tick = () => {
    seconds = seconds > 0 ? seconds - 1 : 599;
    const minutes = Math.floor(seconds / 60).toString().padStart(2, '0');
    const remainder = (seconds % 60).toString().padStart(2, '0');
    element.textContent = `${minutes}:${remainder}`;
  };
  window.setInterval(tick, 1000);
}

function setupDetails() {
  $('#details-button').addEventListener('click', () => {
    $('#activity').scrollIntoView({ behavior: 'smooth', block: 'center' });
    showToast('Run details are visible in the flow overview.');
  });
}

function createRelayCore() {
  const canvas = document.getElementById('relay-canvas');
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(34, 230 / 190, .1, 100);
  camera.position.set(0, 0, 5.2);
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(230, 190, false);
  renderer.setClearColor(0x000000, 0);

  const core = new THREE.Group();
  scene.add(core);
  const cyan = new THREE.MeshBasicMaterial({ color: 0x8ee8e0, wireframe: true, transparent: true, opacity: .82 });
  const warm = new THREE.MeshBasicMaterial({ color: 0xf29a72, wireframe: true, transparent: true, opacity: .66 });
  const inner = new THREE.MeshBasicMaterial({ color: 0xb5a4ec, transparent: true, opacity: .42 });
  const shell = new THREE.Mesh(new THREE.IcosahedronGeometry(1.12, 2), cyan);
  const secondary = new THREE.Mesh(new THREE.IcosahedronGeometry(1.38, 1), warm);
  const nucleus = new THREE.Mesh(new THREE.SphereGeometry(.48, 24, 24), inner);
  core.add(shell, secondary, nucleus);

  const ringA = new THREE.Mesh(new THREE.TorusGeometry(1.48, .012, 8, 80), cyan);
  ringA.rotation.set(.8, -.25, -.2);
  const ringB = new THREE.Mesh(new THREE.TorusGeometry(1.7, .008, 8, 80), warm);
  ringB.rotation.set(-.35, .75, .4);
  core.add(ringA, ringB);

  const particleGeometry = new THREE.BufferGeometry();
  const particles = new Float32Array(110 * 3);
  for (let i = 0; i < particles.length; i += 3) {
    const radius = 1.8 + Math.random() * 1.15;
    const angle = Math.random() * Math.PI * 2;
    particles[i] = Math.cos(angle) * radius;
    particles[i + 1] = (Math.random() - .5) * 2.6;
    particles[i + 2] = Math.sin(angle) * radius;
  }
  particleGeometry.setAttribute('position', new THREE.BufferAttribute(particles, 3));
  const particleMaterial = new THREE.PointsMaterial({ color: 0x8ee8e0, size: .022, transparent: true, opacity: .7 });
  scene.add(new THREE.Points(particleGeometry, particleMaterial));

  const resize = () => {
    const width = canvas.clientWidth || 230;
    const height = canvas.clientHeight || 190;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
  };
  const observer = new ResizeObserver(resize);
  observer.observe(canvas);
  let frame = 0;
  const render = () => {
    frame += .006;
    core.rotation.y = frame;
    core.rotation.x = Math.sin(frame * .9) * .16;
    shell.rotation.z = frame * .7;
    secondary.rotation.y = -frame * .8;
    ringA.rotation.z += .002;
    ringB.rotation.x += .0015;
    nucleus.scale.setScalar(1 + Math.sin(frame * 4) * .06);
    renderer.render(scene, camera);
    requestAnimationFrame(render);
  };
  resize();
  render();
}

setupChart();
setupNavigation();
setupCountdown();
setupDetails();
$('#run-button').addEventListener('click', triggerSync);
createRelayCore();
