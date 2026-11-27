import * as T from "three";
import type { Floor } from "./catalog";
import { label, mesh, pickable, type Paint, type World } from "./materials.ts";

function catalogMarker(w: World, p: Paint, floor: Floor, darkInk: boolean) {
  const group = new T.Group();
  group.position.set(0, 2.62, 0.45);
  w.root.add(group);
  const crystal = mesh(
    group,
    new T.IcosahedronGeometry(0.33, 1),
    new T.MeshStandardMaterial({
      color: p.color("grey", darkInk ? 700 : 400),
      roughness: 1,
    })
  );
  crystal.rotation.set(0.18, 0.45, -0.12);
  const setLabel = label(group, p, floor.models[0].id, 0, -0.52, 0, 0.52, 1536, darkInk);
  pickable(w, group, floor.key);
  return { group, setLabel };
}

/** Reuse the marker and its label as the visitor browses additional catalog models. */
export function createExhibitSelector(w: World, p: Paint, darkInk = false) {
  let marker: ReturnType<typeof catalogMarker> | undefined;
  return (floor?: Floor) => {
    for (const [key, mark] of w.marks) mark.visible = key === floor?.key;
    if (floor?.sculpture === "Other") {
      marker ??= catalogMarker(w, p, floor, darkInk);
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
