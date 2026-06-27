bl_info = {
    "name": "Multitouch Viewport Navigation",
    "author": "Claude",
    "version": (1, 7, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Touch",
    "description": "Pan, rotate, zoom the 3D viewport and scroll any UI/menu with multitouch (Windows).",
    "category": "3D View",
}

import bpy
import ctypes
import math
import sys
import time
from ctypes import wintypes
from mathutils import Vector, Quaternion, Matrix

IS_WINDOWS = sys.platform.startswith("win")

# Module-level constant (used in scroll-mode pixel-to-wheel conversion).
WHEEL_DELTA = 120
SCROLL_PIXELS_PER_NOTCH = 30.0  # how many vertical pixels of drag = one wheel notch

# Signature stamped into dwExtraInfo of OUR injected mouse events, so the input
# hook can recognise them (via GetMessageExtraInfo) and let them through while
# still suppressing Windows' own touch-synthesised clicks.
_INJECT_SIG = 0x5113CA75

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    # Pointer / touch constants
    PT_TOUCH = 2
    POINTER_FLAG_NEW = 0x00000001
    POINTER_FLAG_INRANGE = 0x00000002
    POINTER_FLAG_INCONTACT = 0x00000004
    POINTER_FLAG_FIRSTBUTTON = 0x00000010
    POINTER_FLAG_PRIMARY = 0x00002000
    POINTER_FLAG_UP = 0x00040000
    POINTER_FLAG_DOWN = 0x00010000
    POINTER_FLAG_UPDATE = 0x00020000

    # Wheel / window-walking constants
    WM_MOUSEWHEEL = 0x020A
    GA_ROOT = 2

    # mouse_event flags for injecting a real held left-button press.
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP   = 0x0004

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class POINTER_INFO(ctypes.Structure):
        _fields_ = [
            ("pointerType", wintypes.DWORD),
            ("pointerId", wintypes.UINT),
            ("frameId", wintypes.UINT),
            ("pointerFlags", wintypes.DWORD),
            ("sourceDevice", wintypes.HANDLE),
            ("hwndTarget", wintypes.HWND),
            ("ptPixelLocation", POINT),
            ("ptHimetricLocation", POINT),
            ("ptPixelLocationRaw", POINT),
            ("ptHimetricLocationRaw", POINT),
            ("dwTime", wintypes.DWORD),
            ("historyCount", wintypes.UINT),
            ("InputData", ctypes.c_int32),
            ("dwKeyStates", wintypes.DWORD),
            ("PerformanceCount", ctypes.c_uint64),
            ("ButtonChangeType", ctypes.c_int),
        ]

    class POINTER_TOUCH_INFO(ctypes.Structure):
        _fields_ = [
            ("pointerInfo", POINTER_INFO),
            ("touchFlags", wintypes.DWORD),
            ("touchMask", wintypes.DWORD),
            ("rcContact", RECT),
            ("rcContactRaw", RECT),
            ("orientation", wintypes.DWORD),
            ("pressure", wintypes.DWORD),
        ]

    try:
        user32.GetPointerType.argtypes = [wintypes.UINT, ctypes.POINTER(wintypes.DWORD)]
        user32.GetPointerType.restype = wintypes.BOOL

        user32.GetPointerInfo.argtypes = [wintypes.UINT, ctypes.POINTER(POINTER_INFO)]
        user32.GetPointerInfo.restype = wintypes.BOOL

        user32.GetPointerTouchInfo.argtypes = [wintypes.UINT, ctypes.POINTER(POINTER_TOUCH_INFO)]
        user32.GetPointerTouchInfo.restype = wintypes.BOOL

        user32.GetPointerFrameTouchInfo.argtypes = [
            wintypes.UINT, ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(POINTER_TOUCH_INFO)
        ]
        user32.GetPointerFrameTouchInfo.restype = wintypes.BOOL

        user32.EnableMouseInPointer.argtypes = [wintypes.BOOL]
        user32.EnableMouseInPointer.restype = wintypes.BOOL

        # Window-walking + message-posting for synthetic wheel events.
        user32.WindowFromPoint.argtypes = [POINT]
        user32.WindowFromPoint.restype = wintypes.HWND

        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND

        user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL

        # Hook plumbing
        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = wintypes.LPARAM

        user32.GetCurrentThreadId = ctypes.windll.kernel32.GetCurrentThreadId
        user32.GetCurrentThreadId.restype = wintypes.DWORD

        user32.GetMessageExtraInfo.argtypes = []
        user32.GetMessageExtraInfo.restype = wintypes.LPARAM

        # For injecting a real held left-button press during button/gizmo drags.
        user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        user32.SetCursorPos.restype = wintypes.BOOL
        user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.DWORD,
                                       ctypes.POINTER(ctypes.c_ulong)]
        user32.mouse_event.restype = None

        TOUCH_API_AVAILABLE = True
    except (AttributeError, OSError):
        TOUCH_API_AVAILABLE = False

    WH_GETMESSAGE = 3
    PM_NOREMOVE = 0
    PM_REMOVE = 1

    WM_POINTERUPDATE = 0x0245
    WM_POINTERDOWN = 0x0246
    WM_POINTERUP = 0x0247
    WM_POINTERENTER = 0x0249
    WM_POINTERLEAVE = 0x024A
    WM_POINTERCAPTURECHANGED = 0x024C

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM),
            ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD),
            ("pt", POINT),
        ]
else:
    TOUCH_API_AVAILABLE = False


# ---------------------------------------------------------------------------
# Gesture state
# ---------------------------------------------------------------------------
class TouchPoint:
    __slots__ = ("id", "x", "y", "down", "last_update")
    def __init__(self, pid, x, y):
        self.id = pid
        self.x = float(x)
        self.y = float(y)
        self.down = True
        self.last_update = time.time()


class GestureState:
    """Tracks active touch contacts and derives pan/rotate/zoom/scroll deltas."""

    def __init__(self):
        self.points = {}            # pid -> TouchPoint
        self.prev_centroid = None
        self.prev_spread = None
        self.prev_angle = None
        self.mode = None            # 'view3d' | 'scroll' | None — locked at gesture start
        self.view3d_target = None   # (area, region, rv3d) when mode == 'view3d'
        self.wheel_accum = 0.0      # accumulated sub-notch wheel delta in scroll mode
        self.last_inject_xy = None  # last (x, y) where we injected a held click

    def reset_baseline(self):
        self.prev_centroid = self._centroid()
        self.prev_spread = self._spread()
        self.prev_angle = self._angle()

    def begin_gesture(self, mode, view3d_target=None):
        self.mode = mode
        self.view3d_target = view3d_target

    def end_gesture(self):
        self.mode = None
        self.view3d_target = None
        self.wheel_accum = 0.0

    def update_point(self, pid, x, y, is_down, is_up):
        if is_up:
            self.points.pop(pid, None)
            # Recompute baselines so the remaining fingers don't cause a jump.
            self.reset_baseline()
            return
        if pid not in self.points or is_down:
            self.points[pid] = TouchPoint(pid, x, y)
            self.reset_baseline()
        else:
            p = self.points[pid]
            p.x = float(x)
            p.y = float(y)
            p.last_update = time.time()

    def _centroid(self):
        if not self.points:
            return None
        n = len(self.points)
        sx = sum(p.x for p in self.points.values()) / n
        sy = sum(p.y for p in self.points.values()) / n
        return (sx, sy)

    def _spread(self):
        if len(self.points) < 2:
            return None
        pts = list(self.points.values())
        cx, cy = self._centroid()
        return sum(math.hypot(p.x - cx, p.y - cy) for p in pts) / len(pts)

    def _angle(self):
        if len(self.points) < 2:
            return None
        pts = list(self.points.values())
        a, b = pts[0], pts[1]
        return math.atan2(b.y - a.y, b.x - a.x)

    def consume_delta(self):
        """Return (pan_dx, pan_dy, zoom_factor, rotate_dx, rotate_dy, n_fingers).
        Update baselines so deltas are *incremental*. Screen pixels, y-down."""
        n = len(self.points)
        if n == 0:
            return None

        cur_c = self._centroid()
        cur_s = self._spread()
        cur_a = self._angle()

        pan_dx = pan_dy = 0.0
        zoom = 1.0
        rot_dx = rot_dy = 0.0

        if self.prev_centroid is not None and cur_c is not None:
            pan_dx = cur_c[0] - self.prev_centroid[0]
            pan_dy = cur_c[1] - self.prev_centroid[1]

        if self.prev_spread and cur_s and self.prev_spread > 1e-3:
            zoom = cur_s / self.prev_spread

        # One-finger drag -> orbit (view3d) or vertical scroll (else).
        if n == 1:
            rot_dx = pan_dx
            rot_dy = pan_dy
            pan_dx = pan_dy = 0.0

        self.prev_centroid = cur_c
        self.prev_spread = cur_s
        self.prev_angle = cur_a

        return pan_dx, pan_dy, zoom, rot_dx, rot_dy, n


# ---------------------------------------------------------------------------
# Area / region helpers
# ---------------------------------------------------------------------------
def _screen_to_local(window, screen_x, screen_y):
    """Windows screen coords (y-down) -> Blender window-local (y-up)."""
    wx, wy = window.x, window.y
    win_h = window.height
    local_x = screen_x - wx
    local_y = (wy + win_h) - screen_y
    return local_x, local_y


# Region type priority for hit-testing. WINDOW first so that e.g. the View3D
# main region wins over an overlapping header strip.
# Overlapping side/header regions are checked BEFORE 'WINDOW'. In Blender the
# TOOLS (left toolbar), UI (right sidebar/N-panel), HEADER, etc. are drawn on
# top of the WINDOW region and share its pixel bounds, so if WINDOW is tested
# first every toolbar touch wrongly resolves to the viewport (orbit). Listing
# WINDOW LAST means a touch only counts as 'viewport' when it's in the actual
# empty 3D area, not over a panel/toolbar/header drawn above it.
_REGION_PRIORITY = (
    'UI', 'TOOLS', 'TOOL_PROPS', 'CHANNELS', 'NAVIGATION_BAR',
    'EXECUTE', 'HEADER', 'FOOTER', 'PREVIEW', 'WINDOW',
)


def find_area_under_cursor(screen_x, screen_y, window):
    """Find ANY Blender area + region under the given Windows screen point.
    Returns (area, region, (local_x_in_region, local_y_in_region)) or None."""
    local_x, local_y = _screen_to_local(window, screen_x, screen_y)

    for area in window.screen.areas:
        if not (area.x <= local_x <= area.x + area.width and
                area.y <= local_y <= area.y + area.height):
            continue
        for rtype in _REGION_PRIORITY:
            for region in area.regions:
                if region.type != rtype:
                    continue
                if (region.x <= local_x <= region.x + region.width and
                        region.y <= local_y <= region.y + region.height):
                    return area, region, (local_x - region.x, local_y - region.y)
    return None


# The navigate gizmo cluster (axis ball + zoom/pan/camera/persp/grid mini
# buttons) sits at the top-right of the VISIBLE viewport. When the N-panel
# (UI region) or any other side region is open it overlaps the WINDOW region,
# so the gizmo is drawn shifted LEFT by that sidebar's width. We account for
# that here, and use a tall/wide enough zone to cover every stacked button.
def _in_navigate_gizmo_zone(area, region, local_x, local_y):
    """True if (local_x, local_y) falls in the navigation gizmo cluster of a
    VIEW_3D WINDOW region, accounting for an overlapping right sidebar."""
    try:
        prefs = bpy.context.preferences
        if not prefs.view.show_navigate_ui:
            return False
        ui_scale = prefs.system.ui_scale
    except Exception:
        ui_scale = 1.0

    # Find the width of any region that overlaps the WINDOW region on the RIGHT
    # (the N-panel/UI sidebar). The gizmo is drawn to the left of it.
    right_inset = 0
    try:
        for r in area.regions:
            if r.type == 'UI' and r.width > 1:
                # UI sidebar is anchored to the right edge of the area.
                right_inset = r.width
                break
    except Exception:
        right_inset = 0

    # Zone: wide enough for the mini buttons, tall enough for ball + 5 buttons.
    zone_w = 95.0 * ui_scale
    zone_h = 460.0 * ui_scale

    rx = local_x - region.x
    ry = local_y - region.y
    # Distance from the right edge of the *visible* viewport (sidebar excluded).
    from_right = (region.width - right_inset) - rx
    from_top = region.height - ry
    # A little negative tolerance so touches right at the gizmo's left/edge count.
    return (-8 <= from_right <= zone_w) and (-8 <= from_top <= zone_h)



# ---------------------------------------------------------------------------
# Viewport / wheel actuators
# ---------------------------------------------------------------------------
def apply_gesture(rv3d, region, pan_dx_px, pan_dy_px, zoom_factor,
                  rot_dx_px, rot_dy_px):
    """Apply pan/rotate/zoom deltas to a RegionView3D.
    Pixel deltas use top-down Windows convention; we flip Y here."""
    if rv3d is None:
        return

    if abs(zoom_factor - 1.0) > 1e-4:
        new_dist = rv3d.view_distance / max(zoom_factor, 1e-3)
        rv3d.view_distance = max(0.001, min(new_dist, 1.0e6))

    if pan_dx_px or pan_dy_px:
        rh = region.height if region.height else 1
        scale = rv3d.view_distance / rh
        vm = rv3d.view_matrix
        right = Vector((vm[0][0], vm[0][1], vm[0][2]))
        up = Vector((vm[1][0], vm[1][1], vm[1][2]))
        delta = (-right * pan_dx_px + up * pan_dy_px) * scale * 2.0
        rv3d.view_location = rv3d.view_location + delta

    if rot_dx_px or rot_dy_px:
        rw = region.width if region.width else 1
        rh = region.height if region.height else 1
        az = -rot_dx_px / rw * math.pi
        el = -rot_dy_px / rh * math.pi
        q_world = rv3d.view_rotation
        q_az = Quaternion((0.0, 0.0, 1.0), az)
        vm = q_world.to_matrix()
        right = Vector((vm[0][0], vm[1][0], vm[2][0]))
        q_el = Quaternion(right, el)
        rv3d.view_rotation = (q_az @ q_el @ q_world).normalized()


def post_mouse_wheel(screen_x, screen_y, delta_signed, modifiers=0):
    """Post WM_MOUSEWHEEL to the top-level window under (screen_x, screen_y).
    `delta_signed` is in WHEEL_DELTA units (positive = scroll up / forward).
    LPARAM screen coords let Blender's GHOST route to the right region/popup
    without moving the cursor (compare with SendInput, which requires SetCursorPos)."""
    if not (IS_WINDOWS and TOUCH_API_AVAILABLE):
        return
    # Move cursor to touch point first so Blender routes the wheel to
    # the panel under the finger, not whichever was last focused.
    user32.SetCursorPos(int(screen_x), int(screen_y))
    pt = POINT(int(screen_x), int(screen_y))
    hwnd = user32.WindowFromPoint(pt)
    if not hwnd:
        return
    top = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
    # WPARAM: HIWORD = signed 16-bit wheel delta, LOWORD = modifier flags.
    delta_lo = int(delta_signed) & 0xFFFF
    wparam = ((delta_lo << 16) | (int(modifiers) & 0xFFFF)) & 0xFFFFFFFF
    # LPARAM: HIWORD = screen y, LOWORD = screen x.
    lparam = ((int(screen_y) & 0xFFFF) << 16) | (int(screen_x) & 0xFFFF)
    user32.PostMessageW(top, WM_MOUSEWHEEL, wparam, lparam)


def _inject_left_down(screen_x, screen_y):
    """Move the real cursor to (screen_x, screen_y) and press the left mouse
    button down, leaving it held. Used to start a hold-drag on a gizmo/button."""
    if not (IS_WINDOWS and TOUCH_API_AVAILABLE):
        return
    user32.SetCursorPos(int(screen_x), int(screen_y))
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, None)


def _inject_left_down(screen_x, screen_y):
    """Move the real cursor to the point and press+hold the left mouse button.
    We drive the click ourselves because the touch-synth button-down is
    suppressed by our hook before the gesture is classified as passthrough."""
    if not (IS_WINDOWS and TOUCH_API_AVAILABLE):
        return
    user32.SetCursorPos(int(screen_x), int(screen_y))
    extra = ctypes.c_ulong(_INJECT_SIG)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, ctypes.byref(extra))


def _inject_move(screen_x, screen_y):
    """Move the real cursor while the left button is held, so Blender sees a
    drag that follows the finger."""
    if not (IS_WINDOWS and TOUCH_API_AVAILABLE):
        return
    user32.SetCursorPos(int(screen_x), int(screen_y))


def _inject_left_up(screen_x, screen_y):
    """Release the held left mouse button at the given screen point."""
    if not (IS_WINDOWS and TOUCH_API_AVAILABLE):
        return
    user32.SetCursorPos(int(screen_x), int(screen_y))
    extra = ctypes.c_ulong(_INJECT_SIG)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, ctypes.byref(extra))


def _inject_left_up(screen_x, screen_y):
    """Release the held left mouse button at (screen_x, screen_y)."""
    if not (IS_WINDOWS and TOUCH_API_AVAILABLE):
        return
    user32.SetCursorPos(int(screen_x), int(screen_y))
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, None)


# ---------------------------------------------------------------------------
# WH_GETMESSAGE hook — captures WM_POINTER* on the GUI thread
# ---------------------------------------------------------------------------
class TouchHook:
    def __init__(self):
        self.installed = False
        self.hook_handle = None
        self.events = []
        self._proc = None  # keep alive

    def install(self):
        if not (IS_WINDOWS and TOUCH_API_AVAILABLE):
            return False
        if self.installed:
            return True

        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM)

        # Count of fingers currently touching. Shared between the filter
        # and the capture logic inside hook_proc below.
        touch_count = [0]
        # When True, the current gesture is over a button/toolbar/gizmo, so
        # the filter lets synthesised clicks through (don't suppress them).
        # The modal operator flips self._hook.passthrough[0] as gestures begin.
        self.passthrough = [False]
        _passthrough = self.passthrough

        # Synthesised mouse messages Windows generates from touch contacts.
        # We replace these with WM_NULL (0x0000) while any finger is down so
        # Blender never sees the fake clicks/drags alongside our real gestures.
        _SYNTH = frozenset({
            # WM_MOUSEMOVE intentionally excluded: letting cursor-move
            # messages through so Blender tracks the finger position and
            # routes wheel events to the panel under the touch.
            0x0201, 0x0202, 0x0203,    # WM_LBUTTONDOWN/UP/DBLCLK
            0x0204, 0x0205, 0x0206,    # WM_RBUTTONDOWN/UP/DBLCLK
            0x0207, 0x0208, 0x0209,    # WM_MBUTTONDOWN/UP/DBLCLK
        })

        def hook_proc(nCode, wParam, lParam):
            try:
                if nCode >= 0 and wParam == PM_REMOVE:
                    msg = ctypes.cast(lParam, ctypes.POINTER(MSG)).contents
                    m = msg.message

                    # While any finger is on the screen, suppress the
                    # mouse messages Windows auto-generates from touch — UNLESS
                    # the current gesture is a button/toolbar/gizmo passthrough,
                    # in which case let the click reach Blender so it registers.
                    is_ours = False
                    if m in _SYNTH:
                        try:
                            xinfo = user32.GetMessageExtraInfo()
                            is_ours = (int(xinfo) & 0xFFFFFFFF) == _INJECT_SIG
                        except Exception:
                            is_ours = False
                    if (touch_count[0] > 0 and m in _SYNTH
                            and not _passthrough[0] and not is_ours):
                        msg.message = 0x0000  # WM_NULL — Blender ignores it
                        # Still let CallNextHookEx run so other hooks are unaffected.

                    elif m in (WM_POINTERDOWN, WM_POINTERUPDATE, WM_POINTERUP,
                               WM_POINTERENTER, WM_POINTERLEAVE):
                        pid = msg.wParam & 0xFFFF
                        ptype = wintypes.DWORD()
                        if user32.GetPointerType(pid, ctypes.byref(ptype)) and ptype.value == PT_TOUCH:
                            info = POINTER_INFO()
                            if user32.GetPointerInfo(pid, ctypes.byref(info)):
                                is_down = bool(info.pointerFlags & POINTER_FLAG_DOWN) or m == WM_POINTERDOWN
                                is_up = (bool(info.pointerFlags & POINTER_FLAG_UP)
                                         or m == WM_POINTERUP
                                         or m == WM_POINTERLEAVE)
                                # Keep touch_count in sync so the filter above
                                # knows exactly when fingers are active.
                                if m == WM_POINTERDOWN:
                                    touch_count[0] += 1
                                elif m in (WM_POINTERUP, WM_POINTERLEAVE):
                                    touch_count[0] = max(0, touch_count[0] - 1)
                                self.events.append({
                                    "pid": pid,
                                    "x": info.ptPixelLocation.x,
                                    "y": info.ptPixelLocation.y,
                                    "down": is_down,
                                    "up": is_up,
                                })
                                if len(self.events) > 1024:
                                    del self.events[:512]
            except Exception:
                pass
            return user32.CallNextHookEx(self.hook_handle, nCode, wParam, lParam)

        self._proc = HOOKPROC(hook_proc)
        tid = user32.GetCurrentThreadId()
        self.hook_handle = user32.SetWindowsHookExW(WH_GETMESSAGE, self._proc, None, tid)
        if not self.hook_handle:
            err = ctypes.get_last_error()
            print(f"[Multitouch] SetWindowsHookExW failed, error {err}")
            self._proc = None
            return False
        self.installed = True
        print("[Multitouch] Hook installed — WM_POINTER capture + synthesised-mouse filter active.")
        return True

    def uninstall(self):
        if self.installed and self.hook_handle:
            user32.UnhookWindowsHookEx(self.hook_handle)
        self.hook_handle = None
        self._proc = None
        self.installed = False
        self.events.clear()

    def drain(self):
        if not self.events:
            return []
        ev = self.events
        self.events = []
        return ev


# ---------------------------------------------------------------------------
# Modal operator
# ---------------------------------------------------------------------------
class VIEW3D_OT_multitouch_navigate(bpy.types.Operator):
    """Pan, rotate, and zoom the 3D viewport, plus scroll UI/menus, via multitouch."""
    bl_idname = "view3d.multitouch_navigate"
    bl_label = "Multitouch Navigate"
    bl_options = {'REGISTER'}

    _timer = None
    _hook = None
    _state = None

    @classmethod
    def poll(cls, context):
        return IS_WINDOWS and TOUCH_API_AVAILABLE

    def invoke(self, context, event):
        if not IS_WINDOWS:
            self.report({'ERROR'}, "Multitouch addon currently supports Windows only.")
            return {'CANCELLED'}
        if not TOUCH_API_AVAILABLE:
            self.report({'ERROR'}, "Windows Pointer API not available.")
            return {'CANCELLED'}

        self._hook = TouchHook()
        if not self._hook.install():
            self.report({'ERROR'}, "Failed to install touch hook.")
            return {'CANCELLED'}

        self._state = GestureState()

        wm = context.window_manager
        self._timer = wm.event_timer_add(1.0 / 120.0, window=context.window)
        wm.modal_handler_add(self)

        context.scene.multitouch_props.active = True
        self.report({'INFO'}, "Multitouch navigation active. Press ESC to stop.")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type in {'ESC'} and event.value == 'PRESS':
            return self.finish(context)

        # Stop button sets active = False; catch it on next timer tick.
        if not context.scene.multitouch_props.active:
            return self.finish(context)

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        # Drain hook events into gesture state.
        events = self._hook.drain()
        for ev in events:
            self._state.update_point(ev["pid"], ev["x"], ev["y"],
                                     ev["down"], ev["up"])

        # Gesture ended? release our injected button (if any), then reset mode.
        if not self._state.points:
            if self._state.mode == 'passthrough' and self._state.last_inject_xy:
                ix, iy = self._state.last_inject_xy
                _inject_left_up(ix, iy)
            self._state.last_inject_xy = None
            self._state.end_gesture()
            if self._hook is not None:
                self._hook.passthrough[0] = False
            return {'PASS_THROUGH'}

        c = self._state._centroid()
        if c is None:
            return {'PASS_THROUGH'}
        cx, cy = c

        # First touch of a new gesture: pick mode based on what's under the centroid.
        if self._state.mode is None:
            target = None
            for window in context.window_manager.windows:
                t = find_area_under_cursor(int(cx), int(cy), window)
                if t is not None:
                    target = t
                    break
            if target is None:
                # Centroid outside any Blender area — wait until it enters one.
                self._state.reset_baseline()
                return {'PASS_THROUGH'}
            area, region, _local = target
            rtype = region.type

            # Regions that are button strips → passthrough (hold-drag clicks).
            # NOTE: TOOLS (left toolbar) is intentionally NOT here — it is
            # scrollable, so it falls through to 'scroll' mode below.
            _BUTTON_REGIONS = {'NAVIGATION_BAR', 'HEADER', 'FOOTER',
                               'TAB', 'EXECUTE'}
            # Regions that are scrollable panel content → scroll on drag.
            _SCROLL_REGIONS = {'UI', 'CHANNELS', 'WINDOW'}

            if area.type == 'VIEW_3D' and rtype == 'WINDOW':
                # Inside the 3D viewport proper. Check the gizmo cluster first.
                local_xy = None
                for window in context.window_manager.windows:
                    lx, ly = _screen_to_local(window, int(cx), int(cy))
                    if (area.x <= lx <= area.x + area.width and
                            area.y <= ly <= area.y + area.height):
                        local_xy = (lx, ly)
                        break
                if local_xy is not None and _in_navigate_gizmo_zone(
                        area, region, local_xy[0], local_xy[1]):
                    self._state.begin_gesture('passthrough')
                else:
                    rv3d = area.spaces.active.region_3d
                    self._state.begin_gesture('view3d', (area, region, rv3d))
            elif rtype in _BUTTON_REGIONS:
                # Toolbars, headers, tool-tabs: taps should click, so pass
                # synthesised clicks through and do no scrolling.
                self._state.begin_gesture('passthrough')
            else:
                # Properties editor, N-panel, outliner, etc.: scroll on drag.
                self._state.begin_gesture('scroll')

            # Keep the hook SUPPRESSING Windows' own touch-synth clicks (the
            # initial synth button-down is eaten before we classify the gesture
            # anyway). For passthrough we drive a clean, controlled left-button
            # press OURSELVES at the touch point, hold it through the drag, and
            # release on lift — this reliably clicks gizmo/header buttons and
            # drags gizmo handles.
            if self._hook is not None:
                self._hook.passthrough[0] = False
            if self._state.mode == 'passthrough':
                _inject_left_down(int(cx), int(cy))
                self._state.last_inject_xy = (int(cx), int(cy))

        delta = self._state.consume_delta()
        if delta is None:
            return {'PASS_THROUGH'}
        pan_dx, pan_dy, zoom, rot_dx, rot_dy, n = delta

        if self._state.mode == 'passthrough':
            # Hold-drag: our injected left button is held; move the cursor to
            # follow the finger so Blender drags the gizmo/handle (or simply
            # holds on a button until release for a clean click).
            _inject_move(int(cx), int(cy))
            self._state.last_inject_xy = (int(cx), int(cy))
            return {'PASS_THROUGH'}

        if self._state.mode == 'view3d':
            target = self._state.view3d_target
            if target is None:
                return {'PASS_THROUGH'}
            area, region, rv3d = target
            if (pan_dx or pan_dy or rot_dx or rot_dy or abs(zoom - 1.0) > 1e-4):
                apply_gesture(rv3d, region, pan_dx, pan_dy, zoom, rot_dx, rot_dy)
                try:
                    area.tag_redraw()
                except ReferenceError:
                    # Area was removed (screen layout change). Drop the gesture.
                    self._state.end_gesture()
        else:
            # Scroll mode: use 1-finger or 2-finger depending on setting.
            sf = context.scene.multitouch_props.scroll_fingers
            if sf == 'ONE':
                dy = rot_dy if n == 1 else 0.0
            else:  # 'TWO'
                dy = pan_dy if n >= 2 else 0.0
            if dy:
                self._state.wheel_accum += dy * (WHEEL_DELTA / SCROLL_PIXELS_PER_NOTCH)
                if abs(self._state.wheel_accum) >= WHEEL_DELTA:
                    notches = int(self._state.wheel_accum / WHEEL_DELTA)
                    d = notches * WHEEL_DELTA
                    # Drag DOWN (Windows y-down → positive dy) sends positive
                    # wheel delta, which Blender treats as scroll-up → content
                    # moves DOWN with the finger (natural scrolling).
                    post_mouse_wheel(int(cx), int(cy), d)
                    self._state.wheel_accum -= d

        return {'PASS_THROUGH'}

    def finish(self, context):
        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        if self._hook is not None:
            self._hook.uninstall()
            self._hook = None
        context.scene.multitouch_props.active = False
        self.report({'INFO'}, "Multitouch navigation stopped.")
        return {'CANCELLED'}


class VIEW3D_OT_multitouch_stop(bpy.types.Operator):
    bl_idname = "view3d.multitouch_stop"
    bl_label = "Stop Multitouch Navigate"

    def execute(self, context):
        context.scene.multitouch_props.active = False
        self.report({'INFO'}, "Press ESC in the 3D viewport to stop.")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Properties + UI panel
# ---------------------------------------------------------------------------
class MultitouchProps(bpy.types.PropertyGroup):
    active: bpy.props.BoolProperty(name="Active", default=False)
    scroll_fingers: bpy.props.EnumProperty(
        name="Scroll With",
        description="How many fingers trigger scrolling in panels and menus",
        items=[
            ('ONE',  '1 Finger',  'One-finger drag scrolls (two fingers do nothing outside the viewport)'),
            ('TWO',  '2 Fingers', 'Two-finger drag scrolls (one finger does nothing outside the viewport)'),
        ],
        default='ONE',
    )


class VIEW3D_PT_multitouch(bpy.types.Panel):
    bl_label = "Multitouch"
    bl_idname = "VIEW3D_PT_multitouch"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Touch"

    def draw(self, context):
        layout = self.layout
        props = context.scene.multitouch_props

        if not IS_WINDOWS:
            layout.label(text="Windows only.", icon='ERROR')
            return
        if not TOUCH_API_AVAILABLE:
            layout.label(text="Pointer API unavailable.", icon='ERROR')
            return

        col = layout.column(align=True)
        if props.active:
            col.label(text="Active", icon='REC')
            col.operator("view3d.multitouch_stop", icon='X', text="Stop Multitouch")
        else:
            col.operator("view3d.multitouch_navigate", icon='HAND',
                         text="Start Multitouch")
        col.separator()
        box = layout.box()
        box.label(text="In the 3D viewport:")
        box.label(text="  • 1 finger → orbit")
        box.label(text="  • 2 fingers drag → pan")
        box.label(text="  • 2 fingers pinch → zoom")
        box.separator()
        box.label(text="Panels & sidebars:")
        box.label(text="  • drag → scroll")
        box.label(text="  • tap → click button")
        box.separator()
        box.label(text="Toolbars & gizmo buttons:")
        box.label(text="  • tap → click")

        layout.separator()
        layout.prop(props, "scroll_fingers", expand=True)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
classes = (
    MultitouchProps,
    VIEW3D_OT_multitouch_navigate,
    VIEW3D_OT_multitouch_stop,
    VIEW3D_PT_multitouch,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.multitouch_props = bpy.props.PointerProperty(type=MultitouchProps)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    try:
        del bpy.types.Scene.multitouch_props
    except Exception:
        pass


if __name__ == "__main__":
    register()
