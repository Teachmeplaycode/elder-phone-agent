"""On-device desensitization before anything is sent to the cloud planner.

Masks phone numbers that the user did not mention, verification / pickup codes,
ID-card and bank-card numbers. Contact names are kept (needed to find people);
amounts are kept (needed by the safety check).
"""
import re

CODE_RE = re.compile(r"((?:验证码|校验码|动态码|取件码|提货码|取餐码|密码|凭)[^\d]{0,6})(\d[\d\- ]{2,10}\d)")
ID_RE = re.compile(r"(?<!\d)\d{6}(?:19|20)\d{2}[01]\d[0-3]\d\d{3}[\dXx](?!\d)")
LONG_DIGITS_RE = re.compile(r"(?<![\d.])\+?\d[\d \-]{5,22}\d(?![\d.])")


def _digits(s):
    return re.sub(r"\D", "", s)


def mask(text, task="", stats=None):
    """Return text with sensitive numbers replaced; numbers that appear in the task are kept."""
    keep = {_digits(m) for m in re.findall(r"\d[\d \-]{3,}\d", task)}  # numbers the user mentioned
    stats = stats if stats is not None else {}

    def code(m):
        stats["code"] = stats.get("code", 0) + 1
        return m.group(1) + "[已隐藏]"

    def ident(m):
        stats["id"] = stats.get("id", 0) + 1
        return "[证件号]"

    def number(m):
        d = _digits(m.group(0))
        if len(d) < 7 or any(k in d or d in k for k in keep):
            return m.group(0)
        if d.startswith("86") and len(d) == 13:
            d = d[2:]
        stats["number"] = stats.get("number", 0) + 1
        if len(d) >= 15:
            return "[卡号]"
        return "[号码%s]" % d[-2:]  # keep last 2 digits so different numbers stay distinguishable

    text = CODE_RE.sub(code, text)
    text = ID_RE.sub(ident, text)
    text = LONG_DIGITS_RE.sub(number, text)
    return text
