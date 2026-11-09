"""UI tree parsing and the three observation representations compared in the paper.

  raw      : the full accessibility XML, each node annotated with an id
  filtered : visible + (interactive or textual) nodes, flat list, all attributes
  pruned   : ours - filtered, then interactive containers absorb descendant
             text (semantic merge), decorative / duplicate nodes dropped,
             obfuscated resource ids removed, one compact line per node
"""
import re
import xml.etree.ElementTree as ET

BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
INTERACTIVE_ATTRS = ("clickable", "long-clickable", "checkable", "scrollable")
MAX_LABEL = 40
SKIP_PACKAGES = ("com.android.systemui", "android")
EDITABLE_CLASSES = ("EditText", "AutoCompleteTextView", "SearchView$SearchAutoComplete")


def is_skipped_package(pkg):
    """System status bar and on-screen keyboards are not part of the app being operated."""
    return pkg in SKIP_PACKAGES or "inputmethod" in pkg or "adbkeyboard" in pkg
FLAG_ORDER = ["可输入", "密码", "可点", "可长按", "可滚动", "已选中", "未选中", "不可用"]

# Short Chinese role names keep the prompt compact and readable for the planner.
ROLE = [
    ("EditText", "输入框"), ("Switch", "开关"), ("CheckBox", "复选框"), ("RadioButton", "单选"),
    ("AutoCompleteTextView", "输入框"), ("ImageButton", "图标按钮"), ("Button", "按钮"), ("RecyclerView", "列表"),
    ("ListView", "列表"), ("ScrollView", "滚动区"), ("ViewPager", "翻页区"),
    ("TabWidget", "标签栏"), ("ImageView", "图片"), ("TextView", "文本"),
]


class Node(object):
    __slots__ = ("id", "attrs", "children", "parent", "bounds")

    def __init__(self, nid, attrs, parent):
        self.id = nid
        self.attrs = attrs
        self.children = []
        self.parent = parent
        m = BOUNDS_RE.match(attrs.get("bounds", ""))
        self.bounds = tuple(int(v) for v in m.groups()) if m else (0, 0, 0, 0)

    def flag(self, name):
        return self.attrs.get(name) == "true"

    @property
    def cls(self):
        return self.attrs.get("class", "")

    @property
    def text(self):
        return clean(self.attrs.get("text"))

    @property
    def desc(self):
        return clean(self.attrs.get("content-desc"))

    @property
    def label(self):
        return self.text or self.desc

    @property
    def editable(self):
        return any(k in self.cls for k in EDITABLE_CLASSES) or \
            self.attrs.get("resource-id", "").endswith("search_src_text")

    @property
    def interactive(self):
        return self.editable or any(self.flag(a) for a in INTERACTIVE_ATTRS)

    @property
    def center(self):
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def area(self):
        x1, y1, x2, y2 = self.bounds
        return max(0, x2 - x1) * max(0, y2 - y1)

    def role(self):
        for key, name in ROLE:
            if key in self.cls:
                return name
        return "控件" if self.interactive else "元素"

    def iter(self):
        yield self
        for c in self.children:
            for n in c.iter():
                yield n


class UITree(object):
    def __init__(self, xml):
        self.xml = xml
        self.nodes = []
        root = ET.fromstring(xml.encode("utf-8"))
        self.roots = [self._build(e, None) for e in root]
        self.width, self.height = self._screen()

    def _build(self, elem, parent):
        n = Node(len(self.nodes), dict(elem.attrib), parent)
        self.nodes.append(n)
        n.children = [self._build(c, n) for c in elem if c.tag == "node"]
        return n

    def _screen(self):
        w = max([n.bounds[2] for n in self.nodes] or [1080])
        h = max([n.bounds[3] for n in self.nodes] or [2400])
        return w, h

    def get(self, nid):
        if nid is None or not (0 <= int(nid) < len(self.nodes)):
            return None
        return self.nodes[int(nid)]

    def visible(self, n):
        x1, y1, x2, y2 = n.bounds
        return n.area > 0 and x2 > 0 and y2 > 0 and x1 < self.width and y1 < self.height

    def packages(self):
        return sorted({n.attrs.get("package", "") for n in self.nodes} - {""})

    def all_text(self):
        return [n.label for n in self.nodes if n.label]

    # ---------------------------------------------------------------- raw
    def render_raw(self):
        out = []

        def walk(n, depth):
            attrs = " ".join('%s="%s"' % (k, v) for k, v in n.attrs.items())
            out.append("%s<node id=\"%d\" %s>" % ("  " * depth, n.id, attrs))
            for c in n.children:
                walk(c, depth + 1)
        for r in self.roots:
            walk(r, 0)
        return "\n".join(out)

    # ----------------------------------------------------------- filtered
    def render_filtered(self):
        lines = []
        for n in self.nodes:
            if not self.visible(n) or not (n.interactive or n.label):
                continue
            flags = [a for a in INTERACTIVE_ATTRS + ("checked", "selected", "password") if n.flag(a)]
            lines.append('[%d] class=%s text="%s" desc="%s" rid=%s bounds=%s %s' % (
                n.id, n.cls, n.text, n.desc, n.attrs.get("resource-id", ""),
                n.attrs.get("bounds", ""), ",".join(flags)))
        return "\n".join(lines)

    # ------------------------------------------------------------- pruned
    def _descendant_labels(self, n, limit=3):
        labels = []
        for d in n.iter():
            if d is n or not self.visible(d):
                continue
            if d.interactive and d is not n:
                continue  # nested controls are listed on their own
            if d.label and d.label not in labels:
                labels.append(d.label)
            if len(labels) >= limit:
                break
        return labels

    def _inside_interactive(self, n):
        p = n.parent
        while p is not None:
            if p.interactive and not p.flag("scrollable"):
                return True
            p = p.parent
        return False

    def _has_interactive_desc(self, n):
        return any(d.interactive for d in n.iter() if d is not n and self.visible(d))

    def _context_label(self, n, levels=3):
        """Text of the row a bare control (e.g. a switch) belongs to."""
        p = n.parent
        for _ in range(levels):
            if p is None:
                break
            labs = self._descendant_labels(p, limit=2)
            if labs:
                return " ".join(labs)
            p = p.parent
        return ""

    def _flags(self, n):
        flags = []
        if n.editable:
            flags.append("可输入")
            if n.flag("password"):
                flags.append("密码")
        if n.flag("clickable") or n.flag("checkable"):
            flags.append("可点")
        if n.flag("long-clickable"):
            flags.append("可长按")
        if n.flag("scrollable"):
            flags.append("可滚动")
        if n.flag("checkable"):
            flags.append("已选中" if n.flag("checked") else "未选中")
        if "enabled" in n.attrs and not n.flag("enabled"):
            flags.append("不可用")
        return flags

    def pruned_nodes(self):
        """Return [(node, role, label, flags)] for the compact representation.

        Rules: (1) drop invisible nodes and the system status bar; (2) an
        interactive node without text absorbs the text of its non-interactive
        descendants; (3) unlabeled wrappers that only contain other controls are
        dropped; (4) bare switches/checkboxes get the text of their row;
        (5) when a control and its ancestor share a label, keep one line with
        merged flags; (6) obfuscated resource ids are removed.
        """
        items = []
        for n in self.nodes:
            if not self.visible(n) or is_skipped_package(n.attrs.get("package", "")):
                continue
            if n.interactive:
                own = n.label
                scroll_only = n.flag("scrollable") and not n.flag("clickable")
                label = own if scroll_only else (own or " / ".join(self._descendant_labels(n)))
                rid = short_rid(n.attrs.get("resource-id", ""))
                if not label and n.editable:
                    label = n.attrs.get("hint", "") or "(空输入框)"
                if not label and n.flag("checkable"):
                    ctx = self._context_label(n)
                    label = ("%s（%s）" % (n.role(), ctx)) if ctx else ("#" + rid if rid else "")
                if not label and not scroll_only:
                    if self._has_interactive_desc(n):
                        continue  # wrapper around other controls
                    if not rid and n.area >= 0.02 * self.width * self.height:
                        continue  # large unlabeled container
                if not label:
                    label = ("#" + rid) if rid else ("" if scroll_only else "(无文字图标)")
                items.append([n, n.role(), truncate(label), self._flags(n)])
            elif n.label and not self._inside_interactive(n):
                items.append([n, "文本", truncate(n.label), []])
        # (5) merge ancestor/descendant pairs with the same label
        by_label = {}
        for it in items:
            by_label.setdefault(it[2], []).append(it)
        drop = set()
        for group in by_label.values():
            if len(group) < 2:
                continue
            ids = {it[0].id: it for it in group}
            for it in group:
                p = it[0].parent
                while p is not None:
                    if p.id in ids and p.id not in drop:
                        anc = ids[p.id]
                        it[3] = merge_flags(it[3], anc[3])
                        drop.add(p.id)
                    p = p.parent
        out, seen = [], set()
        for n, role, label, flags in items:
            key = (role, label, n.bounds)
            if n.id in drop or key in seen:
                continue
            seen.add(key)
            out.append((n, role, label, flags))
        return out

    def display_label(self, n):
        """Label of a node as the planner saw it (merged text), for logs and the safety check."""
        if n is None:
            return ""
        if not hasattr(self, "_labels"):
            self._labels = {m.id: lab for m, role, lab, flags in self.pruned_nodes()}
        return self._labels.get(n.id) or n.label or " / ".join(self._descendant_labels(n))

    def render_pruned(self):
        pkg = [p for p in self.packages() if not is_skipped_package(p)]
        lines = ["当前应用: %s" % (",".join(pkg) or "未知")]
        for n, role, label, flags in self.pruned_nodes():
            y = n.center[1] * 100 // max(1, self.height)
            lines.append('[%d] %s "%s" %s @%d%%' % (n.id, role, label, " ".join(flags), y))
        return "\n".join(lines)

    def render(self, variant):
        return {"raw": self.render_raw, "filtered": self.render_filtered,
                "pruned": self.render_pruned}[variant]()


def short_rid(rid):
    """Keep resource-id names that look meaningful; drop obfuscated ones like 'a3b'."""
    name = rid.split("/")[-1] if rid else ""
    if len(name) < 4 or not re.search(r"[a-z]{3,}", name) or re.fullmatch(r"[a-z]{1,2}\d*[a-z]?\d*", name):
        return ""
    return name


INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def clean(s):
    """Strip bidi / zero-width control characters that break text matching."""
    return INVISIBLE_RE.sub("", s or "").strip()


def merge_flags(a, b):
    s = set(a) | set(b)
    return [f for f in FLAG_ORDER if f in s]


def truncate(s, n=MAX_LABEL):
    s = re.sub(r"\s+", " ", s)
    return s if len(s) <= n else s[: n - 1] + "…"


def find_by_label(tree, label):
    """Locate a node by text for the static-plan baseline (ids change across screens)."""
    if not label:
        return None
    best = None
    for n, role, lab, flags in tree.pruned_nodes():
        if lab == label:
            return n
        if best is None and label in lab:
            best = n
    return best
