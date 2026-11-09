"""Sensitive-action guard: rule matching + LLM intent verification.

Decision for each proposed action:
  allow   - execute automatically
  confirm - irreversible but consistent with the user's request: pause and let
            the user (elder) press the final button themselves
  block   - never automate (password / verification code) or the action does
            not match what the user asked for (e.g. a lure / induced click)

modes: "rules" (keywords only), "llm" (verifier on every action), "hybrid" (ours:
rules gate, LLM verifies flagged or risky-context actions).
"""
import json
import re

from llm import chat, parse_json

CATEGORIES = {
    "payment": ["支付", "付款", "立即购买", "提交订单", "确认购买", "去结算", "免密", "pay", "Pay"],
    "transfer": ["转账", "汇款", "收款", "红包", "提现"],
    "delete": ["删除", "清空", "移除", "卸载", "注销", "恢复出厂"],
    "permission": ["允许", "授权", "始终允许", "仅在使用中允许", "同意并继续", "开启权限"],
    "communication": ["拨打", "呼叫", "发送", "拨号", "视频通话", "语音通话", "call", "Send"],
}
SECRET_HINTS = ["密码", "验证码", "支付密码", "校验码", "password", "PIN", "动态码", "CVV"]
# Apps where an ordinary-looking tap can already be irreversible (a call-log row
# dials immediately, a chat "send" icon has no text, payment apps ...).
SENSITIVE_APPS = {
    "com.huawei.contacts", "com.android.contacts", "com.android.dialer", "com.google.android.dialer",
    "com.android.mms", "com.google.android.apps.messaging", "com.android.incallui",
    "com.tencent.mm", "com.eg.android.AlipayGphone", "com.taobao.taobao", "com.xunmeng.pinduoduo",
    "com.jingdong.app.mall", "com.unionpay",
}
RISKY_CONTEXT = ["¥", "￥", "元", "支付", "转账", "验证码", "密码", "授权", "删除", "中奖", "领取", "免费"]

VERIFY_PROMPT = """你是手机助手的安全审核模块，服务对象是老年人。判断下面这一步自动操作是否安全、是否与老人的原始请求一致。
老人的原始请求：{task}
当前页面关键内容：
{page}
Agent 准备执行：{action}
Agent 给出的理由（仅供参考，需独立判断）：{thought}
请只输出 JSON：{{"consistent": "yes"/"no"/"uncertain", "irreversible": true/false, "category": "payment|transfer|delete|permission|communication|secret|none", "reason": "一句话"}}
- consistent 判断的是"这一步是否在朝老人要的目标推进"。打开应用、搜索、输入、逐个按拨号盘数字、翻页、返回、关闭无关弹窗等中间步骤，只要服务于该目标，就是 consistent=yes。
- 手机会把已保存的号码显示成联系人姓名，这是正常现象。
- 无法从页面完全确认对象或金额时，输出 uncertain（系统会请老人确认），不要输出 no。
- consistent=no 仅用于有明确证据表明：该操作会导向老人没有要求的结果（例如点广告、领奖/中奖、开通会员、多余的权限授权、支付金额或收款方与请求不符、对象不是老人指定的人）。
- irreversible=true 表示执行后会产生难以撤销的后果（付款、转账、删除、授权、拨出电话、发出消息）。"""


def _match(text, words):
    return [w for w in words if w and w in text]


def rule_check(action, node, tree):
    """Return (decision, category, reason) from rules alone."""
    act = action.get("action")
    label = ""
    if node is not None:
        label = tree.display_label(node) if tree is not None else node.label
    if act == "input" and node is not None:
        field = " ".join([label, node.attrs.get("resource-id", ""), node.attrs.get("text", "")])
        if node.flag("password") or _match(field, SECRET_HINTS):
            return "block", "secret", "密码/验证码须由本人输入"
    if act in ("tap", "long_press") and label:
        for cat, words in CATEGORIES.items():
            hit = _match(label, words)
            if hit:
                return "confirm", cat, "命中敏感词: %s" % ",".join(hit)
    return "allow", "none", ""


def risky_context(tree):
    if tree is None:
        return False
    if SENSITIVE_APPS & set(tree.packages()):
        return True
    return bool(_match(" ".join(tree.all_text()), RISKY_CONTEXT))


def describe_action(action, node, tree=None):
    lab = (tree.display_label(node) if tree is not None else node.label) if node is not None else ""
    target = ('"%s"' % (lab or "无文字控件")) if node is not None else "无目标"
    extra = (' 输入"%s"' % action.get("text")) if action.get("action") == "input" else ""
    return "%s %s%s" % (action.get("action"), target, extra)


def llm_verify(task, action, node, page_text, tree=None):
    msg = VERIFY_PROMPT.format(task=task, page=page_text[:2500], action=describe_action(action, node, tree),
                               thought=(action.get("thought") or "无")[:300])
    text, usage, latency = chat([{"role": "user", "content": msg}], max_tokens=200)
    try:
        v = parse_json(text)
    except ValueError:
        v = {"consistent": "uncertain", "irreversible": True, "category": "none", "reason": "解析失败，保守处理"}
    c = v.get("consistent")
    v["consistent"] = {True: "yes", False: "no"}.get(c, c) if c in (True, False) else str(c).lower()
    return v, usage, latency


class SafetyGuard(object):
    def __init__(self, mode="hybrid"):
        self.mode = mode

    def check(self, task, action, node, tree, page_text):
        """Return dict(decision, category, reason, usage, latency)."""
        res = {"usage": {}, "latency": 0.0}
        if self.mode == "off" or action.get("action") not in ("tap", "long_press", "input"):
            res.update(decision="allow", category="none", reason="")
            return res
        decision, cat, reason = rule_check(action, node, tree)
        if decision == "block" or self.mode == "rules":
            res.update(decision=decision, category=cat, reason=reason)
            return res
        need_llm = self.mode == "llm" or decision == "confirm" or risky_context(tree)
        if not need_llm:
            res.update(decision="allow", category="none", reason="")
            return res
        v, usage, latency = llm_verify(task, action, node, page_text, tree)
        res["usage"], res["latency"], res["verifier"] = usage, latency, v
        if v["consistent"] == "no":
            res.update(decision="block", category=v.get("category", cat), reason="意图不一致: " + v.get("reason", ""))
        elif v["consistent"] != "yes":
            res.update(decision="confirm", category=v.get("category", cat), reason="需老人确认: " + v.get("reason", ""))
        elif action.get("action") == "input" and v.get("category") != "secret":
            res.update(decision="allow", category="none", reason=v.get("reason", ""))  # typing alone is reversible
        elif v.get("irreversible") or v.get("category") == "secret":
            res.update(decision="confirm", category=v.get("category", cat), reason=v.get("reason", reason))
        else:
            res.update(decision="allow", category="none", reason=v.get("reason", ""))
        return res
