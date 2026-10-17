"""Minimal fake of the parts of Blender's `bpy` API that remote/render.py and
the bootstrap GPU probe touch. Behaviour is steered by env vars:
  FAKE_GPU          "optix" (default) | "cuda" | "none"
  FAKE_FAIL_FRAMES  comma list of frames whose render raises
  FAKE_MISSING      "1" adds an image whose file does not exist
  FAKE_FRAME_SECONDS  sleep per frame
"""
import os
import struct
import time
import zlib

BLEND_PATH = os.environ.get("FAKE_BLEND_PATH", "")


class Collection(list):
    def __getitem__(self, key):
        if isinstance(key, str):
            for x in self:
                if x.name == key:
                    return x
            raise KeyError(key)
        return list.__getitem__(self, key)


class NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _app:
    version_string = "5.2.2 (fake)"


app = _app()


class Device(NS):
    pass


class CyclesPrefs:
    def __init__(self):
        self._type = "NONE"
        self.devices = Collection()

    @property
    def compute_device_type(self):
        return self._type

    @compute_device_type.setter
    def compute_device_type(self, v):
        if v not in ("NONE", "CUDA", "OPTIX", "HIP", "METAL", "ONEAPI"):
            raise TypeError("enum %r not found" % v)
        self._type = v

    def get_devices(self):
        gpu = os.environ.get("FAKE_GPU", "optix").lower()
        self.devices = Collection([Device(name="CPU", type="CPU", use=True)])
        if gpu in ("optix", "cuda"):
            self.devices.append(Device(name="NVIDIA GeForce RTX 3060", type="CUDA", use=False))
        if gpu == "optix":
            self.devices.append(Device(name="NVIDIA GeForce RTX 3060", type="OPTIX", use=False))
        return self.devices


_cprefs = CyclesPrefs()


class context:
    preferences = NS(addons={"cycles": NS(preferences=_cprefs)})
    window = None
    scene = None


def _png(path, w=4, h=4):
    raw = b"".join(b"\x00" + b"\x80\x40\x20" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(data)


class Image(NS):
    def save_render(self, filepath, scene=None):
        fmt = (scene or context.scene).render.image_settings.file_format
        if fmt == "PNG":
            _png(filepath)
        elif fmt.startswith("OPEN_EXR"):
            open(filepath, "wb").write(b"\x76\x2f\x31\x01" + b"\x00" * 64)
        elif fmt == "JPEG":
            open(filepath, "wb").write(b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9")
        else:
            open(filepath, "wb").write(b"II*\x00" + b"\x00" * 64)


class ImageSettings:
    def __init__(self):
        self.media_type = "IMAGE"
        self._fmt = "PNG"

    @property
    def file_format(self):
        return self._fmt

    @file_format.setter
    def file_format(self, v):
        if self.media_type == "VIDEO" and v != "FFMPEG":
            raise TypeError("format %s not valid for video media type" % v)
        self._fmt = v


class Scene(NS):
    def frame_set(self, f):
        self.frame_current = f


_scene = Scene(name="Scene", frame_current=1, frame_start=1, frame_end=5, frame_step=1, camera=None,
               render=NS(engine="BLENDER_EEVEE", resolution_x=1920, resolution_y=1080, resolution_percentage=100,
                         image_settings=ImageSettings()),
               cycles=NS(device="CPU", samples=4096))
if os.environ.get("FAKE_VIDEO_OUTPUT"):
    _scene.render.image_settings.media_type = "VIDEO"
    _scene.render.image_settings._fmt = "FFMPEG"
context.scene = _scene

_images = Collection([Image(name="Render Result", source="VIEWER", filepath="", packed_file=None)])
if os.environ.get("FAKE_MISSING") == "1":
    _images.append(Image(name="wood.jpg", source="FILE", filepath="//textures/wood.jpg", packed_file=None))


class data:
    scenes = Collection([_scene])
    objects = Collection([NS(name="Camera")])
    images = _images
    libraries = Collection()
    volumes = Collection()
    cache_files = Collection()
    movieclips = Collection()
    sounds = Collection()
    fonts = Collection([NS(name="Bfont", filepath="<builtin>", packed_file=None)])


class _render_ops:
    @staticmethod
    def render(write_still=False, scene=None):
        fails = [int(x) for x in os.environ.get("FAKE_FAIL_FRAMES", "").split(",") if x]
        time.sleep(float(os.environ.get("FAKE_FRAME_SECONDS", "0")))
        if _scene.frame_current in fails:
            raise RuntimeError("CUDA error: Out of memory in cuMemAlloc (fake)")
        if _scene.render.engine != "CYCLES":
            raise RuntimeError("engine not set to CYCLES")
        return {"FINISHED"}


class ops:
    render = _render_ops


class path:
    @staticmethod
    def abspath(p, library=None):
        if p.startswith("//"):
            return os.path.join(os.path.dirname(BLEND_PATH), p[2:])
        return p
