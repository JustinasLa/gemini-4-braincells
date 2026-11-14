"""Shill or Chill verdict coin. Renders a 48-frame flip (rotation about X) with alpha.

frame 0  = SHILL face toward camera
frame 12 = edge-on
frame 24 = CHILL face toward camera (upright)
Usage: python coin.py <outdir> [res] [samples] [frames]
"""

import math
import os
import sys

import bpy
from mathutils import Vector

OUT = sys.argv[1] if len(sys.argv) > 1 else "out"
RES = int(sys.argv[2]) if len(sys.argv) > 2 else 400
SAMPLES = int(sys.argv[3]) if len(sys.argv) > 3 else 48
FRAMES = int(sys.argv[4]) if len(sys.argv) > 4 else 48
HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(
    HERE, "fonts", "Cinzel.ttf"
)  # OFL, Roman inscriptional capitals; download from google/fonts (ofl/cinzel)

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

R = 1.0  # coin radius
D = 0.17  # thickness


def mat_metal(name, color, rough=0.22):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1)
    b.inputs["Metallic"].default_value = 1.0
    b.inputs["Roughness"].default_value = rough
    return m


def mat_enamel(name, color, rough=0.12):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1)
    b.inputs["Metallic"].default_value = 0.0
    b.inputs["Roughness"].default_value = rough
    b.inputs["Coat Weight"].default_value = 1.0
    b.inputs["Coat Roughness"].default_value = 0.04
    # subtle radial sunburst texture (guilloche feel) via a wave texture into roughness/bump
    tc = nt.nodes.new("ShaderNodeTexCoord")
    wave = nt.nodes.new("ShaderNodeTexWave")
    wave.wave_type = "RINGS"
    wave.rings_direction = "SPHERICAL"
    nt.links.new(tc.outputs["Object"], wave.inputs["Vector"])
    wave.inputs["Scale"].default_value = 3.0
    wave.inputs["Distortion"].default_value = 0.0
    wave.inputs["Detail"].default_value = 0.0
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.035
    nt.links.new(wave.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
    return m


def mat_emit(name, strength=8.0, color=(1, 1, 1)):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    e = nt.nodes.new("ShaderNodeEmission")
    e.inputs["Color"].default_value = (*color, 1)
    e.inputs["Strength"].default_value = strength
    nt.links.new(e.outputs[0], out.inputs[0])
    return m


def mat_aged(name, color, rough=0.32):
    """Struck metal: darker in the crevices (AO), scuffed roughness."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    b.inputs["Metallic"].default_value = 1.0
    ao = nt.nodes.new("ShaderNodeAmbientOcclusion")
    ao.inputs["Distance"].default_value = 0.06
    mix = nt.nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.inputs[6].default_value = (color[0] * 0.28, color[1] * 0.22, color[2] * 0.16, 1)
    mix.inputs[7].default_value = (*color, 1)
    nt.links.new(ao.outputs["AO"], mix.inputs[0])
    nt.links.new(mix.outputs[2], b.inputs["Base Color"])
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 60.0
    ramp = nt.nodes.new("ShaderNodeMapRange")
    ramp.inputs["To Min"].default_value = rough - 0.1
    ramp.inputs["To Max"].default_value = rough + 0.15
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Value"])
    nt.links.new(ramp.outputs["Result"], b.inputs["Roughness"])
    return m


def mat_lacquer(name, color, rough=0.5, var=0.25):
    """Hand-applied pigment in the field: cinnabar or verdigris, mottled."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    b.inputs["Metallic"].default_value = 0.0
    b.inputs["Roughness"].default_value = rough
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 7.0
    noise.inputs["Detail"].default_value = 8.0
    mix = nt.nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.inputs[6].default_value = (
        color[0] * (1 - var),
        color[1] * (1 - var),
        color[2] * (1 - var),
        1,
    )
    mix.inputs[7].default_value = (
        min(1, color[0] * (1 + var)),
        min(1, color[1] * (1 + var)),
        min(1, color[2] * (1 + var)),
        1,
    )
    nt.links.new(noise.outputs["Fac"], mix.inputs[0])
    nt.links.new(mix.outputs[2], b.inputs["Base Color"])
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.04
    nt.links.new(noise.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])
    return m


GOLD = mat_aged("gold", (0.95, 0.70, 0.36), 0.3)
GOLD_DARK = mat_aged("gold_dark", (0.72, 0.5, 0.24), 0.4)
RED = mat_lacquer("cinnabar", (0.62, 0.08, 0.04), 0.42, 0.3)
MINT = mat_lacquer("verdigris", (0.16, 0.45, 0.36), 0.62, 0.35)
INK = mat_enamel("enamel_ink", (0.02, 0.02, 0.03), 0.2)

root = bpy.data.objects.new("coin_root", None)
scene.collection.objects.link(root)


def link(obj, parent=root):
    if obj.name not in scene.collection.objects:
        try:
            scene.collection.objects.link(obj)
        except RuntimeError:
            pass
    obj.parent = parent
    return obj


def add_bevel(obj, width, segs=4):
    mod = obj.modifiers.new("bevel", "BEVEL")
    mod.width = width
    mod.segments = segs
    mod.limit_method = "ANGLE"
    return mod


def cyl(name, radius, depth, y, mat, verts=160, bevel=0.0):
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=verts, radius=radius, depth=depth, location=(0, y, 0), rotation=(math.pi / 2, 0, 0)
    )
    o = bpy.context.active_object
    o.name = name
    o.data.materials.append(mat)
    if bevel:
        add_bevel(o, bevel)
    bpy.ops.object.shade_smooth()
    bpy.context.scene.collection.objects.unlink(o) if False else None
    o.parent = root
    return o


# body: brass disc with bevelled edge
body = cyl("body", R, D, 0, GOLD, bevel=0.035)

# reeded edge: thin ridges around the rim
n_ridges = 120
for i in range(n_ridges):
    a = 2 * math.pi * i / n_ridges
    bpy.ops.mesh.primitive_cube_add(
        size=1, location=(math.cos(a) * (R + 0.002), 0, math.sin(a) * (R + 0.002))
    )
    o = bpy.context.active_object
    o.scale = (0.012, D * 0.78, 0.018)
    o.rotation_euler = (0, -a, 0)
    o.data.materials.append(GOLD_DARK)
    o.parent = root

# raised rim ring on both faces (torus)
for side in (-1, 1):
    bpy.ops.mesh.primitive_torus_add(
        major_radius=R * 0.9,
        minor_radius=0.035,
        major_segments=160,
        minor_segments=16,
        location=(0, side * D / 2, 0),
        rotation=(math.pi / 2, 0, 0),
    )
    t = bpy.context.active_object
    t.data.materials.append(GOLD)
    bpy.ops.object.shade_smooth()
    t.parent = root

# enamel field on each face (slightly recessed disc)
front_field = cyl("field_front", R * 0.86, 0.02, -D / 2 - 0.002, RED, bevel=0.006)
back_field = cyl("field_back", R * 0.86, 0.02, D / 2 + 0.002, MINT, bevel=0.006)


# inscription around the rim on each face, Roman capitals bent along a circle
def ring_text(body_text, y, flip):
    bpy.ops.curve.primitive_bezier_circle_add(radius=0.735, location=(0, 0, 0))
    circ = bpy.context.active_object
    bpy.ops.object.text_add(location=(0, 0, 0))
    t = bpy.context.active_object
    t.data.body = body_text
    if os.path.exists(FONT):
        t.data.font = bpy.data.fonts.load(FONT)
    t.data.size = 0.13
    t.data.space_character = 1.05
    t.data.bevel_depth = 0.003
    t.data.align_x = "CENTER"
    t.data.extrude = 0.008
    t.data.materials.append(GOLD)
    t.data.follow_curve = circ
    bpy.context.view_layer.update()
    bpy.ops.object.select_all(action="DESELECT")
    t.select_set(True)
    bpy.context.view_layer.objects.active = t
    bpy.ops.object.convert(target="MESH")
    t = bpy.context.active_object
    circ.hide_render = True
    # text follows the circle in XY; stand it up onto the coin face (XZ plane)
    t.rotation_euler = (math.pi / 2, 0, 0)
    t.location = (0, y, 0)
    holder = bpy.data.objects.new("ring_holder", None)
    scene.collection.objects.link(holder)
    t.parent = holder
    if flip:
        holder.rotation_euler = (math.pi, 0, 0)
    holder.parent = root
    return holder


ring_text("SHILL OR CHILL · FACTA NON VERBA · MMXXVI ·", -D / 2 - 0.006, False)
ring_text("SHILL OR CHILL · FACTA NON VERBA · MMXXVI ·", -D / 2 - 0.006, True)


def text_obj(body_text, size, z, y, mat, extrude=0.018):
    bpy.ops.object.text_add(location=(0, y, z), rotation=(math.pi / 2, 0, 0))
    t = bpy.context.active_object
    t.data.body = body_text
    if os.path.exists(FONT):
        t.data.font = bpy.data.fonts.load(FONT)
    t.data.size = size
    t.data.align_x = "CENTER"
    t.data.align_y = "CENTER"
    t.data.extrude = extrude
    t.data.bevel_depth = 0.009
    t.data.bevel_resolution = 2
    t.data.materials.append(mat)
    bpy.ops.object.convert(target="MESH")
    t = bpy.context.active_object
    bpy.ops.object.shade_smooth()
    return t


FACE_Y = -D / 2 - 0.03  # just in front of the front face


def make_flag(y):
    """Embossed flag emblem: pole + waving pennant, built for the front face."""
    parts = []
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=24, radius=0.03, depth=0.78, location=(-0.24, y, 0.2), rotation=(0, 0, 0)
    )
    pole = bpy.context.active_object
    pole.data.materials.append(GOLD)
    bpy.ops.object.shade_smooth()
    parts.append(pole)
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.055, location=(-0.24, y, 0.6))
    knob = bpy.context.active_object
    knob.data.materials.append(GOLD)
    bpy.ops.object.shade_smooth()
    parts.append(knob)
    # pennant: subdivided plane with a sine wave, solidified
    bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 0, 0))
    p = bpy.context.active_object
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.subdivide(number_cuts=24)
    bpy.ops.object.mode_set(mode="OBJECT")
    for v in p.data.vertices:
        x, zz = v.co.x + 0.5, v.co.y  # x in 0..1
        v.co = Vector(
            (x * 0.56, 0.05 * math.sin(x * 7.0) * x, zz * 0.36 + 0.03 * math.sin(x * 6.0))
        )
    p.location = (-0.21, y, 0.39)
    sol = p.modifiers.new("solid", "SOLIDIFY")
    sol.thickness = 0.03
    add_bevel(p, 0.008, 2)
    p.data.materials.append(GOLD)
    bpy.ops.object.shade_smooth()
    parts.append(p)
    return parts


def make_shades(y):
    """Embossed sunglasses emblem."""
    parts = []
    for sx in (-1, 1):
        bpy.ops.mesh.primitive_cylinder_add(
            vertices=64,
            radius=0.17,
            depth=0.05,
            location=(sx * 0.255, y, 0.26),
            rotation=(math.pi / 2, 0, 0),
        )
        lens = bpy.context.active_object
        lens.scale = (1.2, 0.78, 1)  # wide lens
        lens.rotation_euler = (math.pi / 2, sx * math.radians(-12), 0)
        add_bevel(lens, 0.02, 3)
        lens.data.materials.append(INK)
        bpy.ops.object.shade_smooth()
        parts.append(lens)
        bpy.ops.mesh.primitive_torus_add(
            major_radius=0.175,
            minor_radius=0.026,
            location=(sx * 0.255, y, 0.26),
            rotation=(math.pi / 2, 0, 0),
        )
        fr = bpy.context.active_object
        fr.scale = (1.2, 0.78, 1)
        fr.rotation_euler = (math.pi / 2, sx * math.radians(-12), 0)
        fr.data.materials.append(GOLD)
        bpy.ops.object.shade_smooth()
        parts.append(fr)
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=24, radius=0.024, depth=0.12, location=(0, y, 0.3), rotation=(0, math.pi / 2, 0)
    )
    br = bpy.context.active_object
    br.data.materials.append(GOLD)
    bpy.ops.object.shade_smooth()
    parts.append(br)
    return parts


# FRONT: flag + SHILL
front_parts = make_flag(FACE_Y) + [text_obj("SHILL", 0.34, -0.3, FACE_Y, GOLD, 0.04)]
for o in front_parts:
    o.parent = root

# BACK: shades + CHILL, built as if on the front, then flipped 180deg about X onto the back
back_group = bpy.data.objects.new("back_group", None)
scene.collection.objects.link(back_group)
back_parts = make_shades(FACE_Y) + [text_obj("CHILL", 0.34, -0.3, FACE_Y, GOLD, 0.04)]
for o in back_parts:
    o.parent = back_group
back_group.rotation_euler = (math.pi, 0, 0)
back_group.parent = root

# ---- animation: rotate about X, fixed slight tilt so the rim reads as 3D
spin = bpy.data.objects.new("spin", None)
scene.collection.objects.link(spin)
root.parent = spin
root.rotation_euler = (0, 0, math.radians(-14))  # tilt
spin.rotation_euler = (math.radians(8), 0, 0)
scene.frame_start = 0
scene.frame_end = FRAMES - 1


def set_frame_rot(f):
    spin.rotation_euler = (math.radians(8) + 2 * math.pi * f / FRAMES, 0, 0)


# ---- camera
cam_data = bpy.data.cameras.new("cam")
cam_data.lens = 85
cam = bpy.data.objects.new("cam", cam_data)
scene.collection.objects.link(cam)
cam.location = (0, -6.5, 0.0)
cam.rotation_euler = (math.pi / 2, 0, 0)
scene.camera = cam


# ---- studio lighting: softboxes the metal reflects, invisible to camera
def softbox(name, loc, size, strength, color=(1, 1, 1)):
    bpy.ops.mesh.primitive_plane_add(size=1, location=loc)
    p = bpy.context.active_object
    p.name = name
    p.scale = size
    direction = Vector((0, 0, 0)) - Vector(loc)
    p.rotation_euler = direction.to_track_quat("Z", "Y").to_euler()
    p.data.materials.append(mat_emit(name + "_m", strength, color))
    p.visible_camera = False
    return p


softbox("key", (-3.5, -4.5, 3.8), (3.2, 2.2, 1), 9.0, (1.0, 0.96, 0.9))
softbox("fill", (4.5, -3.0, -1.0), (2.5, 3.5, 1), 3.0, (0.75, 0.85, 1.0))
softbox("rim", (0.5, 4.0, 3.5), (5, 1.2, 1), 10.0, (1.0, 0.9, 0.8))
softbox("top", (0, -1.5, 6), (6, 2, 1), 4.0)
softbox("front", (0.6, -9.5, 1.2), (7, 5, 1), 2.2, (1.0, 0.93, 0.82))
softbox("floor", (0, -3, -5), (8, 3, 1), 1.2, (1.0, 0.75, 0.55))

world = bpy.data.worlds.new("w")
scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes["Background"]
bg.inputs["Color"].default_value = (0.02, 0.022, 0.03, 1)
bg.inputs["Strength"].default_value = 1.0

# ---- render settings
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = SAMPLES
scene.cycles.use_denoising = True
scene.render.threads_mode = "FIXED"
scene.render.threads = 6
scene.render.film_transparent = True
scene.render.resolution_x = RES
scene.render.resolution_y = RES
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA"
scene.view_settings.view_transform = "AgX"
scene.view_settings.look = "AgX - Medium High Contrast"

os.makedirs(OUT, exist_ok=True)
only = os.environ.get("FRAMES_ONLY")
frames = [int(x) for x in only.split(",")] if only else range(FRAMES)
for f in frames:
    set_frame_rot(f)
    scene.render.filepath = os.path.join(OUT, f"f{f:03d}.png")
    bpy.ops.render.render(write_still=True)
    print("rendered", f, flush=True)
