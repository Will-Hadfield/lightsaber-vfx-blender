bl_info = {
    "name":        "Blender Lightsaber VFX",
    "blender":     (5, 0, 1),
    "version":     (1, 0),
    "category":    "Object",
    "location":    "View3D > Sidebar > Lightsaber",
    "description": "Super simple blender lightsaber VFX setup",
}

# ── Standard library ──────────────────────────────────────────────────────────
# FIX #13: All stdlib imports at top of file (were scattered mid-module before).
import json
import math
import os
import subprocess

# ── Blender ───────────────────────────────────────────────────────────────────
import bmesh
import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, StringProperty)
from collections import defaultdict
from mathutils import Vector


# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Blade cylinder geometry
CYL_X       = 0.114985
CYL_Y       = 0.114985
CYL_Z       = 3.8948
BEVEL_SEGS  = 20
TIP_Z_SCALE = 1.5
EMPTY_SCALE = 0.098

# FIX #15: STRETCH_TO rest length is a separate constant from CYL_Z.
# The bevel + tip-scale operations shift vertex positions before the
# dimension-correction clamp runs, so the effective rest length differs
# slightly from the raw CYL_Z primitive depth. Value is empirically tuned.
STRETCH_REST_LENGTH = 3.89401

# Camera geometry — sits at (0, CAMERA_Y_OFFSET, 0) looking at the world origin.
CAMERA_Y_OFFSET = -15.465

# FIX #15: Mask plane geometry constants (were bare magic numbers before).
# The plane is positioned between the camera and the origin to fill the frustum.
MASK_PLANE_Y_OFFSET = -13.9834   # Y translation along camera -Y axis
MASK_PLANE_SCALE_XY = 0.568035   # Uniform XY scale to match frustum width
MASK_PLANE_SCALE_Z  = 0.586391   # Additional Z scale to match aspect ratio

# FIX #5: Render media-type enum values as named constants.
# The Blender 5.0 media_type enum uses 'MULTI_LAYER' (not 'MULTI_LAYER_IMAGE')
# for multi-layer EXR — matching the value the panel UI checks against.
_MEDIA_TYPE_VIDEO = 'VIDEO'
_MEDIA_TYPE_EXR   = 'MULTI_LAYER_IMAGE'

# FIX #7: FPS lookup table at module level — was rebuilt inside a loop before.
# Maps panel enum key → (fps_numerator, fps_denominator) for exact rational fps.
_FPS_MAP = {
    '23.976': (24000, 1001),
    '24':     (24,    1),
    '25':     (25,    1),
    '29.97':  (30000, 1001),
    '30':     (30,    1),
    '50':     (50,    1),
    '59.94':  (60000, 1001),
    '60':     (60,    1),
    '120':    (120,   1),
}

# Pre-computed float values for closest-match fps detection (avoids repeated division).
_FPS_FLOAT = {k: v[0] / v[1] for k, v in _FPS_MAP.items()}


def _srgb_to_lin(c: float) -> float:
    """Convert an sRGB channel value to linear light."""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


_DEFAULT_COLOR = (
    _srgb_to_lin(0x1E / 255),
    _srgb_to_lin(0xFF / 255),
    _srgb_to_lin(0x3B / 255),
    1.0,
)


# ─────────────────────────────────────────────────────────────────────────────
#  MODULE-LEVEL MUTABLE STATE
# ─────────────────────────────────────────────────────────────────────────────

# FIX #6: Preset name cache — avoids an os.listdir() on every single panel draw.
# The dirty flag is set after any save or delete; cleared after the next read.
_preset_cache: list       = []
_preset_cache_dirty: bool = True


def _invalidate_preset_cache() -> None:
    """Mark the preset cache as stale so it refreshes on next draw."""
    global _preset_cache_dirty
    _preset_cache_dirty = True


# ─────────────────────────────────────────────────────────────────────────────
#  LIGHTSABER REGISTRY
#  Stored as a JSON string on the scene custom property "_blade_ls_list".
#  FIX #3: JSON replaces the old comma-join, preventing corruption if a name
#  ever contained a comma.
# ─────────────────────────────────────────────────────────────────────────────

def _get_ls_list(scene) -> list:
    raw = scene.get("_blade_ls_list", "[]")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _set_ls_list(scene, lst: list) -> None:
    scene["_blade_ls_list"] = json.dumps(lst)


def _next_ls_name(scene) -> str:
    """
    Generate a collision-free lightsaber name.
    FIX #1: Uses a monotonically-increasing index stored on the scene instead
    of len(list)+1. This prevents name collisions if entries are ever deleted
    from the registry (the old code would re-use the same number).
    """
    idx = scene.get("_blade_ls_max_idx", 0) + 1
    scene["_blade_ls_max_idx"] = idx
    return f"Lightsaber {idx}"


def _next_3d_ls_name(scene) -> str:
    """Generate a collision-free 3D lightsaber name."""
    idx = scene.get("_blade_3d_ls_max_idx", 0) + 1
    scene["_blade_3d_ls_max_idx"] = idx
    return f"3D Lightsaber {idx}"


# ─────────────────────────────────────────────────────────────────────────────
#  OBJECT LOOKUP HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _get_active_cyl(context):
    """
    Return the blade cylinder for the currently-active lightsaber, or None.
    FIX #18: Primary lookup is via the cylinder name stored on the collection
    at build time (_cyl_name). This survives user renames of the collection.
    Prefix-scan fallback retained for .blend files built with older versions.
    """
    ls_name = context.scene.blade_active_ls
    if not ls_name or ls_name == 'NONE':
        return None
    coll = bpy.data.collections.get(ls_name)
    if coll is None:
        return None

    # Primary: stored name key written at build time
    stored = coll.get("_cyl_name", "")
    if stored:
        obj = bpy.data.objects.get(stored)
        if obj is not None and obj.name in {o.name for o in coll.objects}:
            return obj

    # Fallback: name-prefix scan (legacy .blend compatibility)
    for obj in coll.objects:
        if obj.name.startswith("Cylinder"):
            return obj
    return None


def _get_emission_node(context):
    """
    Return the Emission shader node for the active blade material, or None.
    FIX #11: context is now a required parameter (no bpy.context fallback).
    bpy.context is unreliable and not thread-safe outside of operator/panel calls.
    """
    cyl = _get_active_cyl(context)
    if cyl is None or not cyl.data.materials:
        return None
    mat = cyl.data.materials[0]
    if mat is None or not mat.use_nodes:
        return None
    return next((n for n in mat.node_tree.nodes if n.type == 'EMISSION'), None)


# ─────────────────────────────────────────────────────────────────────────────
#  COMPOSITOR NODE HELPERS
#  FIX #8: Collapsed the two identical node-traversal functions into one.
# ─────────────────────────────────────────────────────────────────────────────

def _get_composite_node(bl_idname: str):
    """Return the first node matching bl_idname in Lightsaber_Composite, or None."""
    grp = bpy.data.node_groups.get("Lightsaber_Composite")
    if grp is None:
        return None
    return next((n for n in grp.nodes if n.bl_idname == bl_idname), None)


def _get_glare_node():
    return _get_composite_node('CompositorNodeGlare')


def _get_huesat_node():
    return _get_composite_node('CompositorNodeHueSat')


# FIX #9: Extracted shared compositor-node configuration helpers.
# Previously these ~30 lines were copy-pasted between _setup_compositor and
# _setup_compositor_from_exr, causing the two to silently drift out of sync.

def _configure_scale_node(n_scale) -> None:
    """Set Scale node to Render Size mode.
    In Blender 4.x and earlier, CompositorNodeScale exposed a 'space' enum property
    with values like 'RENDER_SIZE', 'RELATIVE', 'ABSOLUTE'. In Blender 5.0 that
    property was removed — the node is now purely socket-driven with no mode selector.
    We attempt the assignment and silently ignore AttributeError so the addon works
    on both generations.
    """
    try:
        n_scale.space = 'RENDER_SIZE'  # Blender 4.x
    except AttributeError:
        n_scale.inputs[1].default_value = 'Render Size'  # Blender 5.0


def _configure_glare_node(n_glare, strength: float = 0.08, size: float = 0.086) -> None:
    """Configure CompositorNodeGlare as a Bloom effect."""
    def _si(name, value):
        if name in n_glare.inputs:
            n_glare.inputs[name].default_value = value

    if 'Type'    in n_glare.inputs: n_glare.inputs['Type'].default_value    = 'Bloom'
    if 'Quality' in n_glare.inputs: n_glare.inputs['Quality'].default_value = 'High'
    _si('Threshold',  1.0)
    _si('Smoothness', 1.0)
    _si('Strength',   strength)
    _si('Saturation', 0.9)
    _si('Size',       size)


def _configure_colorspace_node(n_convert) -> None:
    """Configure ConvertColorSpace node: working space → ACES 2.0 sRGB."""
    for from_name in ('scene_linear', 'Linear Rec.709', 'Linear', 'scene linear'):
        try:
            n_convert.from_color_space = from_name
            break
        except Exception:
            continue
    try:
        n_convert.to_color_space = 'ACES 2.0 sRGB'
    except Exception:
        pass


def _configure_huesat_node(n_huesat, saturation: float = 2.0) -> None:
    """Configure CompositorNodeHueSat with the given saturation and standard defaults."""
    if 'Hue'        in n_huesat.inputs: n_huesat.inputs['Hue'].default_value        = 0.5
    if 'Saturation' in n_huesat.inputs: n_huesat.inputs['Saturation'].default_value = saturation
    if 'Value'      in n_huesat.inputs: n_huesat.inputs['Value'].default_value      = 2.0
    if 'Fac'        in n_huesat.inputs: n_huesat.inputs['Fac'].default_value        = 1.0


# ─────────────────────────────────────────────────────────────────────────────
#  LIVE-UPDATE CALLBACKS
# ─────────────────────────────────────────────────────────────────────────────

def _update_color(self, context):
    node = _get_emission_node(context)
    if node:
        node.inputs[0].default_value = tuple(context.scene.blade_color)


def _update_brightness(self, context):
    node = _get_emission_node(context)
    if node:
        node.inputs[1].default_value = context.scene.blade_brightness


def _update_width(self, context):
    cyl = _get_active_cyl(context)
    if cyl is None:
        return
    # blade_width 5.0 → scale 1.0 (default size). Range 0–10 gives 0–2× scale.
    factor = context.scene.blade_width / 5.0
    cyl.scale.x = factor
    cyl.scale.y = factor


def _update_glow_strength(self, context):
    node = _get_glare_node()
    if node and 'Strength' in node.inputs:
        node.inputs['Strength'].default_value = context.scene.blade_glow_strength


def _update_glow_size(self, context):
    node = _get_glare_node()
    if node and 'Size' in node.inputs:
        node.inputs['Size'].default_value = context.scene.blade_glow_size


def _update_saturation(self, context):
    node = _get_huesat_node()
    if node and 'Saturation' in node.inputs:
        node.inputs['Saturation'].default_value = context.scene.blade_saturation


def _update_active_ls(self, context):
    """
    Sync panel sliders to match the newly-selected lightsaber's actual values.

    IMPORTANT — direct dict writes are intentional:
      scene["prop"] = value  bypasses the RNA update callback for that property.
      Using scene.prop = value would trigger _update_color → write to the emission
      node we just read from, and then _update_active_ls would be called again,
      creating an infinite callback loop. The dict-write pattern breaks that cycle.
    """
    scene = context.scene
    node  = _get_emission_node(context)
    cyl   = _get_active_cyl(context)
    if node:
        col = node.inputs[0].default_value
        scene["blade_color"]      = (col[0], col[1], col[2], col[3])
        scene["blade_brightness"] = node.inputs[1].default_value
    if cyl:
        # Inverse of _update_width: scale → blade_width user value
        scene["blade_width"] = cyl.scale.x * 5.0


def _update_isolate(self, context):
    scene   = context.scene
    active  = scene.blade_active_ls
    isolate = scene.blade_isolate
    for coll in bpy.data.collections:
        if coll.name.startswith("Lightsaber ") and coll.name != active:
            coll.hide_select = isolate
            if not isolate:
                # Restore per-object selectability (Cylinder stays locked always)
                for obj in coll.objects:
                    if not obj.name.startswith("Cylinder"):
                        obj.hide_select = False


def _update_see_through(self, context):
    see_through = context.scene.blade_see_through
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    if see_through:
                        space.shading.type      = 'SOLID'
                        space.shading.show_xray = True
                    else:
                        space.shading.show_xray = False
                        space.shading.type      = 'RENDERED'
            break


# ─────────────────────────────────────────────────────────────────────────────
#  DROPDOWN ITEMS
# ─────────────────────────────────────────────────────────────────────────────

def _ls_items(self, context):
    lst = _get_ls_list(context.scene)
    if not lst:
        return [('NONE', "No lightsabers yet", "")]
    return [(name, name, "") for name in lst]


# ─────────────────────────────────────────────────────────────────────────────
#  COMPOSITOR BUILDERS
#  FIX #9: Both builders now call the shared config helpers above instead of
#  duplicating 30+ lines of node-setup code each.
# ─────────────────────────────────────────────────────────────────────────────

def _remove_existing_composite_group() -> None:
    """Remove the Lightsaber_Composite node group if it exists."""
    old = bpy.data.node_groups.get("Lightsaber_Composite")
    if old:
        bpy.data.node_groups.remove(old)


def _setup_compositor(scene) -> None:
    """
    Build the live compositor tree:
      MovieClip → Scale → AlphaOver (background)
      RLayers → Glare → ColorConvert → HueSat → AlphaOver (foreground)
    """
    _remove_existing_composite_group()

    grp       = bpy.data.node_groups.new("Lightsaber_Composite", 'CompositorNodeTree')
    grp_nodes = grp.nodes
    grp_links = grp.links

    grp.interface.new_socket("Image", in_out='OUTPUT', socket_type='NodeSocketColor')

    n_clip    = grp_nodes.new('CompositorNodeMovieClip')
    n_scale   = grp_nodes.new('CompositorNodeScale')
    n_render  = grp_nodes.new('CompositorNodeRLayers')
    n_glare   = grp_nodes.new('CompositorNodeGlare')
    n_convert = grp_nodes.new('CompositorNodeConvertColorSpace')
    n_huesat  = grp_nodes.new('CompositorNodeHueSat')
    n_alpha   = grp_nodes.new('CompositorNodeAlphaOver')
    n_viewer  = grp_nodes.new('CompositorNodeViewer')
    n_out     = grp_nodes.new('NodeGroupOutput')

    n_clip.location    = (-700,  300)
    n_scale.location   = (-400,  300)
    n_render.location  = (-700, -100)
    n_glare.location   = (-200, -100)
    n_convert.location = (  50, -100)
    n_huesat.location  = ( 300, -100)
    n_alpha.location   = ( 550,  100)
    n_viewer.location  = ( 900, -150)
    n_out.location     = ( 900,  100)

    _configure_scale_node(n_scale)
    _configure_glare_node(n_glare)
    _configure_colorspace_node(n_convert)
    _configure_huesat_node(n_huesat)

    grp_links.new(n_clip.outputs['Image'],    n_scale.inputs['Image'])
    grp_links.new(n_scale.outputs['Image'],   n_alpha.inputs['Background'])
    grp_links.new(n_render.outputs['Image'],  n_glare.inputs['Image'])
    grp_links.new(n_glare.outputs['Image'],   n_convert.inputs['Image'])
    grp_links.new(n_convert.outputs['Image'], n_huesat.inputs['Image'])
    grp_links.new(n_huesat.outputs['Image'],  n_alpha.inputs['Foreground'])
    grp_links.new(n_huesat.outputs['Image'],  n_viewer.inputs['Image'])
    grp_links.new(n_alpha.outputs['Image'],   n_out.inputs[0])

    grp['_clip_node_name']       = n_clip.name
    scene.compositing_node_group = grp


def _setup_compositor_from_exr(scene, exr_folder: str,
                                frame_start: int, frame_end: int) -> None:
    """
    Rebuild compositor feeding the glow chain from a pre-rendered EXR image
    sequence instead of a live Render Layers node.
    FIX #9: Glare/HueSat/ColorConvert config now uses shared helpers; values
    are correctly restored from the backed-up scene properties so the composite
    matches exactly what was set in the panel before Step 1 ran.
    """
    _remove_existing_composite_group()

    grp       = bpy.data.node_groups.new("Lightsaber_Composite", 'CompositorNodeTree')
    grp_nodes = grp.nodes
    grp_links = grp.links

    grp.interface.new_socket("Image", in_out='OUTPUT', socket_type='NodeSocketColor')

    n_clip    = grp_nodes.new('CompositorNodeMovieClip')
    n_scale   = grp_nodes.new('CompositorNodeScale')
    n_img     = grp_nodes.new('CompositorNodeImage')   # EXR sequence instead of RLayers
    n_glare   = grp_nodes.new('CompositorNodeGlare')
    n_convert = grp_nodes.new('CompositorNodeConvertColorSpace')
    n_huesat  = grp_nodes.new('CompositorNodeHueSat')
    n_alpha   = grp_nodes.new('CompositorNodeAlphaOver')
    n_viewer  = grp_nodes.new('CompositorNodeViewer')
    n_out     = grp_nodes.new('NodeGroupOutput')

    n_clip.location    = (-700,  300)
    n_scale.location   = (-400,  300)
    n_img.location     = (-700, -100)
    n_glare.location   = (-200, -100)
    n_convert.location = (  50, -100)
    n_huesat.location  = ( 300, -100)
    n_alpha.location   = ( 550,  100)
    n_viewer.location  = ( 900, -150)
    n_out.location     = ( 900,  100)

    _configure_scale_node(n_scale)
    # Restore the glow/saturation values the user set before Step 1 ran
    _configure_glare_node(
        n_glare,
        strength = scene.get('_blade_glow_strength_bak', 0.08),
        size     = scene.get('_blade_glow_size_bak',     0.086),
    )
    _configure_colorspace_node(n_convert)
    _configure_huesat_node(n_huesat, saturation=scene.get('_blade_saturation_bak', 2.0))

    # ── Load the EXR image sequence ───────────────────────────────────────
    # Remove any stale data-block from a previous Step 1 run so we start clean.
    old_img = bpy.data.images.get('Blade_EXR_Sequence')
    if old_img:
        bpy.data.images.remove(old_img)

    exr_files = sorted(f for f in os.listdir(exr_folder) if f.endswith('.exr'))
    if not exr_files:
        raise RuntimeError(f"No EXR files found in {exr_folder}")

    first_exr       = os.path.join(exr_folder, exr_files[0])
    img_data        = bpy.data.images.load(first_exr)
    img_data.source = 'SEQUENCE'
    img_data.name   = 'Blade_EXR_Sequence'
    n_img.image     = img_data

    # Set frame timing directly on the node (Blender 4.x+ API).
    # CompositorNodeImage does NOT have an image_user — properties live on the
    # node itself. Setting these is what makes Blender actually step through the
    # sequence frame-by-frame instead of showing only the first frame.
    n_img.frame_duration   = frame_end - frame_start + 1
    n_img.frame_start      = frame_start
    n_img.frame_offset     = frame_start - 1
    n_img.use_cyclic       = False
    n_img.use_auto_refresh = True

    # For multilayer EXR the node exposes per-pass outputs once the image is
    # assigned. Try to route the Emit pass — that's the pure blade emission data
    # with no environment contribution, giving the cleanest possible glow input.
    # Fall back to outputs[0] (Combined) if the pass name isn't found yet.
    emit_sock = None
    for sock in n_img.outputs:
        if 'emit' in sock.name.lower():
            emit_sock = sock
            break
    exr_src = emit_sock if emit_sock else n_img.outputs[0]

    grp_links.new(n_clip.outputs['Image'],    n_scale.inputs['Image'])
    grp_links.new(n_scale.outputs['Image'],   n_alpha.inputs['Background'])
    grp_links.new(exr_src,                    n_glare.inputs['Image'])
    grp_links.new(n_glare.outputs['Image'],   n_convert.inputs['Image'])
    grp_links.new(n_convert.outputs['Image'], n_huesat.inputs['Image'])
    grp_links.new(n_huesat.outputs['Image'],  n_alpha.inputs['Foreground'])
    grp_links.new(n_huesat.outputs['Image'],  n_viewer.inputs['Image'])
    grp_links.new(n_alpha.outputs['Image'],   n_out.inputs[0])

    grp['_clip_node_name']       = n_clip.name
    scene.compositing_node_group = grp

    # Re-wire the clip if one was already loaded before Step 1
    old_clip_name = scene.get('_blade_clip_name_bak', '')
    if old_clip_name and old_clip_name in bpy.data.movieclips:
        clip_node = grp_nodes.get(grp['_clip_node_name'])
        if clip_node:
            clip_node.clip = bpy.data.movieclips[old_clip_name]


# ─────────────────────────────────────────────────────────────────────────────
#  VIDEO / FRAMERATE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _detect_fps(filepath: str):
    """
    Try to detect framerate from a video file via ffprobe.
    Returns (fps_numerator, fps_denominator) on success, or None on failure.
    """
    try:
        cmd    = ['ffprobe', '-v', 'quiet', '-print_format', 'json',
                  '-show_streams', filepath]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for stream in data.get('streams', []):
                if stream.get('codec_type') == 'video':
                    r = stream.get('r_frame_rate', '')
                    if '/' in r:
                        num, den = r.split('/')
                        return (int(num), int(den))
    except Exception:
        pass
    return None


def _apply_fps_from_clip(scene, clip) -> None:
    """Auto-detect and apply the framerate from a loaded movie clip."""
    filepath = bpy.path.abspath(clip.filepath)
    result   = _detect_fps(filepath)
    if result:
        num, den = result
        scene.render.fps      = num
        scene.render.fps_base = den
        # FIX #7: Uses pre-built _FPS_FLOAT dict and min() — no per-key dict rebuild.
        fps_val = num / den
        closest = min(_FPS_FLOAT, key=lambda k: abs(_FPS_FLOAT[k] - fps_val))
        scene["blade_framerate"] = closest
    else:
        # Fallback: Blender's own clip.fps attribute
        try:
            fps = clip.fps
            if fps and fps > 0:
                scene.render.fps      = round(fps * 1000)
                scene.render.fps_base = 1000
        except Exception:
            pass


def _apply_video(context, filepath: str) -> None:
    """Load a video/image and wire it into the compositor and camera background."""
    scene = context.scene
    if not filepath or not os.path.isfile(filepath):
        return

    abs_fp = bpy.path.abspath(filepath)
    clip   = next(
        (c for c in bpy.data.movieclips if bpy.path.abspath(c.filepath) == abs_fp),
        None,
    )
    if clip is None:
        clip = bpy.data.movieclips.load(filepath)

    scene['_blade_clip_name_bak'] = clip.name
    scene.frame_start             = 1
    scene.frame_end               = clip.frame_duration

    _apply_fps_from_clip(scene, clip)

    grp = bpy.data.node_groups.get("Lightsaber_Composite")
    if grp:
        node = grp.nodes.get(grp.get('_clip_node_name', ''))
        if node and node.bl_idname == 'CompositorNodeMovieClip':
            node.clip = clip

    cam_obj = scene.camera
    if cam_obj and cam_obj.type == 'CAMERA':
        cam = cam_obj.data
        cam.show_background_images = True
        bg  = next((b for b in cam.background_images if b.source == 'MOVIE_CLIP'), None)
        if bg is None:
            bg = cam.background_images.new()
        bg.source        = 'MOVIE_CLIP'
        bg.clip          = clip
        bg.display_depth = 'BACK'
        bg.alpha         = 1.0


def _update_video_path(self, context):
    _apply_video(context, context.scene.blade_video_path)


def _update_framerate(self, context):
    entry = _FPS_MAP.get(context.scene.blade_framerate)
    if entry:
        context.scene.render.fps      = entry[0]
        context.scene.render.fps_base = entry[1]


# ─────────────────────────────────────────────────────────────────────────────
#  MASK COLLECTION HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _masks_collection(scene):
    """Return (creating if needed) the 'Masks' collection linked to the scene."""
    coll = bpy.data.collections.get("Masks")
    if coll is None:
        coll = bpy.data.collections.new("Masks")
        scene.collection.children.link(coll)
    return coll


# ─────────────────────────────────────────────────────────────────────────────
#  RENDER HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _blade_exr_dir(scene=None) -> str:
    """Return the directory used for the EXR blade-render sequence.

    Resolution order:
      1. If a custom directory has been set (blade_exr_dir scene prop) AND that
         path exists on disk, use it — frames land directly inside it.
      2. Otherwise fall back to the fixed Blade_Frames folder inside Blender's
         temp directory.

    Always pass `scene` explicitly (context.scene) so this is safe inside
    operators, modal timers, and poll() callbacks where bpy.context may be stale.
    Falls back to bpy.context.scene only if scene is not provided (panel draws).
    """
    if scene is None:
        try:
            scene = bpy.context.scene
        except Exception:
            scene = None

    if scene is not None:
        custom = scene.get("blade_exr_dir", "").strip()
        if not custom:
            # Also check the RNA property (set via the UI)
            try:
                custom = scene.blade_exr_dir.strip()
            except Exception:
                custom = ""
        if custom:
            expanded = bpy.path.abspath(custom)
            if os.path.isdir(expanded):
                return expanded

    return os.path.join(bpy.app.tempdir, "Blade_Frames")



# ─────────────────────────────────────────────────────────────────────────────
#  BUILD SUB-FUNCTIONS
#  FIX #12: build() decomposed into clearly-scoped helpers — one responsibility
#  each, individually debuggable and readable.
#  FIX #10: _deselect / _activate are module-level (were re-created per build() call).
# ─────────────────────────────────────────────────────────────────────────────

def _deselect() -> None:
    """Deselect all objects."""
    bpy.ops.object.select_all(action='DESELECT')


def _activate(obj, vl) -> None:
    """Deselect all, then select and activate a single object."""
    _deselect()
    obj.select_set(True)
    vl.objects.active = obj


def _unlink_from_all_collections(obj) -> None:
    """Remove an object from every collection it currently belongs to."""
    for coll in list(obj.users_collection):
        coll.objects.unlink(obj)


def _build_blade_mesh(vl, ls_coll, ls_name: str, color: tuple, brightness: float):
    """
    Create the blade cylinder:
      - Add primitive
      - Bevel top rim to form the tapered tip
      - Shade smooth
      - Assign Blade / Tip vertex groups
      - Create and assign emission material
    Returns (cylinder_obj, taper_z).
    """
    bpy.ops.mesh.primitive_cylinder_add(
        vertices       = 100,
        radius         = CYL_X / 2,
        depth          = CYL_Z,
        enter_editmode = False,
        align          = 'WORLD',
        location       = (0, 0, 0),
    )
    cyl      = bpy.context.active_object
    cyl.name = f"Cylinder_{ls_name}"
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    # ── Bevel the top rim to create the blade tip ──────────────────────────
    bpy.ops.object.editmode_toggle()
    bpy.ops.mesh.select_mode(use_extend=False, use_expand=False, type='EDGE')
    bpy.ops.mesh.select_all(action='DESELECT')

    bm = bmesh.from_edit_mesh(cyl.data)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    max_z = max(v.co.z for v in bm.verts)

    for e in bm.edges:
        v0, v1 = e.verts
        at_top = (abs(v0.co.z - max_z) < 1e-5 and abs(v1.co.z - max_z) < 1e-5)
        on_rim = (math.sqrt(v0.co.x**2 + v0.co.y**2) > 1e-5 and
                  math.sqrt(v1.co.x**2 + v1.co.y**2) > 1e-5)
        e.select = at_top and on_rim

    bmesh.update_edit_mesh(cyl.data, loop_triangles=False, destructive=False)
    bpy.ops.mesh.bevel(offset=CYL_X / 2, offset_pct=0, segments=BEVEL_SEGS, affect='EDGES')
    bpy.ops.transform.resize(
        value=(1, 1, TIP_Z_SCALE), orient_type='GLOBAL',
        constraint_axis=(False, False, True),
    )
    bpy.ops.object.editmode_toggle()

    # Clamp total Z back to CYL_Z (bevel can shift it slightly)
    if abs(cyl.dimensions.z - CYL_Z) > 1e-4:
        cyl.scale.z *= CYL_Z / cyl.dimensions.z
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

    _activate(cyl, vl)
    bpy.ops.object.shade_smooth()
    try:
        bpy.ops.object.shade_auto_smooth()
    except Exception:
        pass

    # ── Vertex groups: Blade (cylindrical body) vs Tip (bevelled cap) ─────
    mesh  = cyl.data
    max_r = max(math.sqrt(v.co.x**2 + v.co.y**2) for v in mesh.vertices)
    z_groups = defaultdict(list)
    for v in mesh.vertices:
        z_groups[round(v.co.z, 5)].append(v)

    taper_z = -CYL_Z / 2
    for z in sorted(z_groups.keys()):
        ring_r = max(math.sqrt(v.co.x**2 + v.co.y**2) for v in z_groups[z])
        if ring_r >= max_r * 0.995:
            taper_z = z

    for grp_name in ("Blade", "Tip"):
        if grp_name in cyl.vertex_groups:
            cyl.vertex_groups.remove(cyl.vertex_groups[grp_name])
    vg_blade = cyl.vertex_groups.new(name="Blade")
    vg_tip   = cyl.vertex_groups.new(name="Tip")
    vg_blade.add([v.index for v in mesh.vertices if v.co.z <= taper_z + 1e-4], 1.0, 'REPLACE')
    vg_tip.add(  [v.index for v in mesh.vertices if v.co.z  > taper_z + 1e-4], 1.0, 'REPLACE')

    # ── Emission material ──────────────────────────────────────────────────
    mat = bpy.data.materials.new("BladeMat")
    mat.use_nodes = True
    mat.node_tree.nodes.clear()
    em  = mat.node_tree.nodes.new('ShaderNodeEmission')
    out = mat.node_tree.nodes.new('ShaderNodeOutputMaterial')
    em.inputs[0].default_value = color
    em.inputs[1].default_value = brightness
    mat.node_tree.links.new(em.outputs[0], out.inputs[0])
    cyl.data.materials.append(mat)

    _unlink_from_all_collections(cyl)
    ls_coll.objects.link(cyl)
    # NOTE: cyl.hide_select is set AFTER parenting in build() so parent_set can select it

    return cyl, taper_z


def _build_armature(vl, ls_coll, ls_name: str, cyl, taper_z: float):
    """
    Create a two-bone (Blade + Tip) armature, attach it to the cylinder via an
    Armature modifier. Returns the armature object.
    """
    _deselect()
    bpy.ops.object.armature_add(enter_editmode=False, align='WORLD', location=(0, 0, 0))
    arm      = bpy.context.active_object
    arm.name = f"Armature_{ls_name}"
    arm.data.display_type = 'BBONE'
    arm.show_in_front     = True

    bpy.ops.object.editmode_toggle()
    eb0               = arm.data.edit_bones[0]
    eb0.name          = "Blade"
    eb0.head          = Vector((0, 0, -CYL_Z / 2))
    eb0.tail          = Vector((0, 0, taper_z))
    eb0.inherit_scale = 'NONE'
    ebt               = arm.data.edit_bones.new("Tip")
    ebt.parent        = eb0
    ebt.use_connect   = True
    ebt.head          = eb0.tail.copy()
    ebt.tail          = Vector((0, 0, CYL_Z / 2))
    ebt.inherit_scale = 'NONE'
    bpy.ops.object.editmode_toggle()

    arm_mod                   = cyl.modifiers.new("Armature", type='ARMATURE')
    arm_mod.object            = arm
    arm_mod.use_vertex_groups = True

    _unlink_from_all_collections(arm)
    ls_coll.objects.link(arm)
    return arm


def _build_target_empty(ls_coll, ls_name: str, cyl):
    """
    Create Blade_Target sphere empty at the top of the cylinder.
    This is the STRETCH_TO target — move it to retract/extend the blade.
    Returns the empty object.
    """
    top_z       = max(v.co.z for v in cyl.data.vertices)
    bpy.ops.object.empty_add(type='SPHERE', align='WORLD', location=(0, 0, top_z))
    e_top       = bpy.context.active_object
    e_top.name  = f"Blade_Target_{ls_name}"
    e_top.scale = (EMPTY_SCALE,) * 3
    _unlink_from_all_collections(e_top)
    ls_coll.objects.link(e_top)
    return e_top


def _build_stretch_constraint(arm, e_top) -> None:
    """Add a STRETCH_TO constraint on the Blade bone pointing at the target empty."""
    _activate(arm, bpy.context.view_layer)
    bpy.ops.object.posemode_toggle()
    pb                = arm.pose.bones["Blade"]
    pb.color.palette  = 'THEME01'
    pb.ik_stiffness_x = 0.99
    pb.ik_stiffness_z = 0.99
    sc                = pb.constraints.new('STRETCH_TO')
    sc.target         = e_top
    sc.rest_length    = STRETCH_REST_LENGTH
    sc.bulge          = 0.0
    sc.use_bulge_min  = True
    sc.bulge_min      = 1.0
    sc.volume         = 'NO_VOLUME'
    sc.keep_axis      = 'SWING_Y'
    sc.influence      = 1.0
    bpy.ops.object.posemode_toggle()


def _build_root_empty(ls_coll, ls_name: str, cyl):
    """
    Create Blade_Root sphere empty at the bottom of the cylinder.
    The cylinder and armature are parented here; moving this empty moves the rig.
    Returns the empty object.
    """
    bot_z                    = min(v.co.z for v in cyl.data.vertices)
    bpy.ops.object.empty_add(type='SPHERE', align='WORLD', location=(0, 0, bot_z))
    e_bot                    = bpy.context.active_object
    e_bot.name               = f"Blade_Root_{ls_name}"
    e_bot.scale              = (EMPTY_SCALE,) * 3
    e_bot.empty_display_size = 1.0
    _unlink_from_all_collections(e_bot)
    ls_coll.objects.link(e_bot)
    return e_bot


def _parent_to_root(vl, cyl, arm, e_bot) -> None:
    """Parent the cylinder and armature to the root empty, keeping world transforms."""
    _deselect()
    cyl.select_set(True)
    arm.select_set(True)
    vl.objects.active = e_bot
    bpy.ops.object.parent_set(type='OBJECT', keep_transform=True)
    _deselect()


def _setup_viewport_compositor(context) -> None:
    """Switch all VIEW_3D areas to Rendered shading with compositor overlay."""
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.shading.use_compositor = 'CAMERA'
                    space.shading.type           = 'RENDERED'
            break


def _setup_camera(scene, vl) -> None:
    """Add Lightsaber_Camera if none exists, position it, and enter Camera view."""
    if scene.camera is not None and scene.camera.name == "Lightsaber_Camera":
        return  # Already set up on a previous Add Lightsaber call
    _deselect()
    bpy.ops.object.camera_add(
        enter_editmode = False,
        align          = 'VIEW',
        location       = (0, 0, 0),
        rotation       = (1.5708, -0, -0),
        scale          = (1, 1, 1),
    )
    cam_obj      = bpy.context.active_object
    cam_obj.name = "Lightsaber_Camera"
    bpy.ops.transform.translate(
        value           = (0, CAMERA_Y_OFFSET, 0),
        orient_type     = 'GLOBAL',
        constraint_axis = (False, True, False),
    )
    cam_obj.hide_select = True
    scene.camera        = cam_obj
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            for space in area.spaces:
                if space.type == 'VIEW_3D':
                    space.region_3d.view_perspective = 'CAMERA'
            break


def _setup_outliner_select_filter() -> None:
    """Show the Select restrict column in all OUTLINER areas."""
    for area in bpy.context.screen.areas:
        if area.type == 'OUTLINER':
            for space in area.spaces:
                if space.type == 'OUTLINER':
                    space.show_restrict_column_select = True
            break


def _hide_layer_coll(layer_coll, target_name: str) -> bool:
    """
    Recursively find and hide a layer collection by name.
    Returns True if found.
    FIX #2: Was re-defined inside a for-loop in Step 2 execute() — now module-level.
    """
    if layer_coll.name == target_name:
        layer_coll.hide_viewport = True
        return True
    for child in layer_coll.children:
        if _hide_layer_coll(child, target_name):
            return True
    return False


def _show_layer_coll(layer_coll, target_name: str) -> bool:
    """
    Recursively find and UN-hide a layer collection by name.
    Mirror of _hide_layer_coll — used by Reset Render to restore visibility.
    Returns True if found.
    """
    if layer_coll.name == target_name:
        layer_coll.hide_viewport = False
        return True
    for child in layer_coll.children:
        if _show_layer_coll(child, target_name):
            return True
    return False


def build(color: tuple, brightness: float) -> None:
    """
    Orchestrate full lightsaber rig construction:
      1.  Collection
      2.  Blade cylinder mesh
      3.  Armature (Blade + Tip bones)
      4.  Blade_Target empty + STRETCH_TO constraint
      5.  Blade_Root empty (parent anchor)
      6.  Parent cylinder + armature → root; lock cylinder selectability
      7.  Compositor (first lightsaber only)
      8.  Render engine + viewport shading
      9.  Camera (first lightsaber only)
     10.  Outliner select filter
     11.  Register in scene list + set active
    """
    scene   = bpy.context.scene
    vl      = bpy.context.view_layer
    ls_name = _next_ls_name(scene)

    # ── 1. Collection ─────────────────────────────────────────────────────
    ls_coll = bpy.data.collections.new(ls_name)
    scene.collection.children.link(ls_coll)

    # ── 2. Blade cylinder ─────────────────────────────────────────────────
    cyl, taper_z = _build_blade_mesh(vl, ls_coll, ls_name, color, brightness)
    # FIX #18: Store the cylinder's object name on the collection so lookup
    # works even if the user renames the object later.
    ls_coll["_cyl_name"] = cyl.name

    # ── 3. Armature ───────────────────────────────────────────────────────
    arm = _build_armature(vl, ls_coll, ls_name, cyl, taper_z)

    # ── 4. Blade_Target empty + STRETCH_TO constraint ─────────────────────
    e_top = _build_target_empty(ls_coll, ls_name, cyl)
    _build_stretch_constraint(arm, e_top)

    # ── 5. Blade_Root empty ───────────────────────────────────────────────
    e_bot = _build_root_empty(ls_coll, ls_name, cyl)

    # ── 6. Parent + lock ──────────────────────────────────────────────────
    # cyl.hide_select must remain False during parent_set so it can be selected.
    _parent_to_root(vl, cyl, arm, e_bot)
    cyl.hide_select = True
    arm.hide_set(True)

    # ── 7. Compositor (first lightsaber only) ─────────────────────────────
    existing_grp = getattr(scene, 'compositing_node_group', None)
    if existing_grp is None or existing_grp.name != "Lightsaber_Composite":
        _setup_compositor(scene)

    # ── 8. Render engine + viewport ───────────────────────────────────────
    scene.render.engine = 'BLENDER_EEVEE'
    _setup_viewport_compositor(bpy.context)

    # ── 9. Camera (first lightsaber only) ─────────────────────────────────
    _setup_camera(scene, vl)

    # ── 10. Outliner filter ───────────────────────────────────────────────
    _setup_outliner_select_filter()

    # ── 11. Register + set active ─────────────────────────────────────────
    lst = _get_ls_list(scene)
    lst.append(ls_name)
    _set_ls_list(scene, lst)
    scene.blade_active_ls = ls_name

    _activate(cyl, vl)
    print(f"[ProSabers] Built '{ls_name}'")


def build_3d(color: tuple, brightness: float) -> None:
    """
    Build a full lightsaber rig identical to build() but named '3D Lightsaber N',
    then appends two half-spheres and a Shrinkwrap constraint on the top empty
    so it snaps to the Front Half sphere surface.
    """
    scene   = bpy.context.scene
    vl      = bpy.context.view_layer
    ls_name = _next_3d_ls_name(scene)

    # ── 1. Collection ─────────────────────────────────────────────────────
    ls_coll = bpy.data.collections.new(ls_name)
    scene.collection.children.link(ls_coll)

    # ── 2. Blade cylinder ─────────────────────────────────────────────────
    cyl, taper_z = _build_blade_mesh(vl, ls_coll, ls_name, color, brightness)
    ls_coll["_cyl_name"] = cyl.name

    # ── 3. Armature ───────────────────────────────────────────────────────
    arm = _build_armature(vl, ls_coll, ls_name, cyl, taper_z)

    # ── 4. Blade_Target empty + STRETCH_TO constraint ─────────────────────
    e_top = _build_target_empty(ls_coll, ls_name, cyl)
    _build_stretch_constraint(arm, e_top)

    # ── 5. Blade_Root empty ───────────────────────────────────────────────
    e_bot = _build_root_empty(ls_coll, ls_name, cyl)

    # ── 6. Parent + lock ──────────────────────────────────────────────────
    _parent_to_root(vl, cyl, arm, e_bot)
    cyl.hide_select = True
    arm.hide_set(True)

    # ── 7. Compositor ─────────────────────────────────────────────────────
    existing_grp = getattr(scene, 'compositing_node_group', None)
    if existing_grp is None or existing_grp.name != "Lightsaber_Composite":
        _setup_compositor(scene)

    # ── 8. Render engine + viewport ───────────────────────────────────────
    scene.render.engine = 'BLENDER_EEVEE'
    _setup_viewport_compositor(bpy.context)

    # ── 9. Camera ─────────────────────────────────────────────────────────
    _setup_camera(scene, vl)

    # ── 10. Outliner filter ───────────────────────────────────────────────
    _setup_outliner_select_filter()

    # ── 11. Register + set active ─────────────────────────────────────────
    lst = _get_ls_list(scene)
    lst.append(ls_name)
    _set_ls_list(scene, lst)
    scene.blade_active_ls = ls_name

    # ── 12. Half spheres ──────────────────────────────────────────────────
    bot_z = e_bot.location.z

    _deselect()
    bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
    sphere_obj = bpy.context.active_object

    mod = sphere_obj.modifiers.new(name="Subd", type='SUBSURF')
    mod.levels = 6
    mod.render_levels = 6
    bpy.ops.object.modifier_apply(modifier=mod.name)

    bpy.ops.object.editmode_toggle()
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.transform.tosphere(value=1.0)
    bpy.ops.object.editmode_toggle()

    sphere_obj.dimensions = (CYL_Z * 2, CYL_Z * 2, CYL_Z * 2)
    bpy.ops.object.transform_apply(scale=True)
    sphere_obj.location = (0, 0, bot_z)
    bpy.ops.object.transform_apply(location=True)

    bm = bmesh.new()
    bm.from_mesh(sphere_obj.data)
    faces_to_delete = [f for f in bm.faces if f.calc_center_median().y < 0]
    bmesh.ops.delete(bm, geom=faces_to_delete, context='FACES')
    bm.to_mesh(sphere_obj.data)
    bm.free()
    sphere_obj.data.update()

    sphere_obj.name = f"Back Half_{ls_name}"
    sphere_obj.hide_render    = True
    sphere_obj.visible_camera = False
    _unlink_from_all_collections(sphere_obj)
    ls_coll.objects.link(sphere_obj)

    _deselect()
    sphere_obj.select_set(True)
    vl.objects.active = sphere_obj
    bpy.ops.object.duplicate()
    front_half = bpy.context.active_object
    front_half.name = f"Front Half_{ls_name}"
    front_half.rotation_euler[2] = math.pi
    bpy.ops.object.transform_apply(rotation=True)
    front_half.hide_render    = True
    front_half.visible_camera = False
    _unlink_from_all_collections(front_half)
    ls_coll.objects.link(front_half)

    # ── 13. Parent both half-spheres and tip empty to the root (bottom) empty ──
    _deselect()
    sphere_obj.select_set(True)
    front_half.select_set(True)
    e_top.select_set(True)
    vl.objects.active = e_bot
    bpy.ops.object.parent_set(type='OBJECT', keep_transform=True)
    _deselect()

    # Now lock selectability after parenting is done
    sphere_obj.hide_select = True
    front_half.hide_select = True

    # ── 14. Shrinkwrap constraint on Blade_Target → Front Half ────────────
    sw                 = e_top.constraints.new('SHRINKWRAP')
    sw.target          = front_half
    sw.distance        = 0.0
    sw.shrinkwrap_type = 'NEAREST_SURFACE'
    sw.wrap_mode       = 'ON_SURFACE'
    sw.influence       = 1.0

    _activate(cyl, vl)
    print(f"[ProSabers] Built 3D rig '{ls_name}'")


# ─────────────────────────────────────────────────────────────────────────────
#  PRESET SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

def _presets_dir() -> str:
    """Return (and create if needed) the lightsaber presets folder."""
    # FIX #14: Removed redundant `import bpy` — already imported at module level.
    base   = bpy.utils.user_resource('SCRIPTS', path="presets")
    folder = os.path.join(base, "lightsaber_presets")
    os.makedirs(folder, exist_ok=True)
    return folder


def _list_presets_from_disk() -> list:
    """Read preset names from disk. Only called when cache is dirty."""
    folder = _presets_dir()
    return [f[:-5] for f in sorted(os.listdir(folder)) if f.endswith(".json")]


def _get_presets() -> list:
    """FIX #6: Return cached preset list; refresh from disk only when dirty."""
    global _preset_cache, _preset_cache_dirty
    if _preset_cache_dirty:
        _preset_cache       = _list_presets_from_disk()
        _preset_cache_dirty = False
    return _preset_cache


def _preset_items(self, context):
    names = _get_presets()
    if not names:
        return [('NONE', "No presets yet", "")]
    return [(n, n, "") for n in names]


def _save_preset(name: str, scene) -> None:
    safe = "".join(c for c in name if c not in r'\/:*?"<>|').strip()
    path = os.path.join(_presets_dir(), safe + ".json")
    data = {
        "blade_color":         list(scene.blade_color),
        "blade_brightness":    scene.blade_brightness,
        "blade_saturation":    scene.blade_saturation,
        "blade_glow_strength": scene.blade_glow_strength,
        "blade_glow_size":     scene.blade_glow_size,
    }
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    _invalidate_preset_cache()


def _load_preset(name: str, scene) -> bool:
    path = os.path.join(_presets_dir(), name + ".json")
    if not os.path.isfile(path):
        return False
    with open(path) as f:
        data = json.load(f)
    if "blade_color" in data:
        c = data["blade_color"]
        scene.blade_color = (c[0], c[1], c[2], c[3] if len(c) > 3 else 1.0)
    for key in ("blade_brightness", "blade_saturation", "blade_glow_strength", "blade_glow_size"):
        if key in data:
            setattr(scene, key, data[key])
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  OPERATORS
#  FIX #16: All operators now declare poll() to gray themselves out when
#  preconditions aren't met — making invalid states literally unclickable.
#  FIX #24/#25: Step 1 and Step 2 require a confirmation click before rendering.
# ─────────────────────────────────────────────────────────────────────────────

class BLADE_OT_save_preset(bpy.types.Operator):
    bl_idname      = "blade.save_preset"
    bl_label       = "Save Preset"
    bl_description = "Save current Blade Material and Glow settings as a named preset"

    @classmethod
    def poll(cls, context):
        return bool(context.scene.blade_preset_name.strip())

    def execute(self, context):
        name = context.scene.blade_preset_name.strip()
        _save_preset(name, context.scene)
        context.scene.blade_active_preset = name
        self.report({'INFO'}, f"Preset '{name}' saved")
        return {'FINISHED'}


class BLADE_OT_load_preset(bpy.types.Operator):
    bl_idname      = "blade.load_preset"
    bl_label       = "Load Preset"
    bl_description = "Apply the selected preset to the active lightsaber"

    @classmethod
    def poll(cls, context):
        return context.scene.blade_active_preset not in ('', 'NONE')

    def execute(self, context):
        name = context.scene.blade_active_preset
        if _load_preset(name, context.scene):
            self.report({'INFO'}, f"Preset '{name}' loaded")
        else:
            self.report({'ERROR'}, f"Could not load preset '{name}'")
            return {'CANCELLED'}
        return {'FINISHED'}


class BLADE_OT_confirm_delete_preset(bpy.types.Operator):
    bl_idname      = "blade.confirm_delete_preset"
    bl_label       = "Delete Preset?"
    bl_description = "Permanently delete the selected preset"
    bl_options     = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return context.scene.blade_active_preset not in ('', 'NONE')

    def execute(self, context):
        name = context.scene.blade_active_preset
        path = os.path.join(_presets_dir(), name + ".json")
        if os.path.isfile(path):
            os.remove(path)
            _invalidate_preset_cache()
            self.report({'INFO'}, f"Preset '{name}' deleted")
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def draw(self, context):
        self.layout.label(
            text=f"Delete preset '{context.scene.blade_active_preset}'? This cannot be undone."
        )


class BLADE_OT_browse_media(bpy.types.Operator):
    bl_idname      = "blade.browse_media"
    bl_label       = "Import"
    bl_description = "Select a background video or image file"

    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(
        default = ("*.mp4;*.mov;*.avi;*.mkv;*.webm;*.mxf;*.m4v;*.mpg;*.mpeg;"
                   "*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.exr;*.hdr;*.bmp"),
        options = {'HIDDEN'},
    )

    def execute(self, context):
        context.scene.blade_video_path = self.filepath
        _apply_video(context, self.filepath)
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class BLADE_OT_add_lightsaber(bpy.types.Operator):
    bl_idname      = "blade.add_lightsaber"
    bl_label       = "Add Lightsaber"
    bl_description = "Build a new lightsaber blade rig in its own collection"
    bl_options     = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            build(
                color      = tuple(context.scene.blade_color),
                brightness = context.scene.blade_brightness,
            )
            try:
                context.scene.view_settings.view_transform = 'Standard'
            except Exception:
                pass
            try:
                context.scene.render.film_transparent = True
            except Exception:
                pass
            # Enable the Emit render pass so the EXR captured in Step 1 contains
            # the blade's pure emission data as a named pass. This is what feeds
            # the glare node in the compositor — without it the EXR has no Emit
            # output and Step 2 has nothing clean to bloom from.
            try:
                context.view_layer.use_pass_emit = True
            except Exception:
                pass
            # Re-apply video if one was already selected before this rig was added
            vid = context.scene.blade_video_path
            if vid:
                _apply_video(context, vid)
            self.report({'INFO'}, "Lightsaber added!")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        return {'FINISHED'}


class BLADE_OT_add_3d_lightsaber(bpy.types.Operator):
    bl_idname      = "blade.add_3d_lightsaber"
    bl_label       = "3D Saber (Pro Mode)"
    bl_description = "Build a 3D lightsaber rig with half-sphere geometry attached"
    bl_options     = {'REGISTER', 'UNDO'}

    def execute(self, context):
        try:
            build_3d(
                color      = tuple(context.scene.blade_color),
                brightness = context.scene.blade_brightness,
            )
            try:
                context.scene.view_settings.view_transform = 'Standard'
            except Exception:
                pass
            try:
                context.scene.render.film_transparent = True
            except Exception:
                pass
            try:
                context.view_layer.use_pass_emit = True
            except Exception:
                pass
            vid = context.scene.blade_video_path
            if vid:
                _apply_video(context, vid)
            self.report({'INFO'}, "3D Lightsaber added!")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        return {'FINISHED'}


class BLADE_OT_snap_to_volume(bpy.types.Operator):
    bl_idname      = "blade.snap_to_volume"
    bl_label       = "Snap to Volume"
    bl_description = "Toggle viewport snapping to Volume on/off"
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ls_name = context.scene.blade_active_ls
        if not ls_name or ls_name == 'NONE':
            return False
        coll = bpy.data.collections.get(ls_name)
        if coll is None:
            return False
        return any(obj.name.startswith("Blade_Target_") for obj in coll.objects)

    def execute(self, context):
        ts = context.scene.tool_settings
        if context.scene.blade_snap_to_volume:
            # Turn off — restore snap state
            ts.use_snap      = False
            ts.snap_elements = {'INCREMENT'}
            context.scene.blade_snap_to_volume = False
        else:
            # Turn on — enable snapping to Volume
            ts.use_snap      = True
            ts.snap_elements = {'VOLUME'}
            context.scene.blade_snap_to_volume = True
        return {'FINISHED'}


class BLADE_OT_create_mask(bpy.types.Operator):
    bl_idname      = "blade.create_mask"
    bl_label       = "Create Mask"
    bl_description = ("Add a holdout plane for masking. "
                      "Use the Knife tool in Edit Mode to cut your shape")
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        """Require at least one lightsaber — no point masking an empty scene."""
        return len(_get_ls_list(context.scene)) > 0

    def execute(self, context):
        scene = context.scene
        frame = scene.frame_current
        vl    = context.view_layer

        # Safely escape any active edit mode before adding new geometry
        active = vl.objects.active
        if active and active.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception:
                pass
        bpy.ops.object.select_all(action='DESELECT')

        count     = scene.get("_blade_mask_count", 0) + 1
        scene["_blade_mask_count"] = count
        mask_name = f"Mask {count}"

        bpy.ops.mesh.primitive_plane_add(
            enter_editmode=False, align='WORLD',
            location=(0, 0, 0), scale=(1, 1, 1),
        )
        plane      = context.active_object
        plane.name = mask_name

        # FIX #15: Named constants replace the bare magic numbers that were here.
        bpy.ops.transform.rotate(
            value=math.pi / 2, orient_axis='X', orient_type='GLOBAL')
        bpy.ops.transform.translate(
            value=(0, MASK_PLANE_Y_OFFSET, 0), orient_type='GLOBAL',
            constraint_axis=(False, True, False))
        bpy.ops.transform.resize(
            value=(MASK_PLANE_SCALE_XY,) * 3, orient_type='GLOBAL')
        bpy.ops.transform.resize(
            value=(1, 1, MASK_PLANE_SCALE_Z), orient_type='GLOBAL',
            constraint_axis=(False, False, True))

        plane.is_holdout = True

        masks_coll = _masks_collection(scene)
        _unlink_from_all_collections(plane)
        masks_coll.objects.link(plane)

        # Keyframe visibility: hidden at frame-1 and frame+1, visible at frame.
        for hide, fr in ((True, frame - 1), (False, frame), (True, frame + 1)):
            plane.hide_viewport = hide
            plane.keyframe_insert(data_path="hide_viewport", frame=fr)

        for hide, fr in ((True, frame - 2), (False, frame - 1), (True, frame)):
            plane.hide_render = hide
            plane.keyframe_insert(data_path="hide_render", frame=fr)
        # Leave it visible now for immediate editing
        plane.hide_viewport = False
        plane.hide_render   = False

        vl.objects.active = plane
        plane.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.wm.tool_set_by_id(name="builtin.knife")

        self.report(
            {'INFO'},
            f"'{mask_name}' ready — draw cut, Enter to cut, then Mask Shape for face select",
        )
        return {'FINISHED'}


class BLADE_OT_mask_shape(bpy.types.Operator):
    bl_idname      = "blade.mask_shape"
    bl_label       = "Knife / Select Toggle"
    bl_description = "Toggle between Knife tool and face Select mode"

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def execute(self, context):
        try:
            current = context.workspace.tools.from_space_view3d_mode('EDIT_MESH').idname
        except Exception:
            current = ''
        if current == 'builtin.knife':
            bpy.ops.wm.tool_set_by_id(name="builtin.select_box")
            context.tool_settings.mesh_select_mode = (False, False, True)
        else:
            bpy.ops.wm.tool_set_by_id(name="builtin.knife")
        return {'FINISHED'}


class BLADE_OT_step1_render(bpy.types.Operator):
    bl_idname      = "blade.step1_render"
    bl_label       = "Step 1: Render Blade EXR"
    bl_description = ("Hides masks from camera, renders the blade as a floating-point "
                      "EXR sequence, then rebuilds the compositor to feed from those EXRs")
    bl_options     = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        """Require both a background video and at least one lightsaber."""
        scene = context.scene
        return bool(scene.blade_video_path) and len(_get_ls_list(scene)) > 0

    def invoke(self, context, event):
        """Show a confirmation dialog before starting a potentially long render."""
        return context.window_manager.invoke_confirm(self, event)

    def draw(self, context):
        self.layout.label(
            text="Render all frames as EXR? This may take several minutes."
        )

    def execute(self, context):
        scene = context.scene
        rd    = scene.render

        # ── 1. Hide mask objects from camera ──────────────────────────────
        masks_coll = bpy.data.collections.get("Masks")
        if masks_coll:
            for obj in masks_coll.objects:
                try:
                    obj.visible_camera = False
                except Exception:
                    pass
                try:
                    obj.cycles_visibility.camera = False
                except Exception:
                    pass

        # ── 2. Back up glow/saturation for compositor rebuild ──────────────
        scene['_blade_glow_strength_bak'] = scene.blade_glow_strength
        scene['_blade_glow_size_bak']     = scene.blade_glow_size
        scene['_blade_saturation_bak']    = scene.blade_saturation

        # ── 3. Remove compositor node group ───────────────────────────────
        if scene.compositing_node_group:
            old_grp = scene.compositing_node_group
            scene.compositing_node_group = None
            if old_grp.users == 0:
                bpy.data.node_groups.remove(old_grp)
        scene.use_nodes              = False
        scene.render.use_compositing = False

        # ── 4. Resolve the EXR output directory ───────────────────────────
        # Pass scene explicitly so the result is authoritative even if
        # bpy.context.scene were somehow stale.
        blade_dir = _blade_exr_dir(scene)

        # Create the temp fallback folder if it doesn't exist yet.
        # For user-chosen folders we require them to already exist (they picked
        # it from the browser, so it should be there) — makedirs with exist_ok
        # handles both cases safely.
        os.makedirs(blade_dir, exist_ok=True)

        # Wipe only .exr files inside the folder so we don't clobber other
        # files in a user-chosen directory (e.g. their project footage).
        import shutil as _shutil
        for f in os.listdir(blade_dir):
            if f.lower().endswith('.exr'):
                fp = os.path.join(blade_dir, f)
                try:
                    os.remove(fp)
                except Exception as e:
                    self.report({'WARNING'}, f"Could not delete {fp}: {e}")

        # Store the resolved path so Step 2 and the compositor always know
        # exactly which folder the EXR frames landed in, regardless of whether
        # the user changes the field between Step 1 and Step 2.
        scene['_blade_exr_dir_used'] = blade_dir

        exr_output = os.path.join(blade_dir, "blade_")

        # ── 5. Set output to OpenEXR Multilayer, RGBA ──────────────────────
        prev_filepath    = rd.filepath
        prev_file_format = rd.image_settings.file_format
        prev_color_mode  = rd.image_settings.color_mode
        try:
            prev_media_type = rd.image_settings.media_type
        except Exception:
            prev_media_type = None

        # Switch to image output first — when media_type is VIDEO the file_format
        # enum is locked to 'FFMPEG' and rejects 'OPEN_EXR'. Setting media_type
        # to 'IMAGE' first unlocks the full image format enum.
        rd.image_settings.media_type  = 'MULTI_LAYER_IMAGE'
        rd.image_settings.exr_codec   = 'DWAA'
        rd.image_settings.quality     = 100
        rd.image_settings.color_mode  = 'RGBA'
        rd.filepath = exr_output

        # ── 6. Render EXR sequence ─────────────────────────────────────────
        self.report({'INFO'}, f"Rendering EXR sequence to {blade_dir} …")
        bpy.ops.render.render(animation=True)

        # ── 7. Restore render output settings ─────────────────────────────
        rd.filepath = prev_filepath
        # media_type MUST be restored before file_format — it controls which
        # file_format enum values are valid. Wrong order causes a Blender crash.
        if prev_media_type is not None:
            try:
                rd.image_settings.media_type = prev_media_type
            except Exception:
                pass
        try:
            rd.image_settings.file_format = prev_file_format
        except Exception:
            pass
        rd.image_settings.color_mode = prev_color_mode

        # ── 8. Rebuild compositor from EXR sequence ────────────────────────
        scene.use_nodes              = True
        scene.render.use_compositing = True
        _setup_compositor_from_exr(
            scene,
            exr_folder  = blade_dir,
            frame_start = scene.frame_start,
            frame_end   = scene.frame_end,
        )

        scene['_blade_step1_done'] = True
        self.report({'INFO'}, "Step 1 complete — compositor rebuilt with EXR sequence.")
        return {'FINISHED'}


class BLADE_OT_step2_render(bpy.types.Operator):
    bl_idname      = "blade.step2_render"
    bl_label       = "Step 2: Composite & Render Final"
    bl_description = ("Hides lightsaber collections, restores masks as emission planes "
                      "showing the background video, wires a Render Layers node into "
                      "the compositor for the mask layer, then renders the final output")
    bl_options     = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        """
        FIX #21/#23: Require both Step 1 completion AND that the EXR folder
        still exists on disk. The old code showed Step 2 even if the user had
        cleared their temp directory or re-opened the file on a different machine.
        """
        return (
            bool(context.scene.get('_blade_step1_done')) and
            os.path.isdir(context.scene.get('_blade_exr_dir_used', _blade_exr_dir(context.scene)))
        )

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def draw(self, context):
        self.layout.label(text="Render the final composite over all frames? Continue?")

    def execute(self, context):
        scene = context.scene

        # ── 1. Hide all lightsaber collections from viewport + render ──────
        ls_list = _get_ls_list(scene)
        for ls_name in ls_list:
            coll = bpy.data.collections.get(ls_name)
            if coll:
                coll.hide_render = True
                # FIX #2: _hide_layer_coll is now a module-level function,
                # not one re-defined inside this loop on every iteration.
                _hide_layer_coll(context.view_layer.layer_collection, ls_name)

        # ── 2. Restore masks: camera-visible, holdout OFF, emission material ──
        masks_coll = bpy.data.collections.get("Masks")
        bg_path    = scene.blade_video_path

        if masks_coll:
            abs_path = bpy.path.abspath(bg_path) if bg_path else ""
            bg_img   = None
            if abs_path and os.path.isfile(abs_path):
                bg_img = next(
                    (img for img in bpy.data.images
                     if bpy.path.abspath(img.filepath) == abs_path),
                    None,
                )
                if bg_img is None:
                    bg_img = bpy.data.images.load(bg_path)
                try:
                    bg_img.source = 'MOVIE'
                except Exception:
                    pass

            for obj in masks_coll.objects:
                try:
                    obj.visible_camera = True
                except Exception:
                    pass
                try:
                    obj.cycles_visibility.camera = True
                except Exception:
                    pass
                obj.is_holdout = False

                # Build emission + video-texture material so the mask plane
                # shows the background footage during the final render pass.
                mat_name = f"MaskEmit_{obj.name}"
                existing = bpy.data.materials.get(mat_name)
                if existing:
                    bpy.data.materials.remove(existing)

                mat           = bpy.data.materials.new(mat_name)
                mat.use_nodes = True
                nodes         = mat.node_tree.nodes
                links         = mat.node_tree.links
                nodes.clear()

                n_out   = nodes.new('ShaderNodeOutputMaterial')
                n_emit  = nodes.new('ShaderNodeEmission')
                n_tex   = nodes.new('ShaderNodeTexImage')
                n_coord = nodes.new('ShaderNodeTexCoord')
                n_map   = nodes.new('ShaderNodeMapping')

                n_coord.location = (-800, 0)
                n_map.location   = (-600, 0)
                n_tex.location   = (-350, 0)
                n_emit.location  = ( -50, 0)
                n_out.location   = ( 200, 0)

                links.new(n_coord.outputs['Window'],    n_map.inputs['Vector'])
                links.new(n_map.outputs['Vector'],      n_tex.inputs['Vector'])
                links.new(n_tex.outputs['Color'],       n_emit.inputs['Color'])
                links.new(n_emit.outputs['Emission'],   n_out.inputs['Surface'])

                if bg_img:
                    n_tex.image = bg_img
                    try:
                        n_tex.image_user.frame_duration   = scene.frame_end - scene.frame_start + 1
                        n_tex.image_user.frame_start      = scene.frame_start
                        n_tex.image_user.frame_offset     = 0
                        n_tex.image_user.use_cyclic       = True
                        n_tex.image_user.use_auto_refresh = True
                    except Exception:
                        pass

                if obj.data.materials:
                    obj.data.materials[0] = mat
                else:
                    obj.data.materials.append(mat)

        # ── 3. Rebuild compositor for Step 2 ──────────────────────────────
        # Replace the existing node group entirely with the full Step 2 graph:
        #   RenderLayers.Alpha → IDMask → Invert → AlphaOver2.Factor
        #   IDMask → SetAlpha.Alpha
        #   MovieClip → Scale → SetAlpha.Image → AlphaOver1.Background
        #   EXR → Glare → ColorConvert → HueSat → AlphaOver1.Foreground
        #   AlphaOver1 → AlphaOver2.Background
        #   MovieClip2 → AlphaOver2.Foreground
        #   AlphaOver2 → Output + Viewer
        grp = bpy.data.node_groups.get("Lightsaber_Composite")
        if grp:
            grp_nodes = grp.nodes
            grp_links = grp.links

            # Find existing nodes we need to keep/reference
            n_clip_existing = grp_nodes.get(grp.get('_clip_node_name', ''))
            n_img_existing  = next((n for n in grp_nodes if n.bl_idname == 'CompositorNodeImage'), None)
            n_alpha_existing = next((n for n in grp_nodes if n.bl_idname == 'CompositorNodeAlphaOver'), None)
            n_out_existing  = next((n for n in grp_nodes if n.bl_idname == 'NodeGroupOutput'), None)
            n_viewer_existing = next((n for n in grp_nodes if n.bl_idname == 'CompositorNodeViewer'), None)

            # Add new nodes
            n_render   = grp_nodes.new('CompositorNodeRLayers')
            n_clip2    = grp_nodes.new('CompositorNodeMovieClip')
            n_setalpha = grp_nodes.new('CompositorNodeSetAlpha')
            n_idmask   = grp_nodes.new('CompositorNodeIDMask')
            n_invert   = grp_nodes.new('CompositorNodeInvert')
            n_alpha2   = grp_nodes.new('CompositorNodeAlphaOver')

            # Position new nodes relative to existing layout
            if n_alpha_existing:
                ax, ay = n_alpha_existing.location.x, n_alpha_existing.location.y
            else:
                ax, ay = 550, 100

            n_render.location   = (ax - 1000, ay + 300)
            n_idmask.location   = (ax -  700, ay + 300)
            n_invert.location   = (ax -  400, ay + 300)
            n_setalpha.location = (ax -  400, ay)
            n_clip2.location    = (ax,        ay - 300)
            n_alpha2.location   = (ax +  300, ay + 150)

            try:
                n_setalpha.mode = 'APPLY_MASK'
            except Exception:
                pass

            # Re-wire clip2 to same clip as clip1
            if n_clip_existing and n_clip_existing.clip:
                n_clip2.clip = n_clip_existing.clip

            # Disconnect existing AlphaOver from output/viewer
            for link in list(grp_links):
                if n_alpha_existing and link.from_node == n_alpha_existing:
                    grp_links.remove(link)

            # Find the Scale node output → redirect through SetAlpha
            n_scale_existing = next((n for n in grp_nodes if n.bl_idname == 'CompositorNodeScale'), None)
            if n_scale_existing:
                # Remove Scale → old AlphaOver Background link
                for link in list(grp_links):
                    if link.from_node == n_scale_existing and link.to_node == n_alpha_existing:
                        grp_links.remove(link)
                grp_links.new(n_scale_existing.outputs['Image'], n_setalpha.inputs['Image'])

            # RenderLayers Alpha → IDMask → two destinations
            grp_links.new(n_render.outputs['Alpha'],    n_idmask.inputs[0])
            grp_links.new(n_idmask.outputs[0],          n_invert.inputs['Color'])
            grp_links.new(n_invert.outputs['Color'],    n_alpha2.inputs['Fac'])   # Factor
            grp_links.new(n_idmask.outputs[0],          n_setalpha.inputs['Alpha'])

            # SetAlpha → AlphaOver1 Background
            grp_links.new(n_setalpha.outputs['Image'],  n_alpha_existing.inputs['Background'])

            # AlphaOver1 → AlphaOver2 Background ; MovieClip2 → AlphaOver2 Foreground
            grp_links.new(n_alpha_existing.outputs['Image'], n_alpha2.inputs['Background'])
            grp_links.new(n_clip2.outputs['Image'],     n_alpha2.inputs['Foreground'])

            # AlphaOver2 → Output + Viewer
            if n_out_existing:
                grp_links.new(n_alpha2.outputs['Image'], n_out_existing.inputs[0])
            if n_viewer_existing:
                for link in list(grp_links):
                    if link.to_node == n_viewer_existing:
                        grp_links.remove(link)
                grp_links.new(n_alpha2.outputs['Image'], n_viewer_existing.inputs['Image'])

            grp['_clip2_node_name'] = n_clip2.name

        # ── 4. Render final animation ──────────────────────────────────────
        self.report({'INFO'}, "Rendering final composite …")
        bpy.ops.render.render(animation=True)
        self.report({'INFO'}, "Step 2 complete — final render done!")
        return {'FINISHED'}


class BLADE_OT_reset_render(bpy.types.Operator):
    bl_idname      = "blade.reset_render"
    bl_label       = "Reset Render"
    bl_description = ("Reset compositor nodes to the live state (same as just after "
                      "Add Lightsaber). Restores lightsaber collection visibility and "
                      "mask holdout state. Does NOT touch lightsabers, scene frames, "
                      "or any other scene settings.")
    bl_options     = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(_get_ls_list(context.scene)) > 0

    def execute(self, context):
        scene = context.scene

        # ── 1. Restore lightsaber collection visibility ────────────────────
        # Undo whatever Step 2 did: make collections render-visible again and
        # unhide them in the viewport layer collection tree.
        ls_list = _get_ls_list(scene)
        for ls_name in ls_list:
            coll = bpy.data.collections.get(ls_name)
            if coll:
                coll.hide_render = False
            _show_layer_coll(context.view_layer.layer_collection, ls_name)

        # ── 2. Restore masks back to holdout (undo Step 2 emission mats) ──
        masks_coll = bpy.data.collections.get("Masks")
        if masks_coll:
            for obj in masks_coll.objects:
                # Re-enable holdout so they work as masks again
                obj.is_holdout = True
                # Hide from camera (Step 1 hid them; reset should match pre-Step-1)
                try:
                    obj.visible_camera = False
                except Exception:
                    pass
                try:
                    obj.cycles_visibility.camera = False
                except Exception:
                    pass
                # Remove the MaskEmit material Step 2 applied; restore original holdout
                mat_name = f"MaskEmit_{obj.name}"
                emit_mat = bpy.data.materials.get(mat_name)
                if emit_mat:
                    if obj.data.materials and obj.data.materials[0] == emit_mat:
                        obj.data.materials.pop(index=0)
                    bpy.data.materials.remove(emit_mat)

        # ── 3. Rebuild live compositor (MovieClip + RLayers path) ──────────
        # Exactly what build() sets up — no frames are touched here.
        _setup_compositor(scene)
        scene.use_nodes              = True
        scene.render.use_compositing = True

        # ── 4. Re-wire the video clip if one was already loaded ────────────
        # Use direct node assignment to avoid _apply_video resetting frame range.
        vid = scene.blade_video_path
        if vid:
            grp = bpy.data.node_groups.get("Lightsaber_Composite")
            if grp:
                abs_fp = bpy.path.abspath(vid)
                clip = next(
                    (c for c in bpy.data.movieclips
                     if bpy.path.abspath(c.filepath) == abs_fp),
                    None,
                )
                if clip:
                    node = grp.nodes.get(grp.get('_clip_node_name', ''))
                    if node and node.bl_idname == 'CompositorNodeMovieClip':
                        node.clip = clip

        # ── 5. Clear step flags so panel status is accurate ───────────────
        for key in ('_blade_step1_done', '_blade_exr_dir_used'):
            if key in scene:
                del scene[key]

        self.report({'INFO'}, "Compositor reset — lightsaber collections restored.")
        return {'FINISHED'}


class BLADE_OT_render_combined(bpy.types.Operator):
    bl_idname      = "blade.render_combined"
    bl_label       = "Render Combined"
    bl_description = ("Run the full Step 1 (Render Blade EXR + rebuild compositor) "
                      "and then Step 2 (Composite & Render Final) back-to-back in "
                      "the correct sequence, in a single click")
    bl_options     = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        scene = context.scene
        return bool(scene.blade_video_path) and len(_get_ls_list(scene)) > 0

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def draw(self, context):
        self.layout.label(
            text="Run full combined render (Step 1 then Step 2)? This may take several minutes."
        )

    def execute(self, context):
        scene = context.scene
        rd    = scene.render

        # ══════════════════════════════════════════════════════════════════
        #  STEP 1 — Render blade EXR sequence + rebuild compositor
        # ══════════════════════════════════════════════════════════════════

        # ── S1-1. Hide mask objects from camera ───────────────────────────
        masks_coll = bpy.data.collections.get("Masks")
        if masks_coll:
            for obj in masks_coll.objects:
                try:
                    obj.visible_camera = False
                except Exception:
                    pass
                try:
                    obj.cycles_visibility.camera = False
                except Exception:
                    pass

        # ── S1-2. Back up glow/saturation for compositor rebuild ───────────
        scene['_blade_glow_strength_bak'] = scene.blade_glow_strength
        scene['_blade_glow_size_bak']     = scene.blade_glow_size
        scene['_blade_saturation_bak']    = scene.blade_saturation

        # ── S1-3. Remove compositor node group ────────────────────────────
        if scene.compositing_node_group:
            old_grp = scene.compositing_node_group
            scene.compositing_node_group = None
            if old_grp.users == 0:
                bpy.data.node_groups.remove(old_grp)
        scene.use_nodes              = False
        scene.render.use_compositing = False

        # ── S1-4. Resolve EXR output directory & back up render settings ───
        prev_filepath    = rd.filepath
        prev_file_format = rd.image_settings.file_format
        prev_color_mode  = rd.image_settings.color_mode
        try:
            prev_media_type = rd.image_settings.media_type
        except Exception:
            prev_media_type = None

        # Pass scene explicitly — reliable in all operator contexts.
        blade_dir = _blade_exr_dir(scene)
        os.makedirs(blade_dir, exist_ok=True)

        # Wipe only .exr files so we don't clobber other files in a
        # user-chosen directory (e.g. their project footage).
        for f in os.listdir(blade_dir):
            if f.lower().endswith('.exr'):
                fp = os.path.join(blade_dir, f)
                try:
                    os.remove(fp)
                except Exception as e:
                    self.report({'WARNING'}, f"Could not delete {fp}: {e}")

        # Store the resolved path so Step 2 (and the compositor) always reads
        # EXRs from exactly the same folder Step 1 wrote them to.
        scene['_blade_exr_dir_used'] = blade_dir

        exr_output = os.path.join(blade_dir, "blade_")

        rd.image_settings.media_type  = 'MULTI_LAYER_IMAGE'
        rd.image_settings.exr_codec   = 'DWAA'
        rd.image_settings.quality     = 100
        rd.image_settings.color_mode  = 'RGBA'
        rd.filepath = exr_output

        # ── S1-5. Render EXR sequence ─────────────────────────────────────
        self.report({'INFO'}, f"Combined: rendering EXR sequence to {blade_dir} …")
        bpy.ops.render.render(animation=True)

        # ── S1-6. Restore render output settings ──────────────────────────
        rd.filepath = prev_filepath
        if prev_media_type is not None:
            try:
                rd.image_settings.media_type = prev_media_type
            except Exception:
                pass
        try:
            rd.image_settings.file_format = prev_file_format
        except Exception:
            pass
        rd.image_settings.color_mode = prev_color_mode

        # ── S1-7. Rebuild compositor from EXR sequence ────────────────────
        scene.use_nodes              = True
        scene.render.use_compositing = True
        _setup_compositor_from_exr(
            scene,
            exr_folder  = blade_dir,
            frame_start = scene.frame_start,
            frame_end   = scene.frame_end,
        )
        scene['_blade_step1_done'] = True
        self.report({'INFO'}, "Combined: Step 1 complete — compositor rebuilt. Starting Step 2 …")

        # ══════════════════════════════════════════════════════════════════
        #  STEP 2 — Composite & render final output
        #  (runs immediately after Step 1 with compositor already set up)
        # ══════════════════════════════════════════════════════════════════

        # ── S2-1. Hide all lightsaber collections from viewport + render ──
        ls_list = _get_ls_list(scene)
        for ls_name in ls_list:
            coll = bpy.data.collections.get(ls_name)
            if coll:
                coll.hide_render = True
                _hide_layer_coll(context.view_layer.layer_collection, ls_name)

        # ── S2-2. Restore masks: camera-visible, holdout OFF, emission mat ─
        bg_path = scene.blade_video_path
        if masks_coll:
            abs_path = bpy.path.abspath(bg_path) if bg_path else ""
            bg_img   = None
            if abs_path and os.path.isfile(abs_path):
                bg_img = next(
                    (img for img in bpy.data.images
                     if bpy.path.abspath(img.filepath) == abs_path),
                    None,
                )
                if bg_img is None:
                    bg_img = bpy.data.images.load(bg_path)
                try:
                    bg_img.source = 'MOVIE'
                except Exception:
                    pass

            for obj in masks_coll.objects:
                try:
                    obj.visible_camera = True
                except Exception:
                    pass
                try:
                    obj.cycles_visibility.camera = True
                except Exception:
                    pass
                obj.is_holdout = False

                mat_name = f"MaskEmit_{obj.name}"
                existing = bpy.data.materials.get(mat_name)
                if existing:
                    bpy.data.materials.remove(existing)

                mat           = bpy.data.materials.new(mat_name)
                mat.use_nodes = True
                nodes         = mat.node_tree.nodes
                links         = mat.node_tree.links
                nodes.clear()

                n_out   = nodes.new('ShaderNodeOutputMaterial')
                n_emit  = nodes.new('ShaderNodeEmission')
                n_tex   = nodes.new('ShaderNodeTexImage')
                n_coord = nodes.new('ShaderNodeTexCoord')
                n_map   = nodes.new('ShaderNodeMapping')

                n_coord.location = (-800, 0)
                n_map.location   = (-600, 0)
                n_tex.location   = (-350, 0)
                n_emit.location  = ( -50, 0)
                n_out.location   = ( 200, 0)

                links.new(n_coord.outputs['Window'],  n_map.inputs['Vector'])
                links.new(n_map.outputs['Vector'],    n_tex.inputs['Vector'])
                links.new(n_tex.outputs['Color'],     n_emit.inputs['Color'])
                links.new(n_emit.outputs['Emission'], n_out.inputs['Surface'])

                if bg_img:
                    n_tex.image = bg_img
                    try:
                        n_tex.image_user.frame_duration   = scene.frame_end - scene.frame_start + 1
                        n_tex.image_user.frame_start      = scene.frame_start
                        n_tex.image_user.frame_offset     = 0
                        n_tex.image_user.use_cyclic       = True
                        n_tex.image_user.use_auto_refresh = True
                    except Exception:
                        pass

                if obj.data.materials:
                    obj.data.materials[0] = mat
                else:
                    obj.data.materials.append(mat)

        # ── S2-3. Extend compositor for Step 2 mask layer ─────────────────
        grp = bpy.data.node_groups.get("Lightsaber_Composite")
        if grp:
            grp_nodes = grp.nodes
            grp_links = grp.links

            n_clip_existing   = grp_nodes.get(grp.get('_clip_node_name', ''))
            n_alpha_existing  = next((n for n in grp_nodes if n.bl_idname == 'CompositorNodeAlphaOver'), None)
            n_out_existing    = next((n for n in grp_nodes if n.bl_idname == 'NodeGroupOutput'), None)
            n_viewer_existing = next((n for n in grp_nodes if n.bl_idname == 'CompositorNodeViewer'), None)

            n_render   = grp_nodes.new('CompositorNodeRLayers')
            n_clip2    = grp_nodes.new('CompositorNodeMovieClip')
            n_setalpha = grp_nodes.new('CompositorNodeSetAlpha')
            n_idmask   = grp_nodes.new('CompositorNodeIDMask')
            n_invert   = grp_nodes.new('CompositorNodeInvert')
            n_alpha2   = grp_nodes.new('CompositorNodeAlphaOver')

            if n_alpha_existing:
                ax, ay = n_alpha_existing.location.x, n_alpha_existing.location.y
            else:
                ax, ay = 550, 100

            n_render.location   = (ax - 1000, ay + 300)
            n_idmask.location   = (ax -  700, ay + 300)
            n_invert.location   = (ax -  400, ay + 300)
            n_setalpha.location = (ax -  400, ay)
            n_clip2.location    = (ax,        ay - 300)
            n_alpha2.location   = (ax +  300, ay + 150)

            try:
                n_setalpha.mode = 'APPLY_MASK'
            except Exception:
                pass

            if n_clip_existing and n_clip_existing.clip:
                n_clip2.clip = n_clip_existing.clip

            for link in list(grp_links):
                if n_alpha_existing and link.from_node == n_alpha_existing:
                    grp_links.remove(link)

            n_scale_existing = next((n for n in grp_nodes if n.bl_idname == 'CompositorNodeScale'), None)
            if n_scale_existing:
                for link in list(grp_links):
                    if link.from_node == n_scale_existing and link.to_node == n_alpha_existing:
                        grp_links.remove(link)
                grp_links.new(n_scale_existing.outputs['Image'], n_setalpha.inputs['Image'])

            grp_links.new(n_render.outputs['Alpha'],          n_idmask.inputs[0])
            grp_links.new(n_idmask.outputs[0],                n_invert.inputs['Color'])
            grp_links.new(n_invert.outputs['Color'],          n_alpha2.inputs['Fac'])
            grp_links.new(n_idmask.outputs[0],                n_setalpha.inputs['Alpha'])
            grp_links.new(n_setalpha.outputs['Image'],        n_alpha_existing.inputs['Background'])
            grp_links.new(n_alpha_existing.outputs['Image'],  n_alpha2.inputs['Background'])
            grp_links.new(n_clip2.outputs['Image'],           n_alpha2.inputs['Foreground'])

            if n_out_existing:
                grp_links.new(n_alpha2.outputs['Image'], n_out_existing.inputs[0])
            if n_viewer_existing:
                for link in list(grp_links):
                    if link.to_node == n_viewer_existing:
                        grp_links.remove(link)
                grp_links.new(n_alpha2.outputs['Image'], n_viewer_existing.inputs['Image'])

            grp['_clip2_node_name'] = n_clip2.name

        # ── S2-4. Render final animation ──────────────────────────────────
        self.report({'INFO'}, "Combined: rendering final composite …")
        bpy.ops.render.render(animation=True)
        self.report({'INFO'}, "Combined render complete — both steps finished!")
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────────────────
#  PANELS
#  Single N-panel tab "Lightsaber" with collapsible child sub-panels.
#  All panels share bl_category = "Lightsaber" so they appear in one tab.
#  Child panels use bl_parent_id + bl_options = {'DEFAULT_CLOSED'} to be
#  expandable/collapsible sections inside the root panel.
#
#  Layout:
#    BLADE_PT_main         — root: Add button, active selector, see-through, video
#      BLADE_PT_blade      — collapsible: material, presets, color, size, glow
#      BLADE_PT_animation  — collapsible: auto-keyframe, motion blur
#      BLADE_PT_render     — collapsible: Step 1 / Step 2 / Reset / Combined status + buttons
#        BLADE_PT_video_export  — collapsible (nested): output settings
#      BLADE_PT_mask       — collapsible: mask creation + knife tool
# ─────────────────────────────────────────────────────────────────────────────

_CATEGORY = "Lightsaber"


class BLADE_PT_main(bpy.types.Panel):
    """Root panel — always visible at the top of the tab."""
    bl_label       = "Blender Saber Addon"
    bl_idname      = "BLADE_PT_main"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = _CATEGORY

    def draw(self, context):
        layout  = self.layout
        scene   = context.scene
        ls_list = _get_ls_list(scene)

        # ── Add Lightsaber ─────────────────────────────────────────────────
        col = layout.column()
        col.scale_y = 2.0
        col.operator("blade.add_lightsaber", icon='LIGHT')
        col.operator("blade.add_3d_lightsaber", icon='MESH_UVSPHERE')

        layout.separator()

        if ls_list:
            # ── Active lightsaber selector ─────────────────────────────────
            box = layout.box()
            box.label(text="Active Lightsaber", icon='OUTLINER_OB_LIGHT')
            box.prop(scene, "blade_active_ls", text="")
            box.prop(scene, "blade_isolate",   text="Isolate this Lightsaber")

            layout.separator()

            row = layout.row(align=True)
            row.prop(scene, "blade_see_through",
                     text="See Through", icon='XRAY', toggle=True)

        layout.separator()

        # ── Background Video / Image — always visible ──────────────────────
        box_v = layout.box()
        box_v.label(text="Background Video / Image", icon='SEQUENCE')
        row = box_v.row(align=True)
        row.prop(scene, "blade_video_path", text="")
        row.operator("blade.browse_media", text="", icon='FILEBROWSER')
        box_v.separator(factor=0.4)
        box_v.prop(scene, "blade_framerate", text="Framerate")


class BLADE_PT_blade(bpy.types.Panel):
    """Collapsible: blade material, size, and glow settings."""
    bl_label       = "Blade Material & Glow"
    bl_idname      = "BLADE_PT_blade"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = _CATEGORY
    bl_parent_id   = "BLADE_PT_main"
    bl_options     = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return len(_get_ls_list(context.scene)) > 0

    def draw(self, context):
        layout = self.layout
        scene  = context.scene

        # ── Presets ────────────────────────────────────────────────────────
        preset_row = layout.row(align=True)
        preset_row.prop(scene, "blade_active_preset", text="")
        preset_row.operator("blade.load_preset",           text="", icon='CHECKMARK')
        preset_row.operator("blade.confirm_delete_preset", text="", icon='TRASH')

        save_row = layout.row(align=True)
        save_row.prop(scene, "blade_preset_name", text="")
        save_row.operator("blade.save_preset", text="", icon='ADD')

        layout.separator(factor=0.5)

        # ── Color ──────────────────────────────────────────────────────────
        layout.template_color_picker(scene, "blade_color", value_slider=True)
        layout.row(align=True).prop(scene, "blade_color", text="Glow Color")
        layout.separator(factor=0.5)
        layout.prop(scene, "blade_brightness", text="Glow Brightness", slider=True)
        layout.prop(scene, "blade_saturation", text="Saturation",      slider=True)

        layout.separator()

        # ── Size ───────────────────────────────────────────────────────────
        layout.label(text="Blade Size", icon='MESH_CYLINDER')
        layout.prop(scene, "blade_width", text="Width  (5.0 = default)", slider=True)

        layout.separator()

        # ── Glow ───────────────────────────────────────────────────────────
        layout.label(text="Glow", icon='LIGHT')
        layout.prop(scene, "blade_glow_strength", slider=True)
        layout.prop(scene, "blade_glow_size",     slider=True)


class BLADE_PT_animation(bpy.types.Panel):
    """Collapsible: auto-keyframe and motion blur controls."""
    bl_label       = "Animation"
    bl_idname      = "BLADE_PT_animation"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = _CATEGORY
    bl_parent_id   = "BLADE_PT_main"
    bl_options     = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return len(_get_ls_list(context.scene)) > 0

    def draw(self, context):
        layout = self.layout
        scene  = context.scene

        layout.prop(scene.tool_settings, "use_keyframe_insert_auto",
                    text="Auto Keyframe", icon='REC', toggle=True)

        layout.separator(factor=0.5)

        # ── Snap to Volume (3D saber only) ────────────────────────────────
        if BLADE_OT_snap_to_volume.poll(context):
            layout.operator(
                "blade.snap_to_volume",
                text    = "Snap to Volume  (ON)" if scene.blade_snap_to_volume else "Snap to Volume  (OFF)",
                icon    = 'MESH_UVSPHERE',
                depress = scene.blade_snap_to_volume,
            )
            layout.separator(factor=0.5)

        layout.separator(factor=0.5)

        layout.prop(scene.render, "use_motion_blur", text="Motion Blur")

        blur_row         = layout.row()
        blur_row.enabled = scene.render.use_motion_blur
        blur_row.prop(scene.render, "motion_blur_shutter", text="Blur Amount")

        # FIX #19: motion_blur_steps may have moved to view_layer.eevee in 5.0.
        steps_row         = layout.row()
        steps_row.enabled = scene.render.use_motion_blur
        try:
            steps_row.prop(scene.eevee, "motion_blur_steps", text="Blur Quality")
        except AttributeError:
            try:
                steps_row.prop(context.view_layer.eevee, "motion_blur_steps",
                               text="Blur Quality")
            except AttributeError:
                steps_row.label(text="(Blur Quality unavailable in this build)")


class BLADE_PT_render(bpy.types.Panel):
    """Collapsible: two-step render workflow."""
    bl_label       = "Render Process"
    bl_idname      = "BLADE_PT_render"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = _CATEGORY
    bl_parent_id   = "BLADE_PT_main"
    bl_options     = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return len(_get_ls_list(context.scene)) > 0

    def draw(self, context):
        layout     = self.layout
        scene      = context.scene
        has_video  = bool(scene.blade_video_path)
        step1_done = bool(scene.get('_blade_step1_done'))
        # Use the path Step 1 actually wrote to (stored after last Step 1 run).
        # Falls back to resolving fresh if Step 1 hasn't run yet.
        exr_dir_used = scene.get('_blade_exr_dir_used', _blade_exr_dir(scene))
        exr_exists   = os.path.isdir(exr_dir_used)

        # ── Status line ────────────────────────────────────────────────────
        if not has_video:
            layout.label(text="Set a background video first.", icon='ERROR')
        elif not step1_done:
            layout.label(text="Ready — run Step 1.", icon='INFO')
        elif not exr_exists:
            layout.label(text="EXR folder missing — re-run Step 1.", icon='ERROR')
        else:
            layout.label(text="Step 1 done — ready for Step 2.", icon='CHECKMARK')

        layout.separator(factor=0.5)

        # ── Step 1 / Step 2 Buttons ────────────────────────────────────────
        col = layout.column(align=True)
        col.scale_y = 1.4
        col.operator("blade.step1_render", icon='RENDER_STILL',
                     text="Step 1 — Render Blade EXR")

        if step1_done and exr_exists:
            col.separator(factor=0.3)
            col.operator("blade.step2_render", icon='RENDER_ANIMATION',
                         text="Step 2 — Composite & Render Final")

        # ── EXR Output Directory ───────────────────────────────────────────
        layout.separator(factor=0.6)
        box_dir = layout.box()
        row_dir = box_dir.row(align=True)
        row_dir.prop(scene, "blade_exr_dir", text="")
        box_dir.label(
            text="EXR frame output folder  (empty = temp folder)",
            icon='FILE_FOLDER',
        )

        # ── Combined / Reset ───────────────────────────────────────────────
        layout.separator(factor=0.8)
        box = layout.box()
        box.label(text="Quick Actions", icon='TOOL_SETTINGS')
        col2 = box.column(align=True)
        col2.scale_y = 1.3
        col2.operator("blade.render_combined", icon='RENDER_ANIMATION',
                      text="Render Combined")
        col2.separator(factor=0.3)
        col2.operator("blade.reset_render", icon='LOOP_BACK',
                      text="Reset Render")


class BLADE_PT_video_export(bpy.types.Panel):
    """Collapsible (nested under Render Process): output format settings."""
    bl_label       = "Video Export"
    bl_idname      = "BLADE_PT_video_export"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = _CATEGORY
    bl_parent_id   = "BLADE_PT_render"
    bl_options     = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        rd     = context.scene.render
        img    = rd.image_settings

        layout.label(text="Output Directory", icon='OUTPUT')
        layout.prop(rd, "filepath", text="")
        layout.separator(factor=0.5)
        layout.prop(img, "media_type", text="Media Type")

        if img.media_type == 'VIDEO':
            layout.prop(rd.ffmpeg, "format",               text="Container")
            layout.prop(img,       "color_mode",           text="Color")
            layout.prop(rd.ffmpeg, "codec",                text="Video Codec")
            layout.prop(rd.ffmpeg, "constant_rate_factor", text="Quality")
            layout.prop(rd.ffmpeg, "ffmpeg_preset",        text="Encoding Speed")
        elif img.media_type == 'MULTI_LAYER':
            layout.prop(img, "color_mode", text="Color")
            layout.prop(img, "exr_codec",  text="Codec")
        else:  # IMAGE
            layout.prop(img, "file_format", text="Format")
            layout.prop(img, "color_mode",  text="Color")
            if img.file_format == 'PNG':
                layout.prop(img, "compression", text="Compression")
            elif img.file_format in {'JPEG', 'JPEG2000', 'WEBP'}:
                layout.prop(img, "quality", text="Quality")


class BLADE_PT_mask(bpy.types.Panel):
    """Collapsible: mask creation and knife-tool workflow."""
    bl_label       = "Masking"
    bl_idname      = "BLADE_PT_mask"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = _CATEGORY
    bl_parent_id   = "BLADE_PT_main"
    bl_options     = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return len(_get_ls_list(context.scene)) > 0

    def draw(self, context):
        layout  = self.layout
        in_edit = (context.mode == 'EDIT_MESH')

        col = layout.column()
        col.scale_y = 1.5
        col.operator("blade.create_mask", icon='MOD_MASK')

        # ── Edit-mode only: knife toggle + step-by-step instructions ───────
        if in_edit:
            layout.separator()
            box = layout.box()

            try:
                current_tool = context.workspace.tools.from_space_view3d_mode('EDIT_MESH').idname
                is_knife     = (current_tool == 'builtin.knife')
            except Exception:
                is_knife = False

            row_ks = box.row()
            row_ks.scale_y = 1.3
            row_ks.operator(
                "blade.mask_shape",
                text    = "🔪 Knife Mode  (ON)" if is_knife else "☐ Select Mode  (click for Knife)",
                depress = is_knife,
            )

            box.separator(factor=0.3)
            col_info = box.column(align=True)
            col_info.scale_y = 0.78
            col_info.label(text="• Click to place cut points on the plane.")
            col_info.label(text="• Connect back to start to close the shape.")
            col_info.label(text="• Press Enter to execute the cut.")
            col_info.label(text="• Hold SHIFT and click all faces")
            col_info.label(text="  OUTSIDE of your shape.")
            col_info.label(text="• Press X to delete the selected faces.")
            col_info.label(text="• Press TAB to exit Edit Mode.")


# ─────────────────────────────────────────────────────────────────────────────
#  REGISTER / UNREGISTER
# ─────────────────────────────────────────────────────────────────────────────

# Tuple order matters:
#   - Operators before panels
#   - Parent panels before their children (Blender requires parents registered first)
_CLASSES = (
    BLADE_OT_save_preset,
    BLADE_OT_load_preset,
    BLADE_OT_confirm_delete_preset,
    BLADE_OT_browse_media,
    BLADE_OT_add_lightsaber,
    BLADE_OT_add_3d_lightsaber,
    BLADE_OT_snap_to_volume,
    BLADE_OT_create_mask,
    BLADE_OT_mask_shape,
    BLADE_OT_step1_render,
    BLADE_OT_step2_render,
    BLADE_OT_reset_render,
    BLADE_OT_render_combined,
    BLADE_PT_main,            # root — must come first
    BLADE_PT_blade,           # child of main
    BLADE_PT_animation,       # child of main
    BLADE_PT_render,          # child of main
    BLADE_PT_video_export,    # child of render — must come after BLADE_PT_render
    BLADE_PT_mask,            # child of main
)

_SCENE_PROPS = (
    'blade_active_ls',
    'blade_isolate',
    'blade_see_through',
    'blade_color',
    'blade_brightness',
    'blade_width',
    'blade_glow_strength',
    'blade_glow_size',
    'blade_saturation',
    'blade_preset_name',
    'blade_active_preset',
    'blade_video_path',
    'blade_framerate',
    'blade_snap_to_volume',
    'blade_exr_dir',
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.blade_active_ls = EnumProperty(
        name        = "Active Lightsaber",
        description = "Select which lightsaber to edit",
        items       = _ls_items,
        update      = _update_active_ls,
    )
    bpy.types.Scene.blade_isolate = BoolProperty(
        name        = "Isolate this Lightsaber",
        description = "Make all other lightsaber collections non-selectable",
        default     = False,
        update      = _update_isolate,
    )
    bpy.types.Scene.blade_see_through = BoolProperty(
        name        = "See Through",
        description = "Toggle solid X-ray view (on) vs rendered view (off)",
        default     = False,
        update      = _update_see_through,
    )
    bpy.types.Scene.blade_color = FloatVectorProperty(
        name        = "Color",
        description = "Blade emission color",
        subtype     = 'COLOR',
        size        = 4,
        min         = 0.0,
        max         = 1.0,
        default     = _DEFAULT_COLOR,
        update      = _update_color,
    )
    bpy.types.Scene.blade_brightness = FloatProperty(
        name        = "Brightness",
        description = "Emission strength",
        min         = 0.0,
        soft_max    = 100.0,
        default     = 70.0,
        update      = _update_brightness,
    )
    bpy.types.Scene.blade_width = FloatProperty(
        name        = "Width",
        description = "Blade X/Y scale — 5.0 = original size, range 0 to 10",
        min         = 0.0,
        soft_max    = 10.0,
        default     = 5.0,
        update      = _update_width,
    )
    bpy.types.Scene.blade_glow_strength = FloatProperty(
        name        = "Glow Strength",
        description = "Bloom node Strength",
        min         = 0.0,
        soft_max    = 5.0,
        default     = 0.08,
        update      = _update_glow_strength,
    )
    bpy.types.Scene.blade_glow_size = FloatProperty(
        name        = "Glow Size",
        description = "Bloom node Size",
        min         = 0.0,
        soft_max    = 1.0,
        default     = 0.086,
        update      = _update_glow_size,
    )
    bpy.types.Scene.blade_saturation = FloatProperty(
        name        = "Saturation",
        description = "Hue/Sat/Val node Saturation (compositor)",
        min         = 0.0,
        soft_max    = 4.0,
        default     = 2.0,
        update      = _update_saturation,
    )
    bpy.types.Scene.blade_preset_name = StringProperty(
        name        = "Preset Name",
        description = "Name for the new preset",
        default     = "",
    )
    bpy.types.Scene.blade_active_preset = EnumProperty(
        name        = "Preset",
        description = "Saved lightsaber presets",
        items       = _preset_items,
    )
    bpy.types.Scene.blade_video_path = StringProperty(
        name        = "Video / Image Path",
        description = "Path to background video or image file",
        subtype     = 'NONE',
        default     = "",
        update      = _update_video_path,
    )
    bpy.types.Scene.blade_framerate = EnumProperty(
        name        = "Framerate",
        description = "Set the project framerate",
        items       = [
            ('23.976', "23.976 fps", ""),
            ('24',     "24 fps",     ""),
            ('25',     "25 fps",     ""),
            ('29.97',  "29.97 fps",  ""),
            ('30',     "30 fps",     ""),
            ('50',     "50 fps",     ""),
            ('59.94',  "59.94 fps",  ""),
            ('60',     "60 fps",     ""),
            ('120',    "120 fps",    ""),
        ],
        default     = '30',
        update      = _update_framerate,
    )
    bpy.types.Scene.blade_snap_to_volume = BoolProperty(
        name        = "Snap to Volume",
        description = "Toggle Shrinkwrap mode between Snap to Volume and Nearest Surface",
        default     = False,
    )
    bpy.types.Scene.blade_exr_dir = StringProperty(
        name        = "EXR Output Directory",
        description = "Folder where blade EXR frames are saved. Leave empty to use the default temp folder",
        subtype     = 'DIR_PATH',
        default     = "",
    )




def unregister():
    for prop in _SCENE_PROPS:
        try:
            delattr(bpy.types.Scene, prop)
        except Exception:
            pass
    # Unregister in reverse registration order (children before parents)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
