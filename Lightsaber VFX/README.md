# Blender Lightsaber VFX

A simple, non-destructive lightsaber VFX setup for Blender 5.0+. Add glowing blade objects to your scene and composite them over real video footage — all from a single sidebar panel.

---

## Features

- **Add Lightsabers** — Spawn a fully rigged blade cylinder (with emission material, Stretch To constraint, and glow compositor node group) in one click. Also supports a 3D lightsaber variant.
- **Blade Controls** — Adjust color, brightness, width, glow strength, glow size, and saturation live from the sidebar. Changes are reflected in the viewport and compositor in real time.
- **Presets** — Save and load named blade color/glow presets as JSON files. Presets are stored in Blender's user extension data folder.
- **Video / Background Setup** — Point the addon at a background video or image sequence and it configures the compositor and render settings automatically (resolution, framerate, frame range).
- **Frame Rate Matching** — Choose from common frame rates (23.976 – 120 fps); the addon sets the exact rational numerator/denominator so the render matches your footage.
- **Multi-Lightsaber Support** — Each lightsaber lives in its own collection. An "Active Lightsaber" dropdown lets you switch between them; an Isolate toggle makes the others non-selectable.
- **Mask System** — Create a mask plane (positioned between the camera and scene origin) so the blade passes behind real-world objects. Edit-mode knife instructions are shown inline to guide the cut.
- **Render Workflow** — Step 1 / Step 2 render buttons bake the blade pass and composite pass separately, or use the combined render button to do both in sequence.
- **EXR Output** — Optionally render the blade pass as multi-layer EXR frames to a custom output directory.
- **See-Through Toggle** — Switch between X-ray and rendered viewport shading without leaving the panel.

---

## Installation

1. In Blender 5.0+, open **Edit → Preferences → Get Extensions**.
2. Search for **Blender Saber Addon** and click **Install**, or drag-and-drop the `.zip` onto the Blender window.
3. The panel appears at **View3D → Sidebar (N) → Lightsaber**.

---

## Quick Start

1. Open a new scene and go to the **Lightsaber** tab in the 3D Viewport sidebar.
2. In the **Render** sub-panel, browse to your background video and click **Set Video**. The scene resolution and frame rate will update to match your footage.
3. Click **Add Lightsaber**. A blade cylinder, camera, and compositor node group are created automatically, already set up for your video.
4. Use the **Blade** sub-panel to change the color and glow.
5. Click **Render Both** to produce the final composited output.

---

## Requirements

- Blender **5.0.1** or newer
- No external Python packages required

---

## License

GNU General Public License v3.0 or later — see [https://www.gnu.org/licenses/gpl-3.0.html](https://www.gnu.org/licenses/gpl-3.0.html)
