"""Undo the side effects of experiment tasks on the test phone (Huawei EMUI/HarmonyOS 4).

  python3 cleanup.py            # stop timer, delete experiment alarms and contacts
  python3 cleanup.py --list     # only show what would be removed

Only removes items that experiment tasks create; the owner's own alarms are kept
(PROTECTED_ALARMS) and only test contacts other than the 3 fixtures are deleted.
"""
import argparse
import json
import os
import re
import time
from collections import Counter

import device
from uitree import UITree

# Snapshot of the owner's alarms taken before experiments; anything else is experiment-made.
BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runs", "alarm_baseline.json")


def load_baseline():
    with open(BASELINE, encoding="utf-8") as f:
        return Counter(json.load(f))
EXPERIMENT_CONTACTS = ["测试孙女"]


def screen():
    return UITree(device.dump_ui())


def find(tree, label, exact=True):
    """Prefer a clickable match (e.g. the 闹钟 tab, not the 闹钟 page title)."""
    hits = [(n, flags) for n, role, lab, flags in tree.pruned_nodes()
            if ((lab == label) if exact else (label in lab))]
    for n, flags in hits:
        if "可点" in flags:
            return n
    return hits[0][0] if hits else None


def tap_label(label, exact=True, wait=1.2):
    n = find(screen(), label, exact)
    if n is None:
        return False
    device.tap(*n.center)
    time.sleep(wait)
    return True


def stop_timer():
    device.reset_apps(["clock"])
    device.open_app("clock")
    time.sleep(2)
    tap_label("计时器")
    if tap_label("暂停"):
        tap_label("重置")
        return True
    return False


def alarm_rows(tree):
    """Return [(node, time_str, label)] for alarm rows on the alarm tab."""
    rows = []
    for n, role, lab, flags in tree.pruned_nodes():
        m = re.search(r"(\d{1,2}:\d{2})", lab)
        if m and "可长按" in flags and role != "开关":
            rows.append((n, m.group(1), lab))
    return rows


def scan_alarms():
    """Scroll through the whole alarm list; return full row labels (multiset order kept)."""
    device.reset_apps(["clock"])
    device.open_app("clock")
    time.sleep(2)
    tap_label("闹钟")
    seen, quiet = [], 0
    for _ in range(15):
        rows = [r[2] for r in alarm_rows(screen()) if r[2].count(" / ") >= 2]
        new = [r for r in rows if r not in seen]
        seen += new
        quiet = quiet + 1 if not new else 0
        if quiet >= 2:
            break
        device.swipe("up")
        time.sleep(1)
    return seen


def extra_alarms():
    """Alarms present now but not in the owner's baseline snapshot."""
    base = load_baseline()
    extra = Counter(scan_alarms()) - base
    return list(extra.elements())


def delete_alarms(dry=False):
    targets = extra_alarms()
    if dry or not targets:
        device.home()
        return targets
    removed = []
    for lab in targets:
        device.reset_apps(["clock"])
        device.open_app("clock")
        time.sleep(2)
        tap_label("闹钟")
        for _ in range(8):
            n = find(screen(), lab)
            if n is not None:
                break
            device.swipe("up")
            time.sleep(1)
        if n is None:
            continue
        # press the left part of the row: the floating "add alarm" button sits at the bottom
        x1, y1, x2, y2 = n.bounds
        device.long_press(x1 + (x2 - x1) // 6, (y1 + y2) // 2)
        time.sleep(1.2)
        if find(screen(), "新建闹钟") is not None:  # hit the add button by mistake: back out unsaved
            tap_label("取消")
            continue
        if tap_label("删除"):
            tap_label("删除")  # confirmation dialog, if any
            removed.append(lab)
    device.home()
    return removed


def delete_contacts(dry=False):
    removed = []
    for name in EXPERIMENT_CONTACTS:
        device.reset_apps(["contacts"])
        device.open_app("contacts")
        time.sleep(2.5)
        if not find(screen(), name):
            continue
        if dry:
            removed.append(name)
            continue
        tap_label(name, wait=2)
        tap_label("更多选项") or tap_label("更多")
        if tap_label("删除联系人"):
            tap_label("我已阅读并了解", wait=0.5)
            tap_label("删除", wait=2)
            removed.append(name)
    device.home()
    return removed


def timer_state():
    """Return the timer screen texts (opens the timer tab)."""
    device.reset_apps(["clock"])
    device.open_app("clock")
    time.sleep(2)
    tap_label("计时器")
    return [lab for n, role, lab, flags in screen().pruned_nodes()]


def contact_detail(name):
    """Open a contact by name; return its detail-page texts, or None if absent."""
    device.reset_apps(["contacts"])
    device.open_app("contacts")
    time.sleep(2.5)
    if not tap_label(name, wait=2):
        return None
    texts = screen().all_text()
    device.home()
    return texts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true")
    a = p.parse_args()
    if not a.list:
        print("timer stopped:", stop_timer())
    print("alarms removed:" if not a.list else "alarms to remove:", delete_alarms(a.list))
    print("contacts removed:" if not a.list else "contacts to remove:", delete_contacts(a.list))
    device.reset_apps()
    device.home()


if __name__ == "__main__":
    main()
