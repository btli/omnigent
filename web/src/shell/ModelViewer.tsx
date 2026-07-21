// Interactive 3D preview for STL / 3MF / OBJ files.
//
// Mirrors the ImageViewer pattern in CodeViewer: it takes the already-fetched
// FileContentResponse, decodes it via the shared `fileContentToBlob`, and
// renders it. three.js is imported at this module's top level so the lazy
// dynamic import in CodeViewer keeps the whole 3D stack out of the main bundle
// (same strategy as Monaco).
//
// The whole three.js scene is torn down on unmount — geometry, materials,
// renderer, the animation frame, and the WebGL context are all released so
// repeatedly opening model files can't leak GPU memory.

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { ThreeMFLoader } from "three/examples/jsm/loaders/3MFLoader.js";
import { OBJLoader } from "three/examples/jsm/loaders/OBJLoader.js";
import { type FileContentResponse, fileContentToBlob } from "@/hooks/useFileContent";
import { TruncatedBanner } from "./TruncatedBanner";

type ModelFormat = "stl" | "3mf" | "obj";

function formatForPath(path: string): ModelFormat | null {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  if (ext === "stl" || ext === "3mf" || ext === "obj") return ext;
  return null;
}

/**
 * Parse raw model bytes into a three.js Object3D for the given format.
 *
 * STL and 3MF are parsed from an ArrayBuffer; OBJ is text. Each loader's
 * `parse` is synchronous and self-contained (no network), so a truncated or
 * malformed file throws here and the caller surfaces the error state.
 */
function parseModel(format: ModelFormat, buffer: ArrayBuffer): THREE.Object3D {
  if (format === "stl") {
    const geometry = new STLLoader().parse(buffer);
    // STL carries no material — give it a neutral surface that shows the
    // geometry's shading under the scene lights.
    const material = new THREE.MeshStandardMaterial({
      color: 0x9aa0a6,
      metalness: 0.1,
      roughness: 0.6,
    });
    return new THREE.Mesh(geometry, material);
  }
  if (format === "3mf") {
    return new ThreeMFLoader().parse(buffer);
  }
  // OBJ is ASCII text.
  const text = new TextDecoder().decode(buffer);
  return new OBJLoader().parse(text);
}

/**
 * Frame `object` in `camera`: center it at the origin and pull the camera back
 * far enough that the whole bounding sphere is visible, then aim OrbitControls
 * at the center.
 */
function fitToObject(
  object: THREE.Object3D,
  camera: THREE.PerspectiveCamera,
  controls: OrbitControls,
): void {
  const box = new THREE.Box3().setFromObject(object);
  if (box.isEmpty()) return;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());

  // Recenter the model on the origin so orbiting rotates around its middle.
  object.position.sub(center);

  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  const fov = (camera.fov * Math.PI) / 180;
  // Distance so the bounding sphere fits the vertical FOV, with a little margin.
  const distance = (maxDim / 2 / Math.tan(fov / 2)) * 1.6;

  camera.position.set(distance, distance * 0.6, distance);
  camera.near = distance / 100;
  camera.far = distance * 100;
  camera.updateProjectionMatrix();

  controls.target.set(0, 0, 0);
  controls.update();
}

/**
 * Dispose all geometries and materials reachable from `object` so the GPU
 * buffers backing them are released. three.js does not do this automatically.
 */
function disposeObject(object: THREE.Object3D): void {
  object.traverse((child) => {
    const mesh = child as THREE.Mesh;
    mesh.geometry?.dispose();
    const material = mesh.material;
    if (Array.isArray(material)) {
      material.forEach((m) => m.dispose());
    } else {
      material?.dispose();
    }
  });
}

export function ModelViewer({ data, path }: { data: FileContentResponse; path: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [errored, setErrored] = useState(false);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    // A truncated model is a partial byte stream that won't parse into a valid
    // mesh — skip the scene and go straight to the error UI.
    if (data.truncated) {
      setErrored(true);
      return;
    }
    const format = formatForPath(path);
    if (!format) {
      setErrored(true);
      return;
    }

    setErrored(false);

    let renderer: THREE.WebGLRenderer | null = null;
    let controls: OrbitControls | null = null;
    let object: THREE.Object3D | null = null;
    let rafId = 0;
    let resizeObserver: ResizeObserver | null = null;
    let disposed = false;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000);

    try {
      const blob = fileContentToBlob(data);
      // fileContentToBlob handles both base64 (binary STL/3MF) and utf-8 (ASCII
      // OBJ/STL); read it back as an ArrayBuffer for the loaders. This is async,
      // so guard every step against the effect having been cleaned up.
      blob
        .arrayBuffer()
        .then((buffer) => {
          if (disposed) return;

          object = parseModel(format, buffer);
          scene.add(object);

          // Lighting: a hemisphere fill plus a key directional light so surfaces
          // read with depth rather than as a flat silhouette.
          scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 1.0));
          const key = new THREE.DirectionalLight(0xffffff, 1.2);
          key.position.set(1, 1, 1);
          scene.add(key);

          renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
          renderer.setPixelRatio(window.devicePixelRatio);
          const rect = container.getBoundingClientRect();
          renderer.setSize(rect.width || 1, rect.height || 1);
          container.appendChild(renderer.domElement);

          controls = new OrbitControls(camera, renderer.domElement);
          controls.enableDamping = true;

          fitToObject(object, camera, controls);

          const render = () => {
            rafId = requestAnimationFrame(render);
            controls?.update();
            renderer?.render(scene, camera);
          };
          render();

          const onResize = () => {
            if (!renderer) return;
            const r = container.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return;
            camera.aspect = r.width / r.height;
            camera.updateProjectionMatrix();
            renderer.setSize(r.width, r.height);
          };
          resizeObserver = new ResizeObserver(onResize);
          resizeObserver.observe(container);
        })
        .catch(() => {
          if (!disposed) setErrored(true);
        });
    } catch {
      setErrored(true);
    }

    return () => {
      disposed = true;
      cancelAnimationFrame(rafId);
      resizeObserver?.disconnect();
      controls?.dispose();
      if (object) {
        disposeObject(object);
        scene.remove(object);
      }
      if (renderer) {
        renderer.domElement.remove();
        renderer.dispose();
        // Force the WebGL context to be released rather than waiting on GC.
        renderer.forceContextLoss();
      }
    };
  }, [data, path]);

  const filename = path.split("/").pop() ?? path;

  const body = errored ? (
    <div className="flex items-center justify-center p-8 text-muted-foreground text-sm">
      {data.truncated
        ? "Model is too large to preview (truncated by the server)."
        : "Unable to render 3D model."}
    </div>
  ) : (
    <div
      ref={containerRef}
      aria-label={`3D preview of ${filename}`}
      className="min-h-0 flex-1 cursor-grab overflow-hidden active:cursor-grabbing"
    />
  );

  if (!data.truncated) return <div className="flex h-full flex-col">{body}</div>;
  return (
    <div className="flex h-full flex-col">
      <TruncatedBanner />
      {body}
    </div>
  );
}
