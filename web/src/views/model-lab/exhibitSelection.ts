import * as T from "three";
import type { Floor } from "./catalog";
import { halo, label, mesh, pickable, type Paint, type World } from "./materials.ts";

function catalogMarker(w: World, p: Paint, floor: Floor) {
  const group = new T.Group();
  group.position.set(0, 2.62, 0.45);
  w.root.add(group);
  const crystal = mesh(
    group,
    new T.OctahedronGeometry(0.46),
    new T.MeshStandardMaterial({ color: p.color("primary", 300), metalness: 0.38, roughness: 0.25, flatShading: true })
  );
  crystal.rotation.set(0.18, 0.45, -0.12);
  const rim = mesh(
    group,
    new T.TorusGeometry(0.55, 0.022, 8, 64),
    new T.MeshBasicMaterial({ color: p.color("info", 200) })
  );
  rim.rotation.set(0.92, 0.3, -0.4);
  halo(group, p, p.color("primary", 300), 1.95, 0.42);
  const setLabel = label(group, p, floor.models[0].id, 0, 0.83, 0, 0.66, 1536);
  pickable(w, group, floor.key);
  return { group, setLabel };
}

/** Reuse the marker and its label as the visitor browses additional catalog models. */
export function createExhibitSelector(w: World, p: Paint) {
  let marker: ReturnType<typeof catalogMarker> | undefined;
  return (floor?: Floor) => {
    for (const [key, mark] of w.marks) mark.visible = key === floor?.key;
    if (floor?.sculpture === "Other") {
      marker ??= catalogMarker(w, p, floor);
      if (marker.group.userData.choice !== floor.key) {
        marker.setLabel(floor.models[0].id);
        marker.group.userData.choice = floor.key;
      }
      marker.group.visible = true;
    } else if (marker) {
      marker.group.visible = false;
    }
  };
}
