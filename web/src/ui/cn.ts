/** Join class names, skipping falsy ones. It does not resolve conflicting utilities: a class passed to a ui/ component
 * wins because the component's own styles sit in the components layer (ui.css); elsewhere, do not pass two classes
 * for the same property. */
export function cn(...classes: (string | false | null | undefined)[]) {
  return classes.filter(Boolean).join(" ");
}
