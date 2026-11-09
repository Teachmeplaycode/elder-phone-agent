"""Thin adb wrapper: observe the screen and perform atomic actions.

Uses only `adb` (no helper app on the phone), so it works on any brand.
Chinese text input needs ADBKeyBoard (https://github.com/senzhk/ADBKeyBoard);
without it only ASCII text can be typed.
"""
import base64
import os
import re
import subprocess
import time

SERIAL = os.environ.get("ANDROID_SERIAL")

# Brand-independent ways to open common apps via Android intents.
APP_INTENTS = {
    "phone": ["am", "start", "-a", "android.intent.action.DIAL"],
    "contacts": ["am", "start", "-a", "android.intent.action.VIEW", "-d", "content://contacts/people"],
    "messages": ["am", "start", "-a", "android.intent.action.SENDTO", "-d", "sms:"],
    "clock": ["am", "start", "-a", "android.intent.action.SHOW_ALARMS"],
    "settings": ["am", "start", "-a", "android.settings.SETTINGS"],
    "calendar": ["am", "start", "-a", "android.intent.action.VIEW", "-d", "content://com.android.calendar/time"],
}


class AdbError(RuntimeError):
    pass


def adb(*args, timeout=20, binary=False):
    cmd = ["adb"] + (["-s", SERIAL] if SERIAL else []) + list(args)
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise AdbError("%s -> %s" % (" ".join(cmd), p.stderr.decode(errors="ignore").strip()))
    return p.stdout if binary else p.stdout.decode("utf-8", errors="ignore")


def shell(*args, **kw):
    return adb("shell", *args, **kw)


def devices():
    out = adb("devices")
    return [l.split()[0] for l in out.splitlines()[1:] if l.strip().endswith("device")]


_u2 = None


def _u2_device():
    global _u2
    if _u2 is None:
        import uiautomator2
        _u2 = uiautomator2.connect(SERIAL)
    return _u2


def dump_ui(retries=3):
    """Return the current UI hierarchy XML (obtained through the accessibility framework).

    Prefers uiautomator2 (does not wait for an idle screen, so animated pages
    such as clocks work); falls back to `uiautomator dump`.
    """
    try:
        return _u2_device().dump_hierarchy(compressed=False)
    except ImportError:
        pass
    last = ""
    for _ in range(retries):
        try:
            out = shell("uiautomator", "dump", "/sdcard/_ui.xml", timeout=30)
            last = out
            if "dumped" in out.lower():
                xml = adb("exec-out", "cat", "/sdcard/_ui.xml")
                if xml.strip().startswith("<?xml"):
                    return xml
        except AdbError as e:
            last = str(e)
        time.sleep(0.8)
    raise AdbError("uiautomator dump failed: " + last)


def screenshot_png():
    return adb("exec-out", "screencap", "-p", binary=True, timeout=30)


def screen_size():
    out = shell("wm", "size")
    m = re.findall(r"(\d+)x(\d+)", out)
    w, h = m[-1]
    return int(w), int(h)


def foreground_package():
    out = shell("dumpsys", "window")
    m = re.search(r"mCurrentFocus=Window\{[^ ]+ [^ ]+ ([^/ }]+)", out)
    return m.group(1) if m else ""


def tap(x, y):
    shell("input", "tap", str(int(x)), str(int(y)))


def long_press(x, y, ms=800):
    shell("input", "swipe", str(int(x)), str(int(y)), str(int(x)), str(int(y)), str(ms))


def swipe(direction, w=None, h=None):
    if w is None:
        w, h = screen_size()
    cx, cy = w // 2, h // 2
    dx, dy = int(w * 0.3), int(h * 0.3)
    # "up" means content moves up (finger swipes up) -> reveals content below.
    vec = {"up": (0, -dy), "down": (0, dy), "left": (-dx, 0), "right": (dx, 0)}[direction]
    shell("input", "swipe", str(cx - vec[0] // 2), str(cy - vec[1] // 2),
          str(cx + vec[0] // 2), str(cy + vec[1] // 2), "350")


def swipe_in(bounds, direction):
    """Swipe inside one element (e.g. a time-picker wheel or a nested list)."""
    x1, y1, x2, y2 = bounds
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    dx, dy = int((x2 - x1) * 0.35), int((y2 - y1) * 0.35)
    vec = {"up": (0, -dy), "down": (0, dy), "left": (-dx, 0), "right": (dx, 0)}[direction]
    shell("input", "swipe", str(cx - vec[0]), str(cy - vec[1]), str(cx + vec[0]), str(cy + vec[1]), "300")


def set_alarm(hour, minute, message=""):
    """System AlarmClock intent: opens the alarm editor pre-filled; the user/agent saves it."""
    args = ["am", "start", "-a", "android.intent.action.SET_ALARM",
            "--ei", "android.intent.extra.alarm.HOUR", str(int(hour)),
            "--ei", "android.intent.extra.alarm.MINUTES", str(int(minute))]
    if message:
        args += ["--es", "android.intent.extra.alarm.MESSAGE", '"%s"' % message.replace('"', "")]
    shell(*args)


def set_timer(seconds, message=""):
    args = ["am", "start", "-a", "android.intent.action.SET_TIMER",
            "--ei", "android.intent.extra.alarm.LENGTH", str(int(seconds))]
    if message:
        args += ["--es", "android.intent.extra.alarm.MESSAGE", '"%s"' % message.replace('"', "")]
    shell(*args)


def key(code):
    shell("input", "keyevent", str(code))


def back():
    key(4)


def home():
    key(3)


def has_adb_keyboard():
    return "com.android.adbkeyboard" in shell("pm", "list", "packages", "com.android.adbkeyboard")


ADB_IME = "com.android.adbkeyboard/.AdbIME"


def current_ime():
    return shell("settings", "get", "secure", "default_input_method").strip()


def use_adb_ime():
    """Switch to ADBKeyBoard; return the previous IME so it can be restored."""
    prev = current_ime()
    if prev != ADB_IME and has_adb_keyboard():
        shell("ime", "enable", ADB_IME)
        shell("ime", "set", ADB_IME)
    return prev


def restore_ime(prev):
    if prev and prev != ADB_IME:
        shell("ime", "set", prev)


def clear_text(current=""):
    """Clear the focused field: move to end, then one backspace per character (+margin).

    Works on custom fields (e.g. the dialer) where IME clear commands are ignored.
    """
    n = len(current or "") + 3
    shell("input", "keyevent", "123")  # KEYCODE_MOVE_END
    shell("input", "keyevent", *(["67"] * n))  # KEYCODE_DEL


def input_text(text):
    if all(ord(c) < 128 for c in text):
        shell("input", "text", text.replace(" ", "%s").replace("'", "\\'"))
        return
    if not has_adb_keyboard():
        raise AdbError("Non-ASCII input requires ADBKeyBoard installed and enabled")
    b64 = base64.b64encode(text.encode("utf-8")).decode()
    shell("am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64)


# Vendor launcher activities tried first (generic intents may open an app chooser).
APP_COMPONENTS = {
    "phone": ["com.huawei.contacts/com.android.contacts.activities.DialtactsActivity",
              "com.google.android.dialer/.extensions.GoogleDialtactsActivity"],
    "contacts": ["com.huawei.contacts/com.android.contacts.activities.PeopleActivity",
                 "com.google.android.contacts/com.android.contacts.activities.PeopleActivity"],
    "messages": ["com.android.mms/.ui.ConversationList",
                 "com.google.android.apps.messaging/.ui.ConversationListActivity"],
    "clock": ["com.huawei.deskclock/com.android.deskclock.AlarmsMainActivity",
              "com.google.android.deskclock/com.android.deskclock.DeskClock"],
    "settings": ["com.android.settings/.HWSettings", "com.android.settings/.Settings"],
    "calendar": ["com.huawei.calendar/com.android.calendar.AllInOneActivity",
                 "com.google.android.calendar/com.android.calendar.AllInOneActivity"],
    "camera": ["com.huawei.camera/com.huawei.camera", "com.android.camera2/com.android.camera.CameraLauncher"],
}
# Scene alias -> packages (Huawei / AOSP / Google), used by final.app checks.
SCENE_PACKAGES = {
    "phone": ["com.huawei.contacts", "com.android.dialer", "com.google.android.dialer"],
    "contacts": ["com.huawei.contacts", "com.android.contacts", "com.google.android.contacts"],
    "messages": ["com.android.mms", "com.android.messaging", "com.google.android.apps.messaging"],
    "clock": ["com.huawei.deskclock", "com.android.deskclock", "com.google.android.deskclock"],
    "settings": ["com.android.settings"],
    "calendar": ["com.huawei.calendar", "com.android.calendar", "com.google.android.calendar"],
    "camera": ["com.huawei.camera", "com.android.camera2", "com.android.camera", "com.google.android.GoogleCamera"],
}
_installed = None


def is_emulator():
    return shell("getprop", "ro.kernel.qemu").strip() == "1" or "emulator" in (SERIAL or "")


def load_snapshot(name="clean"):
    """Emulator only: restore a saved snapshot so every episode starts identically."""
    out = adb("emu", "avd", "snapshot", "load", name, timeout=120)
    if "OK" not in out:
        raise AdbError("snapshot load failed: " + out)
    adb("wait-for-device", timeout=120)
    time.sleep(2)


def _is_installed(component):
    global _installed
    if _installed is None:
        _installed = set(l.split(":", 1)[1].strip() for l in shell("pm", "list", "packages").splitlines() if ":" in l)
    return component.split("/")[0] in _installed


def reset_apps(names=None):
    """Force-stop scene apps so every episode starts from the same clean state."""
    for name in (names or APP_COMPONENTS):
        for comp in APP_COMPONENTS.get(name, []):
            shell("am", "force-stop", comp.split("/")[0])


def open_app(name, arg=None):
    """Open an app; for phone/messages an optional number pre-fills the dialer / recipient
    through the standard system intents (the final call/send stays with the user)."""
    if arg and name == "phone":
        shell("am", "start", "-a", "android.intent.action.DIAL", "-d", "tel:" + re.sub(r"[^\d+*#]", "", arg))
        return
    if arg and name == "messages":
        shell("am", "start", "-a", "android.intent.action.SENDTO", "-d", "sms:" + re.sub(r"[^\d+]", "", arg))
        return
    for comp in APP_COMPONENTS.get(name, []):
        if _is_installed(comp):
            out = shell("am", "start", "-n", comp, "2>&1")
            if "Error" not in out:
                return  # otherwise this vendor activity does not exist here; try the next one
    if name in APP_INTENTS:
        shell(*APP_INTENTS[name])
    else:  # treat as package name
        shell("monkey", "-p", name, "-c", "android.intent.category.LAUNCHER", "1")


def call_active():
    """True if any SIM is dialing / in a call (mCallState 1=ringing, 2=offhook)."""
    out = shell("dumpsys", "telephony.registry")
    return any(int(s) != 0 for s in re.findall(r"mCallState=(\d)", out))


def end_call():
    """Safety net: hang up immediately (key event, then the on-screen button)."""
    key(6)  # KEYCODE_ENDCALL
    time.sleep(0.5)
    if call_active():
        xml = dump_ui()
        for m in re.finditer(r'(?:text|content-desc)="(挂断|结束通话|End call)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
            x1, y1, x2, y2 = map(int, m.groups()[1:])
            tap((x1 + x2) // 2, (y1 + y2) // 2)
            break
    return not call_active()


def wait_idle(seconds=1.2):
    time.sleep(seconds)
