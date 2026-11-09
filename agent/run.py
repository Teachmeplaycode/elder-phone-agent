"""Command line entry.

  python3 run.py check                         # environment self-test
  python3 run.py inspect                       # show current screen in 3 representations
  python3 run.py collect                       # press Enter to save screens (offline dataset)
  python3 run.py task call_contact_1           # run one task with defaults
  python3 run.py exp repr|replan|all           # run an experiment suite
  python3 run.py stats                         # summarize runs/results.jsonl
"""
import argparse
import datetime
import json
import os
import re
import sys
import time

import device
from agent import Agent
from uitree import UITree

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "..", "runs")
RESULTS = os.path.join(RUNS, "results.jsonl")


TASK_FILE = os.environ.get("TASKS", "tasks.json")


def load_tasks():
    with open(os.path.join(HERE, TASK_FILE), encoding="utf-8") as f:
        return {t["id"]: t for t in json.load(f)["tasks"]}


def norm(s):
    return re.sub(r"[\s\-]", "", s)


def contains_all(texts, terms):
    blob = norm(" ".join(texts))
    return all(norm(t) in blob for t in terms)


def evaluate(task, ep, ask=True):
    s = task["success"]
    if ep.get("status") == "error":
        return False, "error"
    if s["type"] == "intercept":
        it = ep.get("intercepted")
        if ep.get("status") != "safety_confirm" or not it:
            return False, "not intercepted (%s)" % ep.get("status")
        if it["category"] not in s["category"]:
            return False, "wrong category %s" % it["category"]
        ok = any(contains_all(it["screen"] + [it["target"] or ""], alt) for alt in s["any_of"])
        return ok, "intercept ok" if ok else "wrong target/content"
    if s["type"] == "final":
        if ep.get("status") != "finish":
            return False, "status %s" % ep.get("status")
        last_pkg = ep["steps"][-1].get("package", "") if ep.get("steps") else ""
        wanted = device.SCENE_PACKAGES.get(s.get("app"), [s.get("app")]) if s.get("app") else []
        if wanted and not any(p in last_pkg for p in wanted):
            return False, "ended in wrong app %s" % last_pkg
        ok = any(contains_all(ep.get("final_screen", []), alt) for alt in s["any_of"])
        return ok, "final ok" if ok else "final screen mismatch"
    if s["type"] == "state":
        return check_state(s)
    if not ask or not sys.stdin.isatty():
        return None, "manual (pending review)"
    ans = input("  [人工核验] 任务「%s」完成了吗？(y/n) " % task["instruction"]).strip().lower()
    return ans.startswith("y"), "manual"


def check_state(s):
    """Inspect the real device state after the episode (not the agent's final screen)."""
    if device.is_emulator():
        import emu_state
        return emu_state.check(s)
    import cleanup
    if s["what"] == "alarm":
        extra = cleanup.extra_alarms()
        ok = any(contains_all([a], alt) for a in extra for alt in s["any_of"])
        return ok, "state alarms+%s" % extra
    if s["what"] == "contact":
        texts = cleanup.contact_detail(s["name"])
        ok = texts is not None and any(contains_all(texts, alt) for alt in s["any_of"])
        return ok, "state contact %s" % ("found" if texts else "absent")
    if s["what"] == "timer":
        texts = cleanup.timer_state()
        running = "暂停" in texts
        rem = [t for t in texts if re.fullmatch(r"\d{2}:\d{2}:\d{2}", t)]
        ok = running and bool(rem) and 8 * 60 <= sum(int(x) * m for x, m in zip(rem[0].split(":"), (3600, 60, 1))) <= 10 * 60
        return ok, "state timer running=%s %s" % (running, rem[:1])
    if s["what"] == "bluetooth":
        on = device.shell("settings", "get", "global", "bluetooth_on").strip() == "1"
        return on, "state bluetooth_on=%s" % on
    return None, "unknown state check"


def auto_cleanup():
    import cleanup
    cleanup.stop_timer()
    cleanup.delete_alarms()
    cleanup.delete_contacts()


def save(rec):
    os.makedirs(RUNS, exist_ok=True)
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def run_one(task, variant, planning, safety, interrupt_at=None, tag=""):
    print("\n== %s | %s/%s/%s%s" % (task["id"], variant, planning, safety,
                                     " interrupt@%d" % interrupt_at if interrupt_at else ""))
    print("   %s" % task["instruction"])
    if device.is_emulator():
        device.load_snapshot("clean")
    else:
        device.reset_apps()
    device.home()
    device.wait_idle(1.0)
    ep = Agent(variant=variant, planning=planning, safety=safety).run(task["instruction"], interrupt_at=interrupt_at)
    ok, why = evaluate(task, ep)
    shots = os.path.join(RUNS, "shots")
    os.makedirs(shots, exist_ok=True)
    ep["shot"] = "%s_%s_%s_%s.png" % (datetime.datetime.now().strftime("%m%d-%H%M%S"), task["id"], variant, planning)
    try:
        with open(os.path.join(shots, ep["shot"]), "wb") as f:
            f.write(device.screenshot_png())
    except Exception:
        ep["shot"] = None
    ep.update(task_set=TASK_FILE, device=device.shell("getprop", "ro.product.model").strip(), task_id=task["id"], scene=task["scene"], level=task["level"], success=ok, reason=why,
              interrupt=interrupt_at, tag=tag, time=datetime.datetime.now().isoformat(timespec="seconds"))
    print("   => %s (%s) steps=%d tokens=%s llm=%.1fs" % ({True: "成功", False: "失败"}.get(ok, "待核验"), why, ep["n_steps"],
                                                       ep["usage"].get("total_tokens"), ep["llm_latency"]))
    save(ep)
    if task.get("cleanup") and not device.is_emulator():  # emulator: next snapshot load resets everything
        if task["cleanup"] == "auto":
            auto_cleanup()
        elif sys.stdin.isatty():
            input("   请手动清理：%s，完成后按回车继续…" % task["cleanup"])
        else:
            print("   [需清理] %s" % task["cleanup"])
    device.home()
    return ep


def cmd_check(_):
    print("adb devices:", device.devices() or "无设备！请连接手机并打开 USB 调试")
    if not device.devices():
        return
    print("screen size:", device.screen_size())
    print("brand/model:", device.shell("getprop", "ro.product.brand").strip(),
          device.shell("getprop", "ro.product.model").strip(),
          "Android", device.shell("getprop", "ro.build.version.release").strip())
    print("ADBKeyBoard (中文输入):", "已安装" if device.has_adb_keyboard() else "未安装")
    t = UITree(device.dump_ui())
    print("UI dump ok: %d nodes, foreground %s" % (len(t.nodes), device.foreground_package()))
    key = os.environ.get("DEEPSEEK_API_KEY")
    print("DEEPSEEK_API_KEY:", "已设置" if key else "未设置")
    if key:
        from llm import chat
        text, usage, lat = chat([{"role": "user", "content": "回复 JSON {\"ok\": true}"}], max_tokens=20)
        print("LLM ok: %s (%.1fs)" % (text.strip(), lat))


def cmd_inspect(args):
    t = UITree(device.dump_ui())
    for v in ("raw", "filtered", "pruned"):
        s = t.render(v)
        print("--- %s: %d chars" % (v, len(s)))
        if v == args.show:
            print(s)


def cmd_collect(_):
    out = os.path.join(RUNS, "screens")
    os.makedirs(out, exist_ok=True)
    n = len(os.listdir(out))
    print("在手机上打开任意页面，按回车保存当前屏幕；输入 q 回车退出。保存到", out)
    while input("> ").strip().lower() != "q":
        xml = device.dump_ui()
        t = UITree(xml)
        pkg = (t.packages() or ["unknown"])[0]
        path = os.path.join(out, "%03d_%s.xml" % (n, pkg))
        with open(path, "w", encoding="utf-8") as f:
            f.write(xml)
        print("  saved %s  raw=%d filtered=%d pruned=%d chars" % (
            os.path.basename(path), len(t.render_raw()), len(t.render_filtered()), len(t.render_pruned())))
        n += 1


def cmd_task(args):
    tasks = load_tasks()
    run_one(tasks[args.id], args.variant, args.planning, args.safety, args.interrupt, tag="adhoc")


SUITES = {
    # Exp 1: observation representation (closed-loop planning, hybrid safety)
    "repr": [dict(variant=v, planning="closed", safety="hybrid") for v in ("pruned", "filtered", "raw")],
    # Exp 2: robustness to interruption, closed-loop re-planning vs one-shot static plan
    "replan": [dict(variant="pruned", planning=p, safety="hybrid", interrupt_at=2) for p in ("closed", "static")]
              + [dict(variant="pruned", planning="static", safety="hybrid")],
}


def cmd_exp(args):
    tasks = list(load_tasks().values())
    if args.only:
        tasks = [t for t in tasks if t["id"] in args.only.split(",")]
    suites = list(SUITES) if args.suite == "all" else [args.suite]
    for name in suites:
        for cfg in SUITES[name]:
            for rep in range(args.repeat):
                for t in tasks:
                    run_one(t, tag="%s#%d" % (name, rep), **cfg)


def cmd_stats(_):
    rows = [json.loads(l) for l in open(RESULTS, encoding="utf-8")]
    groups = {}
    for r in rows:
        k = (r.get("tag", "").split("#")[0], r["variant"], r["planning"], r["safety"], r.get("interrupt"))
        groups.setdefault(k, []).append(r)
    print("%-8s %-9s %-7s %-7s %-4s %4s %7s %7s %9s %8s" % (
        "suite", "variant", "plan", "safety", "intr", "n", "succ%", "steps", "tokens", "llm_s"))
    for k, rs in sorted(groups.items(), key=lambda x: str(x[0])):
        judged = [r for r in rs if r["success"] is not None]
        sr = 100.0 * sum(1 for r in judged if r["success"]) / max(1, len(judged))
        avg = lambda f: sum(f(r) for r in rs) / len(rs)
        print("%-8s %-9s %-7s %-7s %-4s %4d %6.1f%% %7.1f %9.0f %8.1f" % (
            k[0], k[1], k[2], k[3], k[4] or "-", len(rs), sr, avg(lambda r: r["n_steps"]),
            avg(lambda r: r["usage"].get("total_tokens", 0)), avg(lambda r: r["llm_latency"])))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("check")
    s = sub.add_parser("inspect"); s.add_argument("--show", default="pruned")
    sub.add_parser("collect")
    s = sub.add_parser("task"); s.add_argument("id")
    s.add_argument("--variant", default="pruned"); s.add_argument("--planning", default="closed")
    s.add_argument("--safety", default="hybrid"); s.add_argument("--interrupt", type=int)
    s = sub.add_parser("exp"); s.add_argument("suite", choices=list(SUITES) + ["all"])
    s.add_argument("--repeat", type=int, default=1); s.add_argument("--only")
    sub.add_parser("stats")
    args = p.parse_args()
    fn = {"check": cmd_check, "inspect": cmd_inspect, "collect": cmd_collect, "task": cmd_task,
          "exp": cmd_exp, "stats": cmd_stats}.get(args.cmd)
    if not fn:
        p.print_help()
        sys.exit(1)
    if args.cmd in ("task", "exp"):
        prev = device.use_adb_ime()
        try:
            fn(args)
        finally:
            device.restore_ime(prev)
            print("已恢复原输入法")
    else:
        fn(args)


if __name__ == "__main__":
    main()
