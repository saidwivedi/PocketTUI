// ============================================================
// 3D viewer: meshes and point clouds
// ============================================================
// The viewer's fourth tenant (08-links.js picks it by MESH_EXT_RE). three.js
// and the loaders it ships for the common formats live in vendor/three.js, an
// ~800 KB bundle injected on the first model opened and never at boot, the way
// the editor brings in CodeMirror. Formats three has no loader for (OFF, and
// NumPy arrays, which is how research code tends to dump a cloud or a pose
// sequence) are parsed here; those parsers are pure functions of their bytes so
// the build tests can run them under node.
//
// The bytes come over /api/fs/download with the pairing header rather than a
// signed link: they are parsed here, not handed to a tag, and the files a model
// names beside itself (an .mtl, its textures, a glTF's .bin) go the same way.
// Every sibling is fetched up front and handed to three as a blob: URL, so no
// loader ever asks the page's own origin for a relative path it cannot serve.
//
// The scene draws on demand: a frame after the controls move (and while their
// damping settles), after a resize and after a theme change, and never on a
// loop, so a model left open costs nothing while nobody touches it.

let threeLoading = null;
function ensureThree() {
  if (window.THREE3D) return Promise.resolve();
  if (threeLoading) return threeLoading;
  threeLoading = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    // The editor's scheme (ensureCM, 29-editor.js): same relative root as the
    // boot-time vendor tags and the same ?v= the service worker manages.
    s.src = "vendor/three.js?v=" + buildVersion();
    s.onload = resolve;
    s.onerror = () => { threeLoading = null; s.remove(); reject(new Error("vendor load failed")); };
    document.head.appendChild(s);
  });
  return threeLoading;
}

function meshExt(path) {
  return (path.match(/\.([a-z0-9]+)$/i) || [, ""])[1].toLowerCase();
}

// A file a model names beside itself, as a path the explorer can fetch. Names
// come from files written on any machine, so a Windows separator is a
// separator, a drive-letter path is taken to mean "the file of that name next
// to the model" (the drive is not on this computer), and . and .. resolve.
function meshSibling(path, name) {
  let rel = String(name).trim().replace(/\\/g, "/");
  if (/^[a-z]:\//i.test(rel)) rel = rel.slice(rel.lastIndexOf("/") + 1);
  const dir = path.slice(0, path.lastIndexOf("/"));
  const parts = (rel.startsWith("/") ? rel : dir + "/" + rel).split("/");
  const out = [];
  for (const p of parts) {
    if (p === "" || p === ".") continue;
    if (p === "..") out.pop(); else out.push(p);
  }
  return "/" + out.join("/");
}

// A 3D Gaussian splat is stored as a PLY whose vertices carry spherical-
// harmonic colour (f_dc_*) and per-splat scales instead of a surface; drawn as
// a plain cloud it is a blur of the splat centres with no colour, which reads
// as a broken file. Only the header is read, as Latin-1 like the format says.
function meshIsSplatPly(bytes) {
  const head = bytes.subarray(0, Math.min(bytes.length, 65536));
  let text = "";
  for (let i = 0; i < head.length; i++) text += String.fromCharCode(head[i]);
  const end = text.indexOf("end_header");
  if (end >= 0) text = text.slice(0, end);
  return /\bproperty\s+\w+\s+f_dc_0\b/.test(text) && /\bproperty\s+\w+\s+scale_0\b/.test(text);
}

// .pts (Leica, CloudCompare) as the XYZ text XYZLoader reads. A .pts line is
// x y z, optionally an intensity, optionally r g b; the first line is usually
// the point count. XYZLoader takes three or six columns, so the count goes and
// so does the intensity (four columns become three, seven become six).
function meshPtsToXyz(text) {
  const out = [];
  for (const raw of text.split("\n")) {
    const t = raw.trim().split(/\s+/);
    if (t.length === 3 || t.length === 6) out.push(t.join(" "));
    else if (t.length === 4) out.push(t.slice(0, 3).join(" "));
    else if (t.length === 7) out.push(t.slice(0, 3).concat(t.slice(4)).join(" "));
  }
  return out.join("\n");
}

// sRGB to the linear values three's vertex colours are stored in; the loaders
// three ships do this themselves, the parsers below have to.
function meshLin(c) {
  return c < 0.04045 ? c * 0.0773993808 : Math.pow(c * 0.9478672986 + 0.0521327014, 2.4);
}

// OFF / COFF (Princeton shape benchmark, ModelNet): a header, the counts, the
// vertices (with r g b [a] after each in COFF, 0..255 or 0..1), then each face
// as its corner count and indices. Faces are polygons and become triangle
// fans. No faces at all is a point cloud. Colours come back in 0..1 sRGB.
function meshParseOff(text) {
  const lines = [];
  for (const raw of text.split("\n")) {
    const l = raw.replace(/#.*/, "").trim();
    if (l) lines.push(l.split(/\s+/));
  }
  if (!lines.length || !/^[A-Z]*OFF$/i.test(lines[0][0])) throw new Error("not an OFF file");
  // The counts may share the keyword's line ("OFF 8 6 0") or sit on their own.
  let counts = lines[0].slice(1), at = 1;
  if (counts.length < 2) counts = lines[at++] || [];
  const nv = parseInt(counts[0], 10), nf = parseInt(counts[1], 10) || 0;
  if (!(nv >= 0) || at + nv > lines.length) throw new Error("bad OFF counts");
  const positions = new Float32Array(nv * 3);
  let colors = null, scale = 1;
  for (let i = 0; i < nv; i++) {
    const v = lines[at + i];
    positions[i * 3] = +v[0]; positions[i * 3 + 1] = +v[1]; positions[i * 3 + 2] = +v[2];
    if (v.length >= 6) {
      if (!colors) colors = new Float32Array(nv * 3);
      for (let k = 0; k < 3; k++) colors[i * 3 + k] = +v[3 + k];
    }
  }
  if (colors) {
    for (let i = 0; i < colors.length; i++) if (colors[i] > 1) { scale = 255; break; }
    for (let i = 0; i < colors.length; i++) colors[i] /= scale;
  }
  at += nv;
  const idx = [];
  for (let f = 0; f < nf && at + f < lines.length; f++) {
    const t = lines[at + f], n = parseInt(t[0], 10);
    for (let k = 2; k < n; k++) idx.push(+t[1], +t[k], +t[k + 1]);
  }
  return { positions, colors, indices: new Uint32Array(idx), faces: nf };
}

// The .npy header: magic, version, header length (u16 in v1, u32 in v2 and
// v3), then a Python dict literal. Read with three regexes rather than a
// parser: numpy writes this dict itself and its shape never varies.
function meshParseNpyHeader(bytes) {
  const magic = [0x93, 0x4e, 0x55, 0x4d, 0x50, 0x59];  // \x93NUMPY
  for (let i = 0; i < 6; i++) if (bytes[i] !== magic[i]) throw new Error("not an .npy file");
  const major = bytes[6];
  const len = major === 1 ? bytes[8] | (bytes[9] << 8)
            : (bytes[8] | (bytes[9] << 8) | (bytes[10] << 16)) + bytes[11] * 16777216;
  const start = major === 1 ? 10 : 12;
  let text = "";
  for (let i = start; i < start + len; i++) text += String.fromCharCode(bytes[i]);
  const descr = (text.match(/'descr'\s*:\s*'([^']+)'/) || [])[1];
  const fortran = /'fortran_order'\s*:\s*True/.test(text);
  const dims = (text.match(/'shape'\s*:\s*\(([^)]*)\)/) || [])[1];
  if (descr === undefined || dims === undefined) throw new Error("bad .npy header");
  const shape = dims.split(",").map(s => s.trim()).filter(Boolean).map(s => parseInt(s, 10));
  return { descr, fortran, shape, offset: start + len };
}

// The array as float32 in C order, whatever it was stored as. Little-endian
// (or byte-sized) floats and ints only: that is what anything a desktop
// writes uses, and a big-endian or complex or object array is reported rather
// than guessed at. An unsupported dtype throws an error that says so.
function meshParseNpy(buffer) {
  const bytes = new Uint8Array(buffer);
  const h = meshParseNpyHeader(bytes);
  const m = h.descr.match(/^([<|=])([fiu])(\d)$/);
  const kinds = { f4: Float32Array, f8: Float64Array, i4: Int32Array, i8: BigInt64Array, u1: Uint8Array };
  const Type = m && kinds[m[2] + m[3]];
  if (!Type) { const e = new Error("dtype " + h.descr); e.dtype = true; throw e; }
  const n = h.shape.reduce((a, b) => a * b, 1);
  // Copied out so the typed array starts aligned whatever the header length.
  const raw = new Type(buffer.slice(h.offset, h.offset + n * Type.BYTES_PER_ELEMENT));
  const data = new Float32Array(n);
  for (let i = 0; i < n; i++) data[i] = Number(raw[i]);
  if (!h.fortran || h.shape.length < 2) return { shape: h.shape, data };
  // Column-major on disk: element (i0..ik) sits at sum(i_j * prod(shape[<j])).
  const out = new Float32Array(n), rank = h.shape.length, ix = new Array(rank).fill(0);
  for (let c = 0; c < n; c++) {
    let f = 0, stride = 1;
    for (let j = 0; j < rank; j++) { f += ix[j] * stride; stride *= h.shape[j]; }
    out[c] = data[f];
    for (let j = rank - 1; j >= 0; j--) { if (++ix[j] < h.shape[j]) break; ix[j] = 0; }
  }
  return { shape: h.shape, data: out };
}

// Which arrays can be drawn and how: (N,3) is a cloud, (N,6) a cloud with a
// colour per point (0..1 or 0..255), (T,J,3) a sequence of J joints or points
// over T frames, drawn a frame at a time. Anything else is null.
function meshNpyCloud(arr) {
  const s = arr.shape;
  if (s.length === 2 && s[1] === 3) return { frames: 1, count: s[0], positions: arr.data, colors: null };
  if (s.length === 3 && s[2] === 3) return { frames: s[0], count: s[1], positions: arr.data, colors: null };
  if (s.length === 2 && s[1] === 6) {
    const n = s[0], positions = new Float32Array(n * 3), colors = new Float32Array(n * 3);
    let max = 0;
    for (let i = 0; i < n; i++) for (let k = 0; k < 3; k++) {
      positions[i * 3 + k] = arr.data[i * 6 + k];
      const c = arr.data[i * 6 + 3 + k];
      colors[i * 3 + k] = c;
      if (c > max) max = c;
    }
    if (max > 1) for (let i = 0; i < colors.length; i++) colors[i] /= 255;
    return { frames: 1, count: n, positions, colors };
  }
  return null;
}

// An .npz is a zip of .npy files. Its central directory says where each entry
// is; the local header in front of the data is read only for its own name and
// extra lengths, since numpy writes those with zip64 placeholders for sizes.
function meshZipEntries(buffer) {
  const dv = new DataView(buffer), len = buffer.byteLength;
  let eocd = -1;
  for (let i = len - 22; i >= Math.max(0, len - 65557); i--) {
    if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error("not a zip file");
  const count = dv.getUint16(eocd + 10, true);
  let p = dv.getUint32(eocd + 16, true);
  const entries = [];
  for (let n = 0; n < count; n++) {
    if (dv.getUint32(p, true) !== 0x02014b50) throw new Error("bad zip directory");
    const method = dv.getUint16(p + 10, true);
    let csize = dv.getUint32(p + 20, true), usize = dv.getUint32(p + 24, true);
    const nameLen = dv.getUint16(p + 28, true), extraLen = dv.getUint16(p + 30, true);
    const commentLen = dv.getUint16(p + 32, true);
    let local = dv.getUint32(p + 42, true);
    const name = new TextDecoder().decode(new Uint8Array(buffer, p + 46, nameLen));
    // zip64: the 0xFFFFFFFF fields are in the extra block, in this order.
    let x = p + 46 + nameLen;
    const xEnd = x + extraLen;
    while (x + 4 <= xEnd) {
      const id = dv.getUint16(x, true), size = dv.getUint16(x + 2, true);
      if (id === 0x0001) {
        let q = x + 4;
        const big = () => { const v = Number(dv.getBigUint64(q, true)); q += 8; return v; };
        if (usize === 0xffffffff) usize = big();
        if (csize === 0xffffffff) csize = big();
        if (local === 0xffffffff) local = big();
      }
      x += 4 + size;
    }
    const data = local + 30 + dv.getUint16(local + 26, true) + dv.getUint16(local + 28, true);
    entries.push({ name, method, offset: data, csize, usize });
    p = xEnd + commentLen;
  }
  return entries;
}

// Every .npy in the archive, parsed. Stored entries are copied through and
// deflated ones inflated by the browser's own DecompressionStream, so there
// is no zip library to ship. Entries that are not arrays this can read are
// left out rather than failing the archive.
async function meshReadNpz(buffer) {
  const out = [];
  for (const e of meshZipEntries(buffer)) {
    if (!/\.npy$/i.test(e.name) || (e.method !== 0 && e.method !== 8)) continue;
    const slice = buffer.slice(e.offset, e.offset + e.csize);
    const body = e.method === 0 ? slice : await new Response(
      new Blob([slice]).stream().pipeThrough(new DecompressionStream("deflate-raw"))).arrayBuffer();
    try { out.push({ name: e.name.replace(/\.npy$/i, ""), arr: meshParseNpy(body) }); }
    catch (err) { if (err.dtype) out.push({ name: e.name.replace(/\.npy$/i, ""), dtype: true }); }
  }
  return out;
}

// The glTF JSON, from a .gltf as it is or from a .glb's first chunk. Read
// before three parses anything, to find the files it names and to refuse the
// compressed variants whose decoders are not in the bundle.
function meshGltfJson(buffer) {
  const dv = new DataView(buffer);
  let text;
  if (buffer.byteLength >= 20 && dv.getUint32(0, true) === 0x46546c67) {  // "glTF"
    text = new TextDecoder().decode(new Uint8Array(buffer, 20, dv.getUint32(12, true)));
  } else {
    text = new TextDecoder().decode(new Uint8Array(buffer));
  }
  return JSON.parse(text);
}

// The texture names an .mtl refers to: the last word of each map_* / bump /
// disp / decal / refl line, since what comes before it is options (-s 1 1 1).
function meshMtlTextures(text) {
  const names = new Set();
  for (const raw of text.split("\n")) {
    const t = raw.trim().split(/\s+/);
    if (t.length >= 2 && /^(?:map_\w+|bump|disp|decal|refl)$/i.test(t[0])) names.add(t[t.length - 1]);
  }
  return [...names];
}

// `Tr` in an .mtl is ambiguous: the spec reads it as transparency (1 = clear),
// but many exporters (Magic123 and other mesh tools among them) write `Tr 1`
// meaning opaque, and three's MTLLoader takes the spec's side, so such a
// model loads at opacity 0 and draws nothing. Flipping the loader's
// invertTrProperty would break the files that follow the spec, so only the
// case no one intends is fixed: a material that would be fully invisible is
// read as meant opaque. Partial transparency (d 0.5, Tr 0.5) is left alone.
function meshOpaqueRescue(materials) {
  for (const m of materials) {
    if (m && m.opacity <= 0.01) { m.opacity = 1; m.transparent = false; }
  }
}

// The paper and ink of whatever theme and palette is on, read off the root so
// the model sits on the same ground as the pane around it.
function meshTheme() {
  const cs = getComputedStyle(document.documentElement);
  return { paper: cs.getPropertyValue("--paper").trim() || "#FAF8F3",
           ink: cs.getPropertyValue("--ink").trim() || "#1a1814" };
}

// Open `path` into `host`. Resolves to { destroy(), resize() }, or to null
// when there is nothing to show — after saying why, unless the open was
// abandoned (opts.signal aborted), which says nothing. Everything this makes
// lives under one element of its own inside the host, so a late load that
// lost the race can only ever clear itself.
async function meshMount(host, path, opts = {}) {
  const signal = opts.signal;
  const ext = meshExt(path);
  const stage = document.createElement("div");
  stage.className = "mesh-stage";
  const info = document.createElement("div");
  info.className = "mesh-info";
  info.textContent = "Loading…";
  const ctl = document.createElement("div");
  ctl.className = "mesh-ctl";
  stage.append(info, ctl);
  host.appendChild(stage);

  const blobs = [];
  let renderer = null, scene = null, controls = null, ro = null, mo = null;
  let raf = 0, current = null, themed = [];
  let materialsWarned = false;
  const refuse = (msg) => { const e = new Error(msg); e.userMsg = msg; return e; };
  const gone = () => !!(signal && signal.aborted);

  async function bytesOf(p) {
    const r = await fetch(apiURL("api/fs/download?path=" + encodeURIComponent(p)),
                          { cache: "no-store", headers: authHeaders(), signal });
    if (r.status === 401) { const e = new Error("401"); e.auth = true; throw e; }
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.arrayBuffer();
  }
  // Each named sibling as a blob: URL, or null when it could not be fetched.
  async function siblings(names, encoded) {
    const map = new Map();
    await Promise.all(names.map(async (name) => {
      try {
        // glTF names are URIs (a space is %20); an .mtl's are plain names.
        const b = await bytesOf(meshSibling(path, encoded ? decodeURI(name) : name));
        const url = URL.createObjectURL(new Blob([b]));
        blobs.push(url);
        map.set(name, url);
      } catch (e) {
        if (e.auth) throw e;
        map.set(name, null);
      }
    }));
    return map;
  }
  function warnMaterials() {
    if (materialsWarned) return;
    materialsWarned = true;
    toast("Materials not found, showing plain mesh");
  }

  function dispose(obj) {
    obj.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      const mats = Array.isArray(o.material) ? o.material : o.material ? [o.material] : [];
      for (const m of mats) {
        for (const v of Object.values(m)) if (v && v.isTexture) v.dispose();
        m.dispose();
      }
    });
  }

  function destroy() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
    if (ro) ro.disconnect();
    if (mo) mo.disconnect();
    window.removeEventListener("ptui-termtheme", retheme);
    if (controls) controls.dispose();
    if (current) dispose(current);
    current = null;
    if (renderer) { renderer.dispose(); renderer.forceContextLoss(); renderer = null; }
    for (const u of blobs) URL.revokeObjectURL(u);
    blobs.length = 0;
    stage.remove();
  }

  function request() {
    if (!raf && renderer) raf = requestAnimationFrame(frame);
  }
  function frame() {
    raf = 0;
    if (!renderer) return;
    // While the damping settles, update() moves the camera and fires the
    // change event that asks for the next frame; at rest it fires nothing.
    controls.update();
    renderer.render(scene, controls.object);
  }
  function resize() {
    if (!renderer) return;
    const w = stage.clientWidth, h = stage.clientHeight;
    if (!w || !h) return;
    renderer.setSize(w, h, false);
    controls.object.aspect = w / h;
    controls.object.updateProjectionMatrix();
    request();
  }
  function retheme() {
    if (!scene) return;
    const t = meshTheme();
    scene.background = new THREE3D.Color(t.paper);
    for (const m of themed) m.color.set(t.ink);
    request();
  }

  // The object the scene shows, replacing whatever it showed before (an .npz
  // switches arrays this way), and the camera framed on it.
  function show(obj, label) {
    const T = THREE3D;
    if (current) { scene.remove(current); dispose(current); }
    current = obj;
    scene.add(obj);
    // Centred on the box, sized by the farthest vertex from that centre: the
    // box's own sphere reaches its corners, which leaves a round cloud at
    // about half the pane.
    const center = new T.Box3().setFromObject(obj).getCenter(new T.Vector3());
    const v = new T.Vector3();
    let far = 0;
    obj.updateMatrixWorld(true);
    obj.traverse((o) => {
      const pos = o.geometry && o.geometry.getAttribute("position");
      if (!pos) return;
      for (let i = 0; i < pos.count; i++) {
        far = Math.max(far, v.fromBufferAttribute(pos, i).applyMatrix4(o.matrixWorld).distanceToSquared(center));
      }
    });
    const r = Math.sqrt(far) || 1;
    const cam = controls.object;
    // Framed by the narrower of the two angles: a tall pane (the docked
    // column, a phone held upright) is limited by its width, not its height.
    const vfov = cam.fov * Math.PI / 180;
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * cam.aspect);
    const dist = r / Math.sin(Math.min(vfov, hfov) / 2) * 1.1;
    cam.near = dist / 1000;
    cam.far = dist * 10 + r;
    cam.position.copy(center).add(new T.Vector3(0.6, 0.45, 1).normalize().multiplyScalar(dist));
    cam.updateProjectionMatrix();
    controls.target.copy(center);
    controls.update();
    info.textContent = label;
    request();
  }

  function bboxDiagonal(geom) {
    geom.computeBoundingBox();
    return geom.boundingBox.getSize(new THREE3D.Vector3()).length() || 1;
  }
  function pointsOf(geom) {
    const T = THREE3D, hasColor = !!geom.getAttribute("color");
    const mat = new T.PointsMaterial({ size: bboxDiagonal(geom) / 300, sizeAttenuation: true,
                                       vertexColors: hasColor });
    if (!hasColor) { mat.color.set(meshTheme().ink); themed.push(mat); }
    return new T.Points(geom, mat);
  }
  function surfaceMaterial(geom) {
    const T = THREE3D, hasColor = !!geom.getAttribute("color");
    return new T.MeshStandardMaterial({
      color: hasColor ? 0xffffff : 0xb3ada2, vertexColors: hasColor, side: T.DoubleSide,
      flatShading: !geom.getAttribute("normal"), metalness: 0, roughness: 0.8 });
  }
  function surfaceOf(geom) {
    return new THREE3D.Mesh(geom, surfaceMaterial(geom));
  }
  function geometryFrom(positions, colors, indices) {
    const T = THREE3D, g = new T.BufferGeometry();
    g.setAttribute("position", new T.BufferAttribute(positions, 3));
    if (colors) {
      const lin = new Float32Array(colors.length);
      for (let i = 0; i < colors.length; i++) lin[i] = meshLin(colors[i]);
      g.setAttribute("color", new T.BufferAttribute(lin, 3));
    }
    if (indices && indices.length) g.setIndex(new T.BufferAttribute(indices, 1));
    return g;
  }

  // Counts for the line in the corner, over everything the object holds.
  function label(obj) {
    let verts = 0, faces = 0, points = 0;
    obj.traverse((o) => {
      const pos = o.geometry && o.geometry.getAttribute("position");
      if (!pos) return;
      if (o.isPoints) { points += pos.count; return; }
      if (!o.isMesh) return;
      verts += pos.count;
      faces += (o.geometry.index ? o.geometry.index.count : pos.count) / 3;
    });
    if (!verts) return points.toLocaleString() + " points";
    return verts.toLocaleString() + " vertices · " + Math.round(faces).toLocaleString() + " faces";
  }

  function plyObject(buf) {
    if (meshIsSplatPly(new Uint8Array(buf))) throw refuse("Gaussian splat files are not supported yet");
    const geom = new THREE3D.PLYLoader().parse(buf);
    return geom.index ? surfaceOf(geom) : pointsOf(geom);
  }

  async function objObject(buf) {
    const T = THREE3D;
    const text = new TextDecoder().decode(buf);
    const manager = new T.LoadingManager();
    manager.onLoad = request;  // textures land after the first frame
    const loader = new T.OBJLoader(manager);
    const lib = (text.match(/^\s*mtllib\s+(.+?)\s*$/m) || [])[1];
    let plain = true;
    if (lib) {
      let mtl = null;
      try { mtl = new TextDecoder().decode(await bytesOf(meshSibling(path, lib))); }
      catch (e) { if (e.auth) throw e; }
      if (mtl === null) warnMaterials();
      else {
        const tex = await siblings(meshMtlTextures(mtl));
        if ([...tex.values()].some(u => u === null)) warnMaterials();
        else {
          manager.setURLModifier((u) => tex.get(u) || u);
          const materials = new T.MTLLoader(manager)
            .setMaterialOptions({ side: T.DoubleSide }).parse(mtl, "");
          materials.preload();
          loader.setMaterials(materials);
          plain = false;
        }
      }
    }
    const group = loader.parse(text);
    let drawn = false;
    group.traverse((o) => {
      if (!o.geometry) return;
      drawn = true;
      if (o.isMesh && plain) {
        const old = Array.isArray(o.material) ? o.material : [o.material];
        for (const m of old) m.dispose();
        o.material = surfaceMaterial(o.geometry);
      } else if (o.isMesh) {
        const mats = Array.isArray(o.material) ? o.material : [o.material];
        for (const m of mats) m.side = T.DoubleSide;
        meshOpaqueRescue(mats);
        // OBJLoader adds face normals when the file has no vn lines; this is a
        // guard so a lit material never gets a geometry without normals.
        if (!o.geometry.getAttribute("normal")) o.geometry.computeVertexNormals();
      }
    });
    if (drawn) return group;
    // Vertices and no faces: OBJLoader draws nothing for that, but it is how a
    // lot of tools write a point cloud.
    const v = [];
    for (const m of text.matchAll(/^\s*v\s+(\S+)\s+(\S+)\s+(\S+)/gm)) v.push(+m[1], +m[2], +m[3]);
    if (!v.length) throw new Error("empty OBJ");
    return pointsOf(geometryFrom(new Float32Array(v), null, null));
  }

  async function gltfObject(buf) {
    const T = THREE3D;
    const json = meshGltfJson(buf);
    const used = [...(json.extensionsUsed || []), ...(json.extensionsRequired || [])];
    if (used.some(x => /draco|basisu|meshopt/i.test(x)))
      throw refuse("Compressed glTF (Draco/KTX2) is not supported yet");
    const named = [];
    for (const list of [json.buffers || [], json.images || []])
      for (const it of list) if (it.uri && !/^data:/i.test(it.uri)) named.push(it.uri);
    const files = await siblings(named, true);
    // A missing buffer is a model with holes in it and fails below; a missing
    // texture is drawn without it, the .mtl's rule. The stand-in is a blank
    // pixel so the loader asks nobody for a name that does not exist.
    const blank = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/axP2jEAAAAASUVORK5CYII=";
    for (const it of json.images || []) if (it.uri && files.get(it.uri) === null) warnMaterials();
    const manager = new T.LoadingManager();
    manager.onLoad = request;
    manager.setURLModifier((u) => {
      if (!files.has(u)) return u;
      return files.get(u) || blank;
    });
    const gltf = await new Promise((resolve, reject) =>
      new T.GLTFLoader(manager).parse(buf, "", resolve, reject));
    return gltf.scene;
  }

  function stlObject(buf) {
    return surfaceOf(new THREE3D.STLLoader().parse(buf));
  }

  function pointsText(buf, pts) {
    let text = new TextDecoder().decode(buf);
    if (pts) text = meshPtsToXyz(text);
    const geom = new THREE3D.XYZLoader().parse(text);
    if (!geom.getAttribute("position") || !geom.getAttribute("position").count) throw new Error("no points");
    return pointsOf(geom);
  }

  function offObject(buf) {
    const off = meshParseOff(new TextDecoder().decode(buf));
    const geom = geometryFrom(off.positions, off.colors, off.indices);
    if (off.indices.length) { geom.computeVertexNormals(); return surfaceOf(geom); }
    return pointsOf(geom);
  }

  // A drawable array becomes a cloud, and a sequence gets a slider over its
  // frames; the slider writes the frame's slice into the one geometry.
  function cloudObject(cloud) {
    const per = cloud.count * 3;
    const geom = geometryFrom(cloud.positions.slice(0, per), cloud.colors, null);
    const obj = pointsOf(geom);
    for (const old of ctl.querySelectorAll(".mesh-frames")) old.remove();
    if (cloud.frames > 1) {
      const range = document.createElement("input");
      range.type = "range";
      range.className = "mesh-frames";
      range.min = "0"; range.max = String(cloud.frames - 1); range.value = "0";
      range.setAttribute("aria-label", "Frame");
      range.addEventListener("input", () => {
        const t = +range.value, attr = geom.getAttribute("position");
        attr.array.set(cloud.positions.subarray(t * per, (t + 1) * per));
        attr.needsUpdate = true;
        request();
      });
      ctl.appendChild(range);
    }
    return obj;
  }

  async function arrayObject(buf) {
    const dtypeMsg = "Unsupported array dtype";
    if (ext === "npy") {
      let arr;
      try { arr = meshParseNpy(buf); }
      catch (e) { if (e.dtype) throw refuse(dtypeMsg); throw e; }
      const cloud = meshNpyCloud(arr);
      if (!cloud) throw refuse("Array shape (" + arr.shape.join(", ") + ") is not a point set");
      return cloudObject(cloud);
    }
    const all = await meshReadNpz(buf);
    const usable = all.filter(a => a.arr && meshNpyCloud(a.arr));
    if (!usable.length) throw refuse(all.some(a => a.dtype) ? dtypeMsg : "No point array in this file");
    if (usable.length > 1) {
      const sel = document.createElement("select");
      sel.className = "mesh-pick";
      sel.setAttribute("aria-label", "Array");
      for (const a of usable) {
        const o = document.createElement("option");
        o.value = a.name;
        o.textContent = a.name + " (" + a.arr.shape.join("×") + ")";
        sel.appendChild(o);
      }
      sel.addEventListener("change", () => {
        themed = [];
        const obj = cloudObject(meshNpyCloud(usable[sel.selectedIndex].arr));
        show(obj, label(obj));
      });
      ctl.prepend(sel);
    }
    return cloudObject(meshNpyCloud(usable[0].arr));
  }

  try {
    const [buf] = await Promise.all([bytesOf(path), ensureThree()]);
    if (gone()) { destroy(); return null; }
    const T = THREE3D;
    renderer = new T.WebGLRenderer({ antialias: true, preserveDrawingBuffer: !!opts.keepBuffer });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    stage.prepend(renderer.domElement);
    scene = new T.Scene();
    scene.add(new T.HemisphereLight(0xffffff, 0x8a8478, 1.6));
    const sun = new T.DirectionalLight(0xffffff, 1.6);
    sun.position.set(1, 2, 1.5);
    sun.target.position.set(0, 0, -1);
    const cam = new T.PerspectiveCamera(45, 1, 0.01, 1000);
    // The light rides with the camera, so the side being looked at is lit
    // however the model is turned.
    scene.add(cam);
    cam.add(sun, sun.target);
    controls = new T.OrbitControls(cam, renderer.domElement);
    controls.enableDamping = true;
    controls.addEventListener("change", request);

    let obj;
    if (ext === "ply") obj = plyObject(buf);
    else if (ext === "obj") obj = await objObject(buf);
    else if (ext === "stl") obj = stlObject(buf);
    else if (ext === "glb" || ext === "gltf") obj = await gltfObject(buf);
    else if (ext === "pcd") {
      const pcd = new T.PCDLoader().parse(buf);
      pcd.material.dispose();
      obj = pointsOf(pcd.geometry);
    }
    else if (ext === "xyz" || ext === "pts") obj = pointsText(buf, ext === "pts");
    else if (ext === "off") obj = offObject(buf);
    else obj = await arrayObject(buf);
    if (gone()) { dispose(obj); destroy(); return null; }

    retheme();
    // Sized first, so the framing below knows the pane's shape.
    resize();
    show(obj, label(obj));
    ro = new ResizeObserver(resize);
    ro.observe(stage);
    // The 29-editor.js pair: the attribute for light/dark, the event for a
    // palette swapped for another of the same lightness.
    mo = new MutationObserver(retheme);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "style"] });
    window.addEventListener("ptui-termtheme", retheme);
    return { destroy, resize };
  } catch (e) {
    const aborted = gone() || (e && e.name === "AbortError");
    destroy();
    if (aborted) return null;
    if (e && e.auth) rejectToken();
    else toast(e && e.userMsg ? e.userMsg : "Couldn't open model");
    return null;
  }
}
