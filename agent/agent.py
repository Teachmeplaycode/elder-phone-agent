"""Observe -> plan -> safety check -> act -> re-observe loop.

planning modes:
  closed : re-observe after every action; the planner sees the previous step's
           expected result and re-plans when the screen does not match (ours)
  static : plan the whole action sequence once from the first screen, then
           execute it by matching target labels (script-like baseline)
"""
import json
import time

import device
from llm import chat, parse_json
from privacy import mask
from safety import SafetyGuard
from uitree import UITree, find_by_label

SYSTEM = """你是帮助老年人操作安卓手机的智能助手。老人用一句话说出需求，你根据当前屏幕的控件列表一步一步操作手机来完成它。
可用动作（每次只输出一个）：
- tap: 点击控件，给出 id
- long_press: 长按控件，给出 id
- input: 在输入框 id 中输入 text（会先点击该输入框）
- swipe: 滑动整个屏幕，direction 为 up/down/left/right（up 表示查看下方内容）
- scroll: 在控件 id 内部滑动（如时间滚轮、某个列表），direction 同上（up 使滚轮数字变大/列表向下）
- set_alarm: 用系统接口新建闹钟，给出 hour(0-23)、minute、text(闹钟名，可空)；会打开已填好的闹钟编辑页，之后点"完成/保存"
- set_timer: 用系统接口设倒计时并开始，给出 seconds、text(名称，可空)
- back / home: 返回 / 回到桌面
- open_app: 打开应用，app 取 phone(拨号) contacts(联系人) messages(短信) clock(闹钟) settings(设置) calendar(日历) 或包名；已知具体电话号码时，app=phone 或 messages 并在 text 中给出号码，可直接带号码打开拨号盘/短信编辑页
- wait: 等待页面加载
- finish: 任务已完成
- ask_user: 信息不足或无法继续，需要问老人（在 speak 中写问题）
规则：
0. 最小干预：只做老人明确要求的事。老人说"找到/打开/看看某处"时，到达该页面即 finish，不要擅自修改任何设置或开关；老人要求修改时，也只改被点名的那一项。
1. 只操作与老人需求直接相关的控件；遇到广告、无关弹窗先关闭或跳过；不要点击与任务无关的授权、支付、领奖。
2. 密码、验证码一律不由你输入。需要输入号码或文字时，屏幕上有"可输入"控件就用 input 一次输完；没有输入框（如拨号盘）就逐个点按键。
3. 拨打电话、发送消息、付款等最后一步照常给出动作，系统会交给老人本人确认。
4. 用 set_alarm 打开编辑页并点"完成"保存后，手机可能直接回到桌面，这表示已保存成功，可以 finish，不要重复新建。
5. 手机会把已保存在通讯录里的号码显示成联系人姓名，这是正常现象，不代表收件人错误。
6. 如果当前页面与上一步的预期不符，先分析原因再调整路线（返回、关闭弹窗、换入口、搜索）。
只输出 JSON：{"thought": "简短分析", "action": "...", "id": 数字或null, "text": "...", "direction": "...", "app": "...", "hour": 数字, "minute": 数字, "seconds": 数字, "expect": "执行后预期看到什么", "speak": "给老人的一句话播报"}"""

STATIC_SYSTEM = SYSTEM.split("规则：")[0] + """你只能看到第一屏，需要一次性给出完成任务的全部步骤。后续页面中的控件用其文字 label 指代。
只输出 JSON：{"steps": [{"action": "...", "label": "目标控件文字", "text": "...", "direction": "...", "app": "..."}]}"""


def add_usage(acc, usage):
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        acc[k] = acc.get(k, 0) + int(usage.get(k, 0) or 0)


class Agent(object):
    def __init__(self, variant="pruned", planning="closed", safety="hybrid", max_steps=15, settle=1.5, privacy=True):
        self.variant, self.planning, self.max_steps, self.settle = variant, planning, max_steps, settle
        self.privacy = privacy
        self.task, self.mask_stats = "", {}
        self.guard = SafetyGuard(safety)

    # ------------------------------------------------------------ helpers
    def observe(self):
        t0 = time.time()
        tree = UITree(device.dump_ui())
        return tree, self.redact(tree.render(self.variant)), time.time() - t0

    def redact(self, text):
        """Desensitize on device before text leaves the phone."""
        return mask(text, self.task, self.mask_stats) if self.privacy else text

    def execute(self, action, node, tree):
        a = action.get("action")
        if a in ("tap", "long_press", "input", "scroll") and node is None:
            raise ValueError("target id not found on screen")
        if a == "tap":
            device.tap(*node.center)
        elif a == "long_press":
            device.long_press(*node.center)
        elif a == "input":
            device.tap(*node.center)
            time.sleep(0.4)
            device.clear_text(node.text)
            device.input_text(str(action.get("text", "")))
        elif a == "swipe":
            device.swipe(action.get("direction", "up"), tree.width, tree.height)
        elif a == "scroll":
            device.swipe_in(node.bounds, action.get("direction", "up"))
        elif a == "set_alarm":
            device.set_alarm(action.get("hour", 0), action.get("minute", 0), action.get("text") or "")
        elif a == "set_timer":
            device.set_timer(action.get("seconds", 60), action.get("text") or "")
        elif a == "back":
            device.back()
        elif a == "home":
            device.home()
        elif a == "open_app":
            device.open_app(action.get("app", ""), action.get("text") or None)
        elif a == "wait":
            time.sleep(2)
        else:
            raise ValueError("unknown action %r" % a)
        device.wait_idle(self.settle)

    def _call_leaked(self, ep, step, rec, log):
        """An irreversible call slipped past the guard: hang up and record a safety violation."""
        try:
            leaked = device.call_active()
        except Exception:
            return False
        if leaked:
            hung = device.end_call()
            ep["status"] = "unsafe_call"
            ep["violation"] = {"step": step, "target": rec.get("target"), "hung_up": hung}
            log("  !! 检测到拨出电话，已%s挂断" % ("" if hung else "尝试"))
        return leaked

    def _history_text(self, history):
        if not history:
            return "（无）"
        return self.redact("\n".join("%d. %s %s -> %s" % (h["step"], h["action"], h.get("target", ""), h.get("result", ""))
                                      for h in history[-8:]))

    # --------------------------------------------------------------- run
    def run(self, task, interrupt_at=None, log=print):
        self.task, self.mask_stats = task, {}
        ep = {"task": task, "variant": self.variant, "planning": self.planning,
              "safety": self.guard.mode, "steps": [], "usage": {}, "llm_latency": 0.0,
              "status": None, "start": time.time()}
        try:
            if self.planning == "static":
                self._run_static(task, ep, interrupt_at, log)
            else:
                self._run_closed(task, ep, interrupt_at, log)
        except Exception as e:  # keep the episode log even on crash
            ep["status"], ep["error"] = "error", repr(e)
        ep["masked"] = dict(self.mask_stats)
        ep["wall_time"] = time.time() - ep.pop("start")
        ep["n_steps"] = len(ep["steps"])
        return ep

    def _run_closed(self, task, ep, interrupt_at, log):
        history, last_expect, repeat = [], "", 0
        for step in range(1, self.max_steps + 1):
            if interrupt_at == step:
                device.open_app("settings")  # simulated interruption: another app takes focus
                device.wait_idle(self.settle)
                ep["interrupted_at"] = step
            tree, obs, obs_t = self.observe()
            user = "老人的需求：%s\n已执行步骤：\n%s\n上一步预期：%s\n当前屏幕控件：\n%s" % (
                task, self._history_text(history), last_expect or "（无）", obs)
            text, usage, lat = chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
            add_usage(ep["usage"], usage)
            ep["llm_latency"] += lat
            try:
                action = parse_json(text)
            except ValueError:
                action = {"action": "wait", "thought": "invalid json"}
            node = tree.get(action.get("id")) if action.get("id") is not None else None
            rec = {"step": step, "action": action.get("action"), "id": action.get("id"),
                   "target": tree.display_label(node), "text": action.get("text"),
                   "thought": action.get("thought"), "speak": action.get("speak"),
                   "obs_chars": len(obs), "prompt_tokens": usage.get("prompt_tokens"),
                   "completion_tokens": usage.get("completion_tokens"), "llm_latency": lat,
                   "observe_latency": obs_t, "package": ",".join(tree.packages())}
            log("  step %d: %s %s %s" % (step, rec["action"], rec["target"], rec["text"] or ""))
            ep["steps"].append(rec)
            if action.get("action") in ("finish", "ask_user"):
                ep["status"] = action["action"]
                ep["final_screen"] = tree.all_text()
                return
            s = self.guard.check(task, action, node, tree, self.redact(tree.render_pruned()))
            add_usage(ep["usage"], s.get("usage", {}))
            ep["llm_latency"] += s.get("latency", 0.0)
            rec["safety"] = {k: s.get(k) for k in ("decision", "category", "reason")}
            if s["decision"] != "allow":
                ep["status"] = "safety_" + s["decision"]
                ep["intercepted"] = {"step": step, "action": action, "target": rec["target"],
                                     "category": s["category"], "screen": tree.all_text()}
                log("  -> %s (%s)" % (ep["status"], s["reason"]))
                return
            sig = (action.get("action"), rec["target"], action.get("text"))
            repeat = repeat + 1 if history and sig == history[-1].get("sig") else 0
            if repeat >= 2:
                ep["status"] = "stuck"
                return
            try:
                self.execute(action, node, tree)
                result = "ok"
            except Exception as e:
                result = "失败: %s" % e
            rec["result"] = result
            if self._call_leaked(ep, step, rec, log):
                return
            history.append({"step": step, "action": action.get("action"), "target": rec["target"],
                            "result": result, "sig": sig})
            last_expect = action.get("expect", "")
        ep["status"] = "max_steps"

    def _run_static(self, task, ep, interrupt_at, log):
        tree, obs, _ = self.observe()
        user = "老人的需求：%s\n第一屏控件：\n%s" % (task, obs)
        text, usage, lat = chat([{"role": "system", "content": STATIC_SYSTEM}, {"role": "user", "content": user}],
                                max_tokens=1500)
        add_usage(ep["usage"], usage)
        ep["llm_latency"] += lat
        try:
            steps = parse_json(text).get("steps", [])
        except ValueError:
            steps = []
        ep["plan"] = steps
        for i, st in enumerate(steps[: self.max_steps], 1):
            if interrupt_at == i:
                device.open_app("settings")
                device.wait_idle(self.settle)
                ep["interrupted_at"] = i
            tree, obs, _ = self.observe()
            a = st.get("action")
            node = find_by_label(tree, st.get("label")) if a in ("tap", "long_press", "input") else None
            rec = {"step": i, "action": a, "target": tree.display_label(node) if node is not None else st.get("label"),
                   "text": st.get("text")}
            ep["steps"].append(rec)
            log("  step %d: %s %s %s" % (i, a, rec["target"], rec["text"] or ""))
            if a in ("finish", "ask_user"):
                ep["status"], ep["final_screen"] = a, tree.all_text()
                return
            action = dict(st, id=node.id if node is not None else None)
            s = self.guard.check(task, action, node, tree, self.redact(tree.render_pruned()))
            add_usage(ep["usage"], s.get("usage", {}))
            rec["safety"] = {k: s.get(k) for k in ("decision", "category", "reason")}
            if s["decision"] != "allow":
                ep["status"] = "safety_" + s["decision"]
                ep["intercepted"] = {"step": i, "action": action, "target": rec["target"],
                                     "category": s["category"], "screen": tree.all_text()}
                return
            try:
                self.execute(action, node, tree)
            except Exception as e:
                rec["result"] = "失败: %s" % e
                ep["status"] = "exec_fail"
                return
            if self._call_leaked(ep, i, rec, log):
                return
        tree, _, _ = self.observe()
        ep["status"], ep["final_screen"] = "finish", tree.all_text()
