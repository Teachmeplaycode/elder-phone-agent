"""State checks on the AOSP/Google emulator (rooted image), used by the held-out test set.

Each episode starts from the 'clean' snapshot, so any matching item found here
was created by the agent in that episode.
"""
import re
import time

import device
from uitree import UITree

ALARM_DB = "/data/user_de/0/com.google.android.deskclock/databases/alarms.db"


def alarms():
    """Return [(hour, minute, enabled, label)] from the clock app database."""
    out = device.shell("sqlite3 -separator '|' %s 'select hour,minutes,enabled,label from alarm_templates'" % ALARM_DB)
    rows = []
    for line in out.strip().splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            rows.append((int(parts[0]), int(parts[1]), parts[2] == "1", parts[3]))
    return rows


def check_alarm(s):
    rows = alarms()
    for h, m, en, label in rows:
        if en and h == s["hour"] and m == s["minute"] and (not s.get("label") or s["label"] in label):
            return True, "alarm %02d:%02d %s" % (h, m, label)
    return False, "alarms=%s" % [(h, m, en, l) for h, m, en, l in rows]


def check_contact(s):
    out = device.shell("content", "query", "--uri", "content://com.android.contacts/data",
                       "--projection", "display_name:data1",
                       "--where", "\"mimetype='vnd.android.cursor.item/phone_v2'\"")
    for line in out.splitlines():
        name = re.search(r"display_name=([^,]*)", line)
        num = re.search(r"data1=([^,]*)", line)
        if name and num and s["name"] in name.group(1) and re.sub(r"\D", "", num.group(1)) == s["number"]:
            return True, "contact %s %s" % (name.group(1), num.group(1))
    return False, "contact absent"


def check_timer(s):
    """Open the timer tab; the clock shows e.g. '20 分钟的定时器' and a 暂停 button when running."""
    device.open_app("clock")
    time.sleep(2.5)
    tree = UITree(device.dump_ui())
    tab = [n for n, role, lab, flags in tree.pruned_nodes() if lab == "定时器" and "可点" in flags]
    if tab:
        device.tap(*tab[-1].center)
        time.sleep(1.5)
        tree = UITree(device.dump_ui())
    texts = tree.all_text()
    running = any(t == "暂停" for t in texts)
    length = None
    for t in texts:
        m = re.search(r"(?:(\d+)\s*小时)?\s*(?:(\d+)\s*分钟)?\s*(?:(\d+)\s*秒)?的定时器", t)
        if m and any(m.groups()):
            h, mi, se = (int(x) if x else 0 for x in m.groups())
            length = h * 3600 + mi * 60 + se
    remaining = []
    for t in texts:
        if re.fullmatch(r"\d{1,2}(:\d{2}){1,2}", t):
            parts = [int(x) for x in t.split(":")]
            remaining.append(sum(p * m for p, m in zip(reversed(parts), (1, 60, 3600))))
    near = [r for r in remaining if s["seconds"] - 180 <= r <= s["seconds"]]
    ok = running and (length == s["seconds"] or bool(near))
    return ok, "timer running=%s length=%s remaining=%s" % (running, length, remaining)


def check_brightness_max(s):
    v = int(device.shell("settings", "get", "system", "screen_brightness").strip() or 0)
    return v >= 250, "screen_brightness=%d" % v


def check(s):
    fn = {"alarm": check_alarm, "contact": check_contact, "timer": check_timer,
          "brightness_max": check_brightness_max}.get(s["what"])
    if fn is None:
        return None, "unknown state check %s" % s["what"]
    return fn(s)
