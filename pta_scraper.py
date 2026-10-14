#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PTA(拼题A / pintia.cn) 理论题抓取工具
--------------------------------------
作用：自动翻页、滚动加载 PTA 的题目集页面，把「单选 / 多选 / 判断 / 填空 / 简答」
      这类理论题整理成一个排版整齐的 Markdown 文件。

用法（新手看这里）：
    1. 双击同目录下的《运行抓取.bat》
       （第一次运行会自动联网安装所需组件，需等几分钟）
    2. 在弹出的浏览器里登录 PTA，打开你要抓的题目集/作业页面
    3. 回到黑色命令行窗口，按回车
    4. 等待提示「抓取完成」，同目录下会生成 PTA理论题.md

也可以手动运行：python pta_scraper.py [题目集网址]

只依赖 playwright 一个库，其余全部使用 Python 自带功能。
"""

import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from html import unescape as html_unescape
from html.parser import HTMLParser
from pathlib import Path

# --------------------------------------------------------------------------
# 基本设置
# --------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
OUT_MD = HERE / "PTA理论题.md"          # 最终生成的 Markdown
RAW_DIR = HERE / "调试用原始数据"        # 原始 JSON，出问题时方便排查
PROFILE_DIR = HERE / "浏览器登录信息"    # 保存登录状态，下次不用重新登录

HOME_URL = "https://pintia.cn/"

# 是否在 Markdown 里附带答案。
# 按需求默认关闭：文档里只出题目和选项，不带答案。
# 想连答案一起导出，把下面改成 True 即可。
INCLUDE_ANSWER = False

# 理论题题型（PTA 实际使用的枚举名）
TYPE_CN = {
    "TRUE_OR_FALSE": "判断题",
    "MULTIPLE_CHOICE": "单选题",
    "MULTIPLE_CHOICE_MORE_THAN_ONE_ANSWER": "多选题",
    "MULTIPLE_SELECT": "多选题",
    "FILL_IN_THE_BLANK": "填空题",
    "FILL_IN_THE_BLANK_FOR_PROGRAMMING": "程序填空题",
    "CODE_COMPLETION": "代码填空题",
    "SHORT_ANSWER": "简答题",
    "SUBJECTIVE": "简答题",
    "ESSAY": "简答题",
    "PROGRAMMING": "编程题",
}

# 题型枚举 -> 网页上题型标签的中文名（接口拿不到时靠点标签翻页）
TYPE_TAB_NAME = {
    "TRUE_OR_FALSE": "判断题",
    "MULTIPLE_CHOICE": "单选题",
    "MULTIPLE_CHOICE_MORE_THAN_ONE_ANSWER": "多选题",
    "MULTIPLE_SELECT": "多选题",
    "FILL_IN_THE_BLANK": "填空题",
    "FILL_IN_THE_BLANK_FOR_PROGRAMMING": "程序填空题",
    "CODE_COMPLETION": "代码填空题",
    "SHORT_ANSWER": "简答题",
    "SUBJECTIVE": "简答题",
}

# 实验题 / 编程题，默认排除
PROGRAMMING_TYPES = {"PROGRAMMING"}

# 题型在文档里的排列顺序（跟 PTA 网页上标签页的顺序一致）
TYPE_ORDER = [
    "TRUE_OR_FALSE",
    "MULTIPLE_CHOICE",
    "MULTIPLE_CHOICE_MORE_THAN_ONE_ANSWER",
    "MULTIPLE_SELECT",
    "FILL_IN_THE_BLANK",
    "FILL_IN_THE_BLANK_FOR_PROGRAMMING",
    "CODE_COMPLETION",
    "SHORT_ANSWER",
    "SUBJECTIVE",
    "ESSAY",
]


def type_rank(t):
    """越小越靠前；未知题型排在最后。"""
    try:
        return TYPE_ORDER.index(t)
    except ValueError:
        return len(TYPE_ORDER) + (1 if t else 0)

# 不是题目对象的 type（题目集、学校等），用于过滤噪声
NON_PROBLEM_TYPES = {
    "EXERCISE", "HOMEWORK", "CONTEST", "PUBLIC", "SCHOOL", "ORGANIZATION",
    "COLLECTION", "EXAM",
}


# --------------------------------------------------------------------------
# HTML -> Markdown 轻量转换（只用标准库，避免额外安装依赖）
# --------------------------------------------------------------------------

class HtmlToMarkdown(HTMLParser):
    """把题干里的 HTML 转成可读的 Markdown 文本。"""

    BLOCK_TAGS = {
        "p", "div", "section", "article", "header", "footer", "blockquote",
        "figure", "figcaption", "hr", "dl", "dt", "dd",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.pre_depth = 0
        self.skip_depth = 0
        self.list_stack = []
        self.ol_counter = []
        self.table_stack = []

    def _newline(self):
        tail = "".join(self.out[-3:])
        if tail and not tail.endswith("\n"):
            self.out.append("\n")

    # -- 开始标签 ---------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip_depth += 1
            return
        if self.skip_depth:
            return

        if tag == "br":
            self.out.append("\n")
        elif tag == "img":
            src = a.get("src", "")
            if src:
                self.out.append("\n![题目图片](%s)\n" % src)
        elif tag == "pre":
            self.pre_depth += 1
            self.out.append("\n\n```\n")
        elif tag == "code":
            if self.pre_depth == 0:
                self.out.append("`")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "table":
            self.table_stack.append({"sep": False, "cols": 0})
            self.out.append("\n\n")
        elif tag == "tr":
            self._newline()
            if self.table_stack:
                self.table_stack[-1]["cols"] = 0
        elif tag in ("td", "th"):
            if self.table_stack and self.table_stack[-1]["cols"] == 0:
                self.out.append("| ")
            else:
                self.out.append(" | ")
            if self.table_stack:
                self.table_stack[-1]["cols"] += 1
        elif tag in ("ul", "ol"):
            self.list_stack.append(tag)
            self.ol_counter.append(0)
            self.out.append("\n")
        elif tag == "li":
            if self.list_stack and self.list_stack[-1] == "ol":
                self.ol_counter[-1] += 1
                prefix = "%d. " % self.ol_counter[-1]
            else:
                prefix = "- "
            indent = "  " * max(0, len(self.list_stack) - 1)
            self.out.append("\n" + indent + prefix)
        elif tag in self.BLOCK_TAGS:
            self.out.append("\n")

    # -- 结束标签 ---------------------------------------------------------
    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return

        if tag == "pre":
            self.pre_depth = max(0, self.pre_depth - 1)
            self.out.append("\n```\n")
        elif tag == "code":
            if self.pre_depth == 0:
                self.out.append("`")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "tr":
            self.out.append(" |\n")
            if self.table_stack:
                t = self.table_stack[-1]
                if not t["sep"] and t["cols"]:
                    self.out.append("|" + " --- |" * t["cols"] + "\n")
                    t["sep"] = True
        elif tag == "table":
            if self.table_stack:
                self.table_stack.pop()
            self.out.append("\n")
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
            if self.ol_counter:
                self.ol_counter.pop()
            self.out.append("\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") or tag in self.BLOCK_TAGS:
            self.out.append("\n")

    # -- 文本 -------------------------------------------------------------
    def handle_data(self, data):
        if self.skip_depth:
            return
        if self.pre_depth:
            self.out.append(data)
        else:
            self.out.append(re.sub(r"\s+", " ", data))

    # -- 结果 -------------------------------------------------------------
    def markdown(self):
        text = "".join(self.out)
        text = text.replace("\xa0", " ").replace("\u200b", "")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"\*\*\s*\*\*", "", text)
        return text.strip()


# 只有出现「像样的 HTML 标签」时才按 HTML 解析。
# 否则题干里的 if (x < 0) 会被误判成标签，把换行全部吃掉。
HTML_TAG_RE = re.compile(
    r"<\s*/?\s*(p|br|div|span|img|pre|code|strong|b|em|i|u|s|table|thead|tbody"
    r"|tr|td|th|ul|ol|li|h[1-6]|blockquote|sup|sub|font|a|hr|figure|figcaption"
    r"|center|small|big|kbd|samp|var|tt)\b[^<>]*>",
    re.IGNORECASE,
)


def html_to_md(raw):
    """容错版 HTML -> Markdown。纯文本会原样返回。"""
    if not raw:
        return ""
    if not isinstance(raw, str):
        raw = str(raw)
    if not HTML_TAG_RE.search(raw):
        return html_unescape(raw)
    try:
        p = HtmlToMarkdown()
        p.feed(raw)
        p.close()
        return p.markdown()
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw)


# PTA 题干里的内部占位标记，写成 ~@[](1) 或 *@[](1)，对阅读没有意义
PLACEHOLDER_RE = re.compile(r"[~*]?@\s*\[[^\]]*\]\s*\([^)]*\)")


def fix_fences(text):
    """保证 ``` 代码块前后各有一个空行，否则 Markdown 渲染会串行。"""
    lines = text.split("\n")
    out = []
    in_fence = False
    for ln in lines:
        if ln.lstrip().startswith("```"):
            indent = len(ln) - len(ln.lstrip())
            fence = ln.strip() if indent <= 3 else ln.rstrip()
            if not in_fence:
                if out and out[-1].strip():
                    out.append("")
                out.append(fence)
                in_fence = True
            else:
                out.append(fence)
                out.append("")
                in_fence = False
        else:
            out.append(ln.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def clean_body(raw):
    """清理题干正文：去掉内部标记、规范空行、修正代码块间距。"""
    if not raw:
        return ""
    text = html_to_md(raw)
    text = PLACEHOLDER_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return fix_fences(text.strip())


def to_plain(raw):
    """压成一行纯文本，用于标题。"""
    if not raw:
        return ""
    text = html_to_md(raw)
    text = PLACEHOLDER_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _squash(s):
    """去掉所有空白和标点，只留内容，用于比较两段文字是否重复。"""
    s = PLACEHOLDER_RE.sub("", s or "")
    s = re.sub(r"[\s`~!@#$%^&*()\-_=+\[\]{};:'\",.<>/?\\|，。、；：？！“”‘’（）《》【】…—]+", "", s)
    return s


def _squash_loose(s):
    """更宽松的指纹：连数字也去掉（网页上会混入代码行号）。"""
    return re.sub(r"\d+", "", _squash(s))


def same_start(a, b, n=16):
    """判断两段文字开头是否基本相同（用于识别 title 与 content 重复）。"""
    na, nb = _squash(a), _squash(b)
    m = min(len(na), len(nb), n)
    return m >= 8 and na[:m] == nb[:m]


# --------------------------------------------------------------------------
# 从接口返回的 JSON 中挖题目
# --------------------------------------------------------------------------

ANSWER_KEYS = (
    "answer", "correctAnswer", "referenceAnswer", "standardAnswer",
    "answerList", "correctAnswers", "trueAnswer", "rightAnswer",
)


def walk_json(obj):
    """递归遍历 JSON，产出所有 dict。"""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def looks_like_problem(d):
    """判断一个 dict 是不是题目对象。

    要求「题型是大写枚举」+「带题号/选项/分值」等硬特征，
    以免把题干内部的富文本节点或题目集对象误当成题目。
    """
    if not isinstance(d, dict):
        return False

    t = d.get("type") or d.get("problemType") or d.get("questionType")
    if not isinstance(t, str):
        return False
    t = t.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,60}", t):
        return False
    if t in NON_PROBLEM_TYPES:
        return False

    label = str(d.get("label") or "").strip()
    choices = d.get("choices") or d.get("options")
    has_choices = isinstance(choices, list) and len(choices) > 0
    has_pid = bool(d.get("problemSetProblemId") or d.get("id") or d.get("problemId"))
    has_body = bool(d.get("content") or d.get("title") or d.get("description"))

    score = d.get("score")
    has_score = isinstance(score, (int, float)) or (
        isinstance(score, str) and score.strip().replace(".", "", 1).isdigit())

    if has_choices:
        return True
    if re.fullmatch(r"\d+\s*[-\u2013\u2014]\s*\d+", label):
        return True
    if has_pid and has_body and (has_score or "problemPoolIndex" in d):
        return True
    return False


def pick_answer(d):
    """从题目对象里找出标准答案（如果有公开）。"""
    for k in ANSWER_KEYS:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]

    choices = d.get("choices") or d.get("options")
    if isinstance(choices, list):
        picked = []
        for i, c in enumerate(choices):
            if not isinstance(c, dict):
                continue
            if c.get("isCorrect") or c.get("correct") or c.get("right"):
                picked.append(c.get("id") or c.get("label") or chr(65 + i))
        if picked:
            return picked

    for k in ("problem", "problemSetProblem", "question"):
        v = d.get(k)
        if isinstance(v, dict):
            got = pick_answer(v)
            if got not in (None, "", [], {}):
                return got
    return None


def extract_submission_answer(detail):
    """从提交详情里取出作答内容。

    PTA 的形态是 {"trueOrFalseSubmissionDetail": {"answer": "FALSE"}}，
    选择题可能是 answer/answers，填空题可能是 answers 列表。
    """
    if not isinstance(detail, dict):
        return None
    for k, v in detail.items():
        if k.endswith("SubmissionDetail") and isinstance(v, dict):
            for kk in ("answer", "answers", "content", "code"):
                if kk in v and v[kk] not in (None, "", [], {}):
                    return v[kk]
    for kk in ("answer", "answers", "content"):
        if kk in detail and detail[kk] not in (None, "", [], {}):
            return detail[kk]
    return None


def collect_submitted_answers(captured):
    """扫描所有接口数据，收集「标准答案」和「你的作答」，按题目 id 索引。"""
    official, mine = {}, {}
    for item in captured:
        d = item.get("data")
        if not isinstance(d, dict):
            continue

        m = d.get("submissionDetailByProblemSetProblemId")
        if isinstance(m, dict):
            for pid, det in m.items():
                a = extract_submission_answer(det)
                if a not in (None, "", [], {}):
                    official[str(pid)] = a

        sub = d.get("submission")
        if isinstance(sub, dict):
            for det in (sub.get("submissionDetails") or []):
                if not isinstance(det, dict):
                    continue
                pid = det.get("problemSetProblemId")
                a = extract_submission_answer(det)
                if pid and a not in (None, "", [], {}):
                    mine[str(pid)] = a
    return official, mine


def as_text(v):
    """只接受字符串形态的正文，其它类型（富文本对象等）忽略掉。"""
    return v if isinstance(v, str) else ""


# 选项区的开头：A. / A、/ A / A）
INLINE_OPT_START_RE = re.compile(r"^\s*A\s*(?:[.、．)）:：]\s*)?$|^\s*A\s*[.、．)）:：]\s")
INLINE_OPT_ANY_RE = re.compile(r"^\s*([A-H])\s*(?:[.、．)）:：]|\s*$)", re.M)


def strip_inline_options(text):
    """题干里有时会把选项也抄一遍（A. xxx / B. xxx），去掉它。

    PTA 真正的选项文本在 problemConfig 里，正文里的这份是重复的。
    """
    lines = (text or "").split("\n")
    for i, ln in enumerate(lines):
        if not INLINE_OPT_START_RE.match(ln):
            continue
        found = set(INLINE_OPT_ANY_RE.findall("\n".join(lines[i:])))
        if len(found) >= 2 and "A" in found:
            return "\n".join(lines[:i]).rstrip()
    return text


def config_choices(d):
    """PTA 的选项文本藏在 problemConfig 里，例如
    {"multipleChoiceProblemConfig": {"choices": ["7", "6", "5", "4"]}}。
    """
    cfg = d.get("problemConfig")
    if not isinstance(cfg, dict):
        return []
    for k, v in cfg.items():
        if k.endswith("ProblemConfig") and isinstance(v, dict):
            ch = v.get("choices")
            if isinstance(ch, list) and ch and all(isinstance(x, str) for x in ch):
                return [html_unescape(x) for x in ch]
    return []


def config_blanks(d):
    """填空题的空格位置（字符下标），例如
    {"fillInTheBlankProblemConfig": {"blanks": [{"index": 189}, {"index": 198}]}}。
    """
    cfg = d.get("problemConfig")
    if not isinstance(cfg, dict):
        return []
    for k, v in cfg.items():
        if k.endswith("ProblemConfig") and isinstance(v, dict):
            bs = v.get("blanks")
            if isinstance(bs, list):
                idx = [b["index"] for b in bs
                       if isinstance(b, dict) and isinstance(b.get("index"), int)]
                if idx:
                    return sorted(set(idx))
    return []


BLANK_MARK = " ____ "


def mark_blanks(raw, blanks):
    """在填空题的作答位置插上下划线，方便一眼看出填哪里。"""
    for i in sorted(blanks, reverse=True):
        if 0 <= i <= len(raw):
            raw = raw[:i] + BLANK_MARK + raw[i:]
    return raw


def normalize_problem(d):
    """把原始题目对象整理成统一结构。"""
    t = str(d.get("type") or d.get("problemType") or d.get("questionType") or "")
    t = t.strip().upper()

    label = str(d.get("label") or d.get("problemLabel") or "").strip()

    title = to_plain(as_text(d.get("title")))
    raw_content = (as_text(d.get("content")) or as_text(d.get("description")) or
                   as_text(d.get("problemContent")) or as_text(d.get("text")))
    blanks = config_blanks(d)
    if blanks and raw_content:
        raw_content = mark_blanks(raw_content, blanks)
    content = clean_body(raw_content)

    if not content and not title:
        return None
    if not content:
        content, title = title, ""

    # ---- 选项：优先 problemConfig，其次题目对象自带的 choices ----
    choices = []
    raw_choices = d.get("choices") or d.get("options") or []
    if isinstance(raw_choices, list) and raw_choices:
        for i, c in enumerate(raw_choices):
            if isinstance(c, dict):
                cid = c.get("id") or c.get("label") or chr(65 + i)
                text = clean_body(as_text(c.get("content")) or as_text(c.get("text")))
                correct = bool(c.get("isCorrect") or c.get("correct") or c.get("right"))
            else:
                cid, text, correct = chr(65 + i), clean_body(as_text(c)), False
            if text.strip():
                choices.append({"id": str(cid), "text": text.strip(), "correct": correct})
    else:
        for i, text in enumerate(config_choices(d)):
            text = clean_body(text)
            if text.strip():
                choices.append({"id": chr(65 + i), "text": text.strip(), "correct": False})

    # 正文里重复抄了一遍选项时，把那一块删掉
    if choices:
        content = strip_inline_options(content)

    # title 常常就是正文的第一句，重复显示会很难看
    if title and same_start(title, content):
        title = ""

    answer = pick_answer(d)
    solution = clean_body(as_text(d.get("solution")))
    if solution and same_start(solution, content):
        solution = ""

    score = d.get("score")
    try:
        score = float(score)
        if score == int(score):
            score = int(score)
    except Exception:
        score = None

    order = d.get("problemPoolIndex")
    if not isinstance(order, int):
        order = d.get("indexInProblemPool")
    if not isinstance(order, int):
        order = None

    pid = str(d.get("problemSetProblemId") or d.get("id") or d.get("problemId") or "")
    key = pid or (label or _squash(title or content)[:40])

    return {
        "key": key,
        "label": label,
        "order": order,
        "type": t,
        "title": title,
        "content": content,
        "choices": choices,
        "answer": answer,
        "user_answer": None,
        "solution": solution,
        "score": score,
        "source": "接口",
    }


def richness(q):
    """信息量打分，同一道题去重时保留最全的那条。"""
    s = 0
    if q.get("content"):
        s += min(len(q["content"]) // 20, 20)
    if q.get("choices"):
        s += 5 + len(q["choices"])
    if q.get("answer") not in (None, "", [], {}):
        s += 10
    if q.get("title"):
        s += 2
    if q.get("label"):
        s += 1
    if q.get("type"):
        s += 3
    return s


# --------------------------------------------------------------------------
# 网页文字兜底（接口取不到时的备用方案）
# --------------------------------------------------------------------------

# 页面上这些字样是界面按钮/标签，不是题目内容
DOM_JUNK_LINES = {
    "复制内容", "格式", "全屏", "收起", "展开", "复制", "查看", "提交", "保存",
    "上一题", "下一题", "上一页", "下一页", "清空", "重置", "运行", "调试",
    "题目详情", "答题情况", "提交本题", "查看答案", "显示答案", "隐藏答案",
    "T", "F", "对", "错", "正确", "错误", "是", "否", "真", "假",
}
DOM_JUNK_RE = re.compile(
    r"^(分数|作者|单位|状态|难度|提交时间|得分|满分|题目类型|题型)\s*[:：]?\s*\S*$"
)

DOM_JS = r"""
() => {
  const labelRe = /\d+\s*[-–—]\s*\d+/g;
  const cand = new Set();

  document.querySelectorAll(
    '[class*="problem"],[class*="Problem"],[class*="question"],' +
    '[class*="Question"],[class*="subject"],[class*="Subject"],[data-e2e]'
  ).forEach(el => cand.add(el));

  const all = document.querySelectorAll('div,section,article,li');
  for (let i = 0; i < all.length && cand.size < 5000; i++) {
    const el = all[i];
    const tc = el.textContent || '';
    if (tc.length > 10 && tc.length < 8000 && /^\s*\d+\s*[-–—]\s*\d+/.test(tc)) {
      cand.add(el);
    }
  }

  const out = [];
  for (const el of cand) {
    if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE') continue;
    const txt = (el.innerText || '').trim();
    if (txt.length < 12) continue;
    const m = txt.match(/^(\d+\s*[-–—]\s*\d+)/);
    if (!m) continue;
    const count = (txt.match(labelRe) || []).length;
    if (count > 1) continue;
    out.push({ label: m[1].replace(/\s/g, ''), text: txt, len: txt.length });
  }
  return out.slice(0, 800);
}
"""


def clean_dom_text(text):
    """把网页文字里的界面杂项清掉，只留下题目本身。"""
    lines = [l.rstrip() for l in (text or "").replace("\r", "").split("\n")]
    if lines:
        lines = lines[1:]          # 第一行是题号，丢掉

    kept = []
    digit_run = []
    for ln in lines:
        s = ln.strip()

        if s.isdigit():
            digit_run.append(ln)
            continue
        # 连续两行以上的纯数字 => 代码行号，整段丢掉
        if digit_run:
            if len(digit_run) < 2:
                kept.extend(digit_run)
            digit_run = []

        if not s:
            kept.append("")
            continue
        if s in DOM_JUNK_LINES:
            continue
        if DOM_JUNK_RE.match(s):
            continue
        kept.append(ln)
    if digit_run and len(digit_run) < 2:
        kept.extend(digit_run)

    text = "\n".join(kept)
    text = PLACEHOLDER_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# 网页上选项的写法：A. xxx / A、xxx / A）xxx
OPTION_LINE_RE = re.compile(r"^\s*([A-H])\s*[.、．)）:：]\s*(\S.*)$")

# 题型中文名 -> 题型枚举（网页标签上显示的名字）
TAB_NAME_TYPE = {v: k for k, v in TYPE_TAB_NAME.items()}


def _valid_options(opts):
    """必须是 A、B、C… 这样连续的一串，否则多半是误判。"""
    return len(opts) >= 2 and [o["id"] for o in opts] == \
        [chr(65 + i) for i in range(len(opts))]


def split_options_from_text(text):
    """把 A/B/C/D 选项行从网页文字里摘出来。返回 (正文, 选项列表)。"""
    opts, kept = [], []
    for ln in (text or "").split("\n"):
        m = OPTION_LINE_RE.match(ln)
        if m:
            opts.append({"id": m.group(1), "text": m.group(2).strip(), "correct": False})
            continue
        kept.append(ln)
    if not _valid_options(opts):
        return text, []
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return body, opts


def extract_options_from_text(text):
    """只取选项，不改正文。"""
    return split_options_from_text(text)[1]


def type_from_dom_text(text):
    """网页题目第一行通常长这样：『2-1 单选题』，用它猜题型。"""
    first = (text or "").split("\n", 1)[0]
    for name, t in TAB_NAME_TYPE.items():
        if name in first:
            return t
    return ""


# --------------------------------------------------------------------------
# 从接口数据里推断「有哪些题型、各多少题」
# --------------------------------------------------------------------------

def find_summaries(captured):
    """找出 problem-summaries 接口返回的题型清单：{题型枚举: 题量}。"""
    for item in captured:
        d = item.get("data")
        if not isinstance(d, dict):
            continue
        s = d.get("summaries")
        if isinstance(s, dict) and s:
            out = {}
            for k, v in s.items():
                if isinstance(v, dict) and isinstance(v.get("total"), int):
                    out[str(k).upper()] = v["total"]
                elif isinstance(v, int):
                    out[str(k).upper()] = v
            if out:
                return out
    return {}


def find_problem_set_id(captured):
    """从抓到的接口地址里找出题目集 id。"""
    for item in captured:
        m = re.search(r"/api/problem-sets/(\d+)", item["url"])
        if m:
            return m.group(1)
    return ""


def type_api_urls(captured, types):
    """把已抓到的题目接口地址改造成「各题型」的地址。

    PTA 的题目列表接口靠 problem_type 参数区分题型，
    所以只要把原请求里的这个参数换掉就行。
    """
    bases = []
    for item in captured:
        u = item["url"]
        if "/exam-problems" in u or re.search(r"/problem-sets/\d+/problems", u):
            bases.append(u)
    if not bases:
        return {}

    for u in bases:
        m = re.search(r"([?&])(problem_type|problemType|type)=([^&]*)", u)
        if m:
            pre, suf = u[:m.start(3)], u[m.end(3):]
            return {t: pre + t + suf for t in types}

    u = bases[0]
    sep = "&" if "?" in u else "?"
    return {t: u + sep + "problem_type=" + t for t in types}


# --------------------------------------------------------------------------
# 浏览器里的操作
# --------------------------------------------------------------------------

def make_harvester(captured, failed_urls):
    """返回一个 response 事件回调，把接口返回的 JSON 记下来。"""

    def on_response(resp):
        try:
            url = resp.url
            if "pintia.cn" not in url or "/api/" not in url:
                return
            headers = resp.headers
            if callable(headers):
                headers = headers()
            ctype = (headers or {}).get("content-type", "")
            if "json" not in str(ctype).lower():
                return
            data = resp.json()
        except Exception:
            try:
                if "pintia.cn" in resp.url and "/api/" in resp.url:
                    failed_urls.append(resp.url)
            except Exception:
                pass
            return
        captured.append({"url": url, "time": time.time(), "data": data})

    return on_response


def better_dom_node(old, new):
    """同一道题可能被好几层容器匹配到，挑最贴近单道题的那一层。"""
    if old is None:
        return new
    lo, ln = old.get("len", 0), new.get("len", 0)
    ok_old, ok_new = lo >= 80, ln >= 80
    if ok_old and ok_new:
        return new if ln < lo else old
    if ok_old:
        return old
    if ok_new:
        return new
    return new if ln > lo else old


def auto_scroll(page, max_rounds=120):
    """慢慢往下滚，触发页面懒加载。"""
    last_h = -1
    stable = 0
    for _ in range(max_rounds):
        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(220)
        try:
            h = page.evaluate("() => document.body.scrollHeight")
        except Exception:
            break
        if h == last_h:
            stable += 1
            if stable >= 3:
                break
        else:
            stable = 0
            last_h = h


NEXT_SELECTORS = [
    'button:has-text("下一页")',
    'a:has-text("下一页")',
    'li:has-text("下一页")',
    '[aria-label="下一页"]',
    '[title="下一页"]',
    'button:has-text("下页")',
    'a:has-text("下页")',
    'button:has-text("Next")',
]


def click_next(page):
    """尝试点「下一页」，成功返回 True。"""
    for sel in NEXT_SELECTORS:
        try:
            loc = page.locator(sel)
            cnt = min(loc.count(), 5)
        except Exception:
            continue
        for i in range(cnt):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                cls = (el.get_attribute("class") or "").lower()
                if "disabled" in cls or "forbid" in cls:
                    continue
                try:
                    if el.is_disabled():
                        continue
                except Exception:
                    pass
            except Exception:
                continue
            try:
                el.scroll_into_view_if_needed(timeout=2000)
                el.click(timeout=3000)
                return True
            except Exception:
                continue
    return False


# ---- 题型标签（判断题 / 单选题 / 多选题 / 填空题 / 程序填空题）----------------

FIND_TABS_JS = r"""
() => {
  const NAMES = ['判断题','单选题','多选题','填空题','程序填空题','代码填空题',
                 '简答题','改错题','函数题','其他题型'];
  const re = new RegExp('^\\s*(' + NAMES.join('|') + ')\\s*\\d*\\s*(\\/\\s*\\d+)?\\s*$');
  const els = document.querySelectorAll('li,div,span,a,button,p,section');
  const out = [];
  const seen = new Set();
  for (const el of els) {
    const raw = (el.innerText || '').replace(/\s+/g, ' ').trim();
    const m = raw.match(re);
    if (!m) continue;
    const name = m[1];
    if (seen.has(name)) continue;
    // 父级如果也是同一个题型名，说明还有更外层，跳过自己
    let p = el.parentElement, outer = false;
    while (p && p !== document.body) {
      const pt = (p.innerText || '').replace(/\s+/g, ' ').trim();
      const pm = pt.match(re);
      if (pm && pm[1] === name) { outer = true; break; }
      p = p.parentElement;
    }
    if (outer) continue;
    seen.add(name);
    el.setAttribute('data-dsh-tab', String(out.length));
    out.push({ index: out.length, name: name, text: raw });
  }
  return out;
}
"""


def find_type_tabs(page):
    """找出页面上「判断题 9 / 29」这类题型标签。"""
    try:
        return page.evaluate(FIND_TABS_JS) or []
    except Exception:
        return []


def click_type_tab(page, index):
    """点开某个题型标签。"""
    try:
        el = page.locator('[data-dsh-tab="%d"]' % index).first
        if el.count() == 0:
            return False
        el.scroll_into_view_if_needed(timeout=2000)
        el.click(timeout=4000)
        return True
    except Exception:
        return False


def snapshot_page(page, dom_items, tab_name=""):
    """滚动加载 + 记录当前页的题目文字，返回本页识别到的题数。"""
    auto_scroll(page)
    try:
        items = page.evaluate(DOM_JS) or []
    except Exception:
        items = []
    for it in items:
        lab = it.get("label") or ""
        if not lab:
            continue
        key = (tab_name + "|" + lab) if tab_name else lab
        it["tab"] = tab_name
        dom_items[key] = better_dom_node(dom_items.get(key), it)
    return len(items)


# --------------------------------------------------------------------------
# 输出：把题目渲染成好看的 Markdown
# --------------------------------------------------------------------------

def type_cn(t):
    return TYPE_CN.get(t, "其他题型" if t else "理论题")


def _bool_cn(v):
    s = str(v).strip()
    if s.lower() in ("true", "t", "1"):
        return "正确"
    if s.lower() in ("false", "f", "0"):
        return "错误"
    return ""


def render_answer_value(ans, choices):
    """把答案转成人看得懂的文字。"""
    if ans is None or ans == [] or ans == "" or ans == {}:
        return ""

    def one(v):
        if isinstance(v, bool):
            return "正确" if v else "错误"
        s = html_unescape(str(v)).strip()
        # 答案就是某个选项的 id
        for i, c in enumerate(choices):
            if c["id"] == s:
                return "%s. %s" % (chr(65 + i), c["text"])
        # 答案写成 A / AB / ABC
        if choices and re.fullmatch(r"[A-H]{1,8}", s):
            parts = []
            for ch in s:
                i = ord(ch) - 65
                if i < len(choices):
                    parts.append("%s. %s" % (ch, choices[i]["text"]))
            return "；".join(parts) if parts else s
        # 判断题的 TRUE / FALSE
        if not choices:
            b = _bool_cn(s)
            if b:
                return b
        # 答案写的是选项下标
        if s.isdigit() and choices and int(s) < len(choices):
            i = int(s)
            return "%s. %s" % (chr(65 + i), choices[i]["text"])
        return s

    if isinstance(ans, (list, tuple)):
        vals = [one(v) for v in ans]
        return "；".join(v for v in vals if v)
    return one(ans)


def render_question(q, index):
    """渲染单道题。"""
    parts = []

    # ---- 小标题：第 N 题 · 题型（分值） ----
    head = "### 第 %d 题 · %s" % (index, type_cn(q.get("type")))
    if q.get("score"):
        head += "（%s 分）" % q["score"]
    parts.append(head)
    parts.append("")

    # ---- 题干 ----
    if q.get("title"):
        parts.append("**%s**" % q["title"])
        parts.append("")
    if q.get("content"):
        parts.append(q["content"])
        parts.append("")

    # ---- 选项 ----
    choices = q.get("choices") or []
    if choices:
        for i, c in enumerate(choices):
            text = c["text"].strip()
            letter = chr(65 + i)
            if "\n" in text:
                # 选项里带代码块，单独起一段才看得清
                parts.append("**%s.**" % letter)
                parts.append("")
                parts.append(text)
                parts.append("")
            else:
                parts.append("- **%s.** %s" % (letter, re.sub(r"\n+", " ", text)))
        while parts and parts[-1] == "":
            parts.pop()
        parts.append("")

    # ---- 答案（按需求默认不出，只留题目和选项） ----
    if INCLUDE_ANSWER:
        ans = render_answer_value(q.get("answer"), choices)
        mine = render_answer_value(q.get("user_answer"), choices)
        if ans:
            parts.append("**答案：** %s" % ans.replace("\n", " "))
        elif mine:
            parts.append("**你的作答：** %s" % mine.replace("\n", " "))

        if q.get("solution"):
            parts.append("")
            parts.append("**解析：** %s" % q["solution"].replace("\n", " "))

    while parts and parts[-1] == "":
        parts.pop()
    return "\n".join(parts)


def sort_key(item):
    """先按题型（判断题→单选题→…），同题型内按题目集里的原始顺序。"""
    idx, q = item
    order = q.get("order")
    if not isinstance(order, int):
        order = 9999
        lab = q.get("label") or ""
        m = re.match(r"(\d+)\D+(\d+)", lab)
        if m:
            order = int(m.group(2))
    return (type_rank(q.get("type") or ""), order, idx)


def write_markdown(questions, meta):
    questions = [q for _, q in sorted(enumerate(questions), key=sort_key)]

    n_official = sum(1 for q in questions
                     if render_answer_value(q.get("answer"), q.get("choices") or []))
    n_mine = sum(1 for q in questions if q.get("user_answer"))

    lines = []
    lines.append("# %s" % (meta.get("name") or "PTA 理论题整理"))
    lines.append("")
    lines.append("> **来源**：%s  " % meta.get("url", ""))
    lines.append("> **抓取时间**：%s  " % meta.get("time", ""))
    stat_txt = "、".join("%s %d 题" % (k, v) for k, v in
                        sorted(meta.get("types", {}).items(), key=lambda x: -x[1]))
    lines.append("> **题量**：共 %d 题%s"
                 % (len(questions), ("（%s）" % stat_txt) if stat_txt else ""))
    if INCLUDE_ANSWER and not n_official:
        lines.append(">")
        if n_mine:
            lines.append("> **提示**：本题目集没有公开标准答案；已经作答过的题目，下面显示的是你自己的作答记录。")
        else:
            lines.append("> **提示**：本题目集还没有公开答案。")
        lines.append("> 在 PTA 上完成并提交后，再运行一次本工具，就能抓到标准答案。")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 按题型分组，每组给个二级标题
    group = {}
    for q in questions:
        group.setdefault(q.get("type") or "", []).append(q)

    n = 0
    for t in sorted(group, key=type_rank):
        items = group[t]
        lines.append("## %s（%d 题）" % (type_cn(t), len(items)))
        lines.append("")
        for q in items:
            n += 1
            lines.append(render_question(q, n))
            lines.append("")

    OUT_MD.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# 把抓到的数据整理成题目列表
# --------------------------------------------------------------------------

def count_ids_by_type(captured):
    """统计接口里每种题型各有几个不重复的题目 id。"""
    got = {}
    for item in captured:
        for d in walk_json(item.get("data")):
            if not looks_like_problem(d):
                continue
            t = str(d.get("type") or d.get("problemType") or "").strip().upper()
            pid = d.get("problemSetProblemId") or d.get("id")
            if not t or not pid:
                continue
            got.setdefault(t, set()).add(str(pid))
    return {t: len(s) for t, s in got.items()}


def build_questions(captured, dom_items, url="", allow_dom_only=True):
    """返回 (题目列表, 文档信息, 统计信息)。"""
    official_ans, my_ans = collect_submitted_answers(captured)

    problems = {}
    unknown_types = {}
    set_name = ""

    for item in captured:
        data = item["data"]
        if isinstance(data, dict):
            ps = data.get("problemSet")
            if isinstance(ps, dict) and ps.get("name") and not set_name:
                set_name = str(ps["name"])

        for d in walk_json(data):
            if not looks_like_problem(d):
                continue
            q = normalize_problem(d)
            if not q:
                continue
            if q["type"] in PROGRAMMING_TYPES:
                continue
            if q["type"] and q["type"] not in TYPE_CN:
                unknown_types[q["type"]] = unknown_types.get(q["type"], 0) + 1
            old = problems.get(q["key"])
            if old is None or richness(q) > richness(old):
                problems[q["key"]] = q

    # 标准答案 / 你的作答 回填
    for k, q in problems.items():
        if k in official_ans and not q.get("answer"):
            q["answer"] = official_ans[k]
        if k in my_ans:
            q["user_answer"] = my_ans[k]

    # 网页文字兜底：只保留接口里确实没有的题（避免同一道题出现两遍）
    index, loose_keys = {}, []
    for q in problems.values():
        nk = _squash(q.get("content") or "")[:40]
        if nk:
            index[nk] = q
        lk = _squash_loose(q.get("content") or "")
        if len(lk) >= 12:
            loose_keys.append((lk, q))

    def already_have(text):
        nk = _squash(text)[:40]
        if len(nk) < 12:
            return None
        hit = index.get(nk)
        if hit is not None:
            return hit
        for k, q in index.items():
            if k.startswith(nk[:20]) or nk.startswith(k[:20]):
                return q
        lk = _squash_loose(text)
        for k, q in loose_keys:
            m = min(len(lk), len(k), 28)
            if m >= 12 and lk[:m] == k[:m]:
                return q
        return None

    dom_only = []
    filled_options = 0
    for lab, it in dom_items.items():
        raw_text = it.get("text", "")
        text = clean_dom_text(raw_text)
        if not text:
            continue
        hit = already_have(text)
        if hit is not None:
            # 接口没给选项文本时，用网页上渲染出来的 A/B/C/D 补上
            if not hit.get("choices"):
                opts = extract_options_from_text(text)
                if opts:
                    hit["choices"] = opts
                    filled_options += 1
            continue

        # 接口已经抓全了就不再用网页文字凑数，否则会重复
        if not allow_dom_only:
            continue

        body, opts = split_options_from_text(text)
        dom_type = TAB_NAME_TYPE.get(it.get("tab") or "") or type_from_dom_text(raw_text)
        dom_only.append({
            "key": "dom::" + lab,
            "label": "",
            "order": None,
            "type": dom_type,
            "title": "",
            "content": body,
            "choices": opts,
            "answer": None,
            "user_answer": None,
            "solution": "",
            "score": None,
            "source": "页面文字",
        })

    questions = list(problems.values()) + dom_only

    type_stat = {}
    for q in questions:
        name = type_cn(q["type"])
        type_stat[name] = type_stat.get(name, 0) + 1

    meta = {
        "name": set_name,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "url": url or "（手动打开的页面）",
        "types": type_stat,
    }
    info = {
        "from_dom": len(dom_only),
        "filled_options": filled_options,
        "with_choices": sum(1 for q in questions if q.get("choices")),
        "with_answer": sum(1 for q in questions
                           if render_answer_value(q.get("answer"), q.get("choices") or [])),
        "with_mine": sum(1 for q in questions if q.get("user_answer")),
        "unknown_types": unknown_types,
    }
    return questions, meta, info


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def banner():
    print("=" * 62)
    print("        PTA 理论题抓取工具")
    print("=" * 62)


def pause_exit(code=0):
    try:
        input("\n按回车键结束程序...")
    except BaseException:
        pass
    sys.exit(code)


PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def _can_import_playwright():
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _has_browser():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = p.chromium.executable_path
        return bool(exe) and os.path.exists(exe)
    except Exception:
        return False


def ensure_environment():
    """第一次运行时自动装好 playwright 和浏览器内核。"""
    changed = False

    if not _can_import_playwright():
        print("\n第一次运行，需要先安装一个小工具（playwright）。")
        print("正在联网安装，大约需要 1~3 分钟，请耐心等待……\n")
        code = subprocess.call(
            [sys.executable, "-m", "pip", "install", "-i", PIP_MIRROR, "playwright"])
        if code != 0:
            print("\n国内镜像没装上，改用官方源再试一次……\n")
            subprocess.call([sys.executable, "-m", "pip", "install", "playwright"])
        if not _can_import_playwright():
            print("\n[!] playwright 安装失败，通常是网络问题。")
            print("    请关掉代理/加速器后重试，或手动运行：")
            print("    python -m pip install playwright")
            return False
        changed = True

    if not _has_browser():
        print("\n正在下载浏览器内核（只需下载一次，大约 1~3 分钟）……\n")
        subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])
        if not _has_browser():
            print("\n[!] 浏览器内核下载失败，通常是网络问题。")
            print("    请关掉代理/加速器后重试，或手动运行：")
            print("    python -m playwright install chromium")
            return False
        changed = True

    if changed:
        print("\n安装完成，以后启动就不用再等了。\n")
    return True


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass

    banner()

    if not ensure_environment():
        pause_exit(1)
        return

    from playwright.sync_api import sync_playwright

    url_arg = sys.argv[1].strip() if len(sys.argv) > 1 else ""

    RAW_DIR.mkdir(exist_ok=True)

    captured = []
    failed_urls = []
    dom_items = {}

    print("""
接下来会弹出一个浏览器窗口，请按下面三步操作：

  第 1 步：如果没登录，先在浏览器里登录 PTA（拼题A）
  第 2 步：打开你要抓取的那个题目集 / 作业的页面
          （就是能看到题目列表的那一页）
  第 3 步：回到这个黑色窗口，按一下回车键

脚本随后会自动一页一页往下翻、把题目全部抓下来。
中途想停，随时按 Ctrl + C 也可以（已抓到的会照常保存）。
""")

    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                viewport={"width": 1440, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            print("\n[!] 启动浏览器失败：%s" % exc)
            print("    请关掉浏览器里所有本程序打开的窗口，删掉同目录下的")
            print("    《浏览器登录信息》文件夹后重试。")
            pause_exit(1)
            return

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", make_harvester(captured, failed_urls))

        try:
            if url_arg:
                print("正在打开：%s" % url_arg)
                page.goto(url_arg, wait_until="domcontentloaded", timeout=60000)
            else:
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print("[提示] 打开首页超时（不影响，你可以手动输入网址）：%s" % exc)

        try:
            input("\n>>> 浏览器里准备就绪后，在这里按回车开始抓取 <<<\n")
        except KeyboardInterrupt:
            print("\n已取消。")
            ctx.close()
            return

        try:
            page.bring_to_front()
        except Exception:
            pass

        print("\n开始抓取，请不要关闭浏览器窗口……\n")

        interrupted = False
        expected = {}          # 题型 -> 该题型的题目总数（来自接口清单）
        got = {}               # 题型 -> 本次实际抓到几道
        missing_types = []     # 接口没给全、需要点网页标签补的题型

        try:
            # ============ 第 1 步：抓当前显示的这一页 ============
            n = snapshot_page(page, dom_items, "当前页")
            print("  [当前页] 页面识别到 %d 道题，接口数据 %d 份" % (n, len(captured)))

            # ============ 第 2 步：问接口要「各题型分别有多少题」 ============
            psid = find_problem_set_id(captured)
            summaries = find_summaries(captured)
            if not summaries and psid:
                u = "https://pintia.cn/api/problem-sets/%s/problem-summaries" % psid
                try:
                    r = ctx.request.get(u, timeout=20000)
                    if r.ok:
                        captured.append({"url": u, "time": time.time(), "data": r.json()})
                        summaries = find_summaries(captured)
                except Exception:
                    pass

            if summaries:
                expected = {t: c for t, c in summaries.items()
                            if t not in PROGRAMMING_TYPES and t not in NON_PROBLEM_TYPES}
                if expected:
                    print("\n  这个题目集包含以下理论题：")
                    for t, c in sorted(expected.items(), key=lambda x: -x[1]):
                        print("      - %s：%d 道" % (type_cn(t), c))
            else:
                print("  没拿到题型清单，将按网页上的题型标签逐个抓取。")

            # ============ 第 3 步：按题型逐个向接口要题目 ============
            target_types = list(expected.keys())
            if not target_types:
                target_types = [t for t in TYPE_CN if t not in PROGRAMMING_TYPES]

            urls = type_api_urls(captured, target_types)
            if urls:
                print("\n  正在按题型逐个抓取……")
                for t in target_types:
                    u = urls.get(t)
                    if not u:
                        continue
                    before = len(captured)
                    try:
                        r = ctx.request.get(u, timeout=30000)
                        if r.ok:
                            captured.append({"url": u, "time": time.time(), "data": r.json()})
                    except Exception as exc:
                        print("      %s 抓取失败：%s" % (type_cn(t), exc))

                    # 数一数这一轮新增了这道题型的几道题
                    cnt = set()
                    for item in captured[before:]:
                        for d in walk_json(item["data"]):
                            if not looks_like_problem(d):
                                continue
                            dt = str(d.get("type") or d.get("problemType") or "").upper()
                            if dt != t:
                                continue
                            pid = d.get("problemSetProblemId") or d.get("id")
                            if pid:
                                cnt.add(str(pid))
                    got[t] = len(cnt)
                    flag = ""
                    if t in expected and len(cnt) < expected[t]:
                        flag = "  ← 比预期少，稍后用网页标签补"
                        missing_types.append(t)
                    print("      %-8s 抓到 %d 道%s" % (type_cn(t), len(cnt), flag))
            else:
                missing_types = ["__all__"]

            # ============ 第 4 步：还缺的题型，点网页题型标签补 ============
            still_missing = [t for t, want in expected.items() if got.get(t, 0) < want]
            if not expected:
                still_missing = ["__all__"]

            if still_missing:
                tabs = find_type_tabs(page)
                if tabs:
                    print("\n  网页上发现题型标签：%s"
                          % "、".join(t["name"] for t in tabs))
                    if still_missing == ["__all__"]:
                        print("  逐个点开，把每个题型的题目都抓下来……")
                    else:
                        print("  这几个还没抓全（%s），逐个点开补齐……"
                              % "、".join(type_cn(t) for t in still_missing))

                    tab_by_name = {t["name"]: t for t in tabs}
                    if still_missing == ["__all__"]:
                        targets = list(tabs)
                    else:
                        targets = []
                        for t in still_missing:
                            tb = tab_by_name.get(TYPE_TAB_NAME.get(t, ""))
                            if tb:
                                targets.append(tb)

                    for tab in targets:
                        print("  → 点开「%s」……" % tab["name"])
                        if not click_type_tab(page, tab["index"]):
                            print("     点不开，跳过。")
                            continue
                        page.wait_for_timeout(3000)
                        try:
                            page.evaluate("() => window.scrollTo(0, 0)")
                        except Exception:
                            pass
                        n = snapshot_page(page, dom_items, tab["name"])
                        print("     页面识别 %d 道题，接口数据累计 %d 份"
                              % (n, len(captured)))
                        # 这个题型可能还分了页
                        page_no = 0
                        while page_no < 60:
                            page_no += 1
                            before_c = len(captured)
                            if not click_next(page):
                                break
                            page.wait_for_timeout(1800)
                            try:
                                page.evaluate("() => window.scrollTo(0, 0)")
                            except Exception:
                                pass
                            snapshot_page(page, dom_items, tab["name"])
                            if len(captured) == before_c:
                                break
                    print("  题型标签翻页完成。")
                else:
                    print("\n  [提示] 网页上没找到题型标签，也没法自动按题型抓取。")
                    print("         不过没关系：现在你可以在浏览器里手动点那些题型标签页，")
                    print("         脚本会一直把看到的题目记下来。")
                    print("         全部看完后按 Ctrl + C 结束（已抓到的照常保存）。")
                    print("\n  等待中……（每 1.5 秒记录一次当前页面，最多等 10 分钟）")
                    waited = 0.0
                    while waited < 600:
                        page.wait_for_timeout(1500)
                        waited += 1.5
                        snapshot_page(page, dom_items, "")
                    print("  等待结束，继续整理已抓到的题目。")

        except KeyboardInterrupt:
            interrupted = True
            print("\n\n已手动中断，正在保存已抓到的内容……")
        except Exception as exc:
            print("\n[提示] 抓取过程中出现异常：%s" % exc)
            traceback.print_exc()

        # ---------------- 补抓读不到内容的接口 ----------------
        for u in list(dict.fromkeys(failed_urls))[:200]:
            try:
                r = ctx.request.get(u, timeout=20000)
                if r.ok:
                    captured.append({"url": u, "time": time.time(), "data": r.json()})
            except Exception:
                continue

        # ---------------- 顺手问一下「标准答案」接口（只在需要答案时） ----------------
        if INCLUDE_ANSWER:
            psid = find_problem_set_id(captured)
            if psid:
                types_found = set(expected) or set()
                if not types_found:
                    for it in captured:
                        for d in walk_json(it["data"]):
                            t = d.get("type") or d.get("problemType")
                            if isinstance(t, str) and re.fullmatch(
                                    r"[A-Z][A-Z0-9_]{2,60}", t.strip()):
                                types_found.add(t.strip().upper())
                for t in sorted(types_found - NON_PROBLEM_TYPES - PROGRAMMING_TYPES):
                    u = ("https://pintia.cn/api/problem-sets/%s/standard-answers"
                         "?problem_type=%s" % (psid, t))
                    try:
                        r = ctx.request.get(u, timeout=20000)
                        if r.ok:
                            captured.append(
                                {"url": u, "time": time.time(), "data": r.json()})
                    except Exception:
                        continue

        # ---------------- 保存原始数据，方便排查 ----------------
        got = count_ids_by_type(captured)
        total_got = sum(v for k, v in got.items() if k not in PROGRAMMING_TYPES)
        total_expected = sum(expected.values()) if expected else 0
        # 接口已经抓够了就不要再拿网页文字凑数，否则同一道题会出现两遍
        allow_dom_only = (total_expected == 0) or (total_got < total_expected)
        print("\n  接口实际抓到 %d 道%s"
              % (total_got, ("（题目集共 %d 道）" % total_expected) if total_expected else ""))
        if not allow_dom_only:
            print("  接口数据已完整，不再使用网页文字兜底。")

        try:
            (RAW_DIR / "接口地址.txt").write_text(
                "\n".join(it["url"] for it in captured), encoding="utf-8")
            (RAW_DIR / "网页文字.json").write_text(
                json.dumps(dom_items, ensure_ascii=False, indent=2), encoding="utf-8")
            for i, item in enumerate(captured[:400], 1):
                safe = re.sub(r"[^A-Za-z0-9]+", "_", item["url"])[:90]
                (RAW_DIR / ("%03d_%s.json" % (i, safe))).write_text(
                    json.dumps(item["data"], ensure_ascii=False, indent=2),
                    encoding="utf-8")
        except Exception:
            pass

        final_url = ""
        try:
            final_url = page.url
        except Exception:
            pass

        try:
            ctx.close()
        except Exception:
            pass

    # ---------------- 解析 ----------------
    print("\n正在整理题目……")

    questions, meta, info = build_questions(
        captured, dom_items, final_url or url_arg, allow_dom_only=allow_dom_only)

    if not questions:
        print("""
[结果] 很抱歉，这次没有抓到任何题目。可能的原因：

  1) 你打开的页面不是题目列表页（比如打开的是课程首页）
  2) 题目还没在浏览器里显示出来就按了回车
  3) 这个题目集需要先「开始答题」才会加载内容

建议重新运行一次，进到能看到题目的页面后，
等题目在浏览器里显示出来了，再按回车。
""")
        OUT_MD.write_text(
            "# PTA 理论题整理\n\n本次没有抓到题目。请查看同目录《使用说明.md》里的排查建议。\n",
            encoding="utf-8")
        pause_exit(0)
        return

    write_markdown(questions, meta)

    set_name = meta.get("name") or ""
    type_stat = meta.get("types") or {}
    added_from_dom = info["from_dom"]
    filled_options = info.get("filled_options", 0)
    with_choices = info.get("with_choices", 0)
    with_answer = info["with_answer"]
    with_mine = info["with_mine"]
    unknown_types = info["unknown_types"]

    print("\n" + "=" * 62)
    print("抓取完成！")
    print("=" * 62)
    if set_name:
        print("  题目集：%s" % set_name)
    print("  题目总数：%d 道" % len(questions))
    for k, v in sorted(type_stat.items(), key=lambda x: -x[1]):
        want = ""
        for t, c in (expected or {}).items():
            if type_cn(t) == k:
                want = "（题目集里共 %d 道）" % c
                break
        print("      - %s：%d 道%s" % (k, v, want))
    if expected:
        short = [(type_cn(t), c, got.get(t, 0))
                 for t, c in expected.items() if got.get(t, 0) < c]
        if short:
            print("")
            print("  [注意] 下面这几个题型没抓全，可以重新运行再试一次：")
            for name, c, g in short:
                print("      %s：题目集里 %d 道，本次只抓到 %d 道" % (name, c, g))
    if with_choices:
        print("  带选项的选择题：%d 道" % with_choices)
    if filled_options:
        print("  其中 %d 道的选项是从网页上补全的（接口没给）" % filled_options)
    if added_from_dom:
        print("  另有 %d 道来自网页文字（接口没给结构化数据）" % added_from_dom)
    if INCLUDE_ANSWER and with_answer:
        print("  有标准答案的：%d 道" % with_answer)
    if INCLUDE_ANSWER and with_mine:
        print("  有你自己作答记录的：%d 道" % with_mine)
    if unknown_types:
        print("  其它题型：%s" % "、".join("%s×%d" % (k, v) for k, v in unknown_types.items()))
    print("")
    print("  已生成文件：%s" % OUT_MD)
    print("  原始数据在：%s（可留作备份，也可整个删掉）" % RAW_DIR)
    if interrupted:
        print("\n  注意：这次是被手动中断的，可能只抓到一部分。")
    print("")
    pause_exit(0)


if __name__ == "__main__":
    os.chdir(HERE)
    main()
