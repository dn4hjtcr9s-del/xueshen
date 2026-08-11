#!/usr/bin/env python3
"""
clean_ocr.py — MinerU OCR 结果清洗流水线

以 content_list.jsonl 的 raw 字段为唯一事实来源，重建干净的结构化文本。

用法:
    python3 scripts/clean_ocr.py --book 16          # 清洗单本书(按 book_id 前缀匹配)
    python3 scripts/clean_ocr.py --all              # 清洗全部
    python3 scripts/clean_ocr.py --book 16 --inroot ocr_text --outroot clean_text

每本书产出(outroot/<book_id>/):
    clean.md              正文(含附录), 结构化 Markdown
    front_matter.md       封面/版权/前言/目录
    back_matter.md        习题答案/参考文献/索引
    toc.json              重建后的目录树(扁平列表)
    clean_content_list.jsonl  清洗后的块级记录
    cleaning_report.json  每条规则的删/改统计
"""
import argparse
import hashlib
import json
import re
import sys
from itertools import pairwise
from pathlib import Path

# ---------------------------------------------------------------- 规则常量

DROP_TYPES = {"page_header", "page_footer", "page_number", "page_aside_text"}

WATERMARK_PATTERNS = [
    re.compile(r"仅供个人学习使用[，,]?未经授权不得另做他用"),
    re.compile(r"中国银行股份有限公司"),
    re.compile(r"中国教育科技研究院"),
    re.compile(r"z[-_]?library\.?\w*", re.I),
    re.compile(r"[1z]-?lib\.sk", re.I),
]

TERMINAL_PUNCT = set("。！？；：.!?;:”’\")）》】』」$")

NEW_PARA_START = re.compile(
    r"^(例\s*\d|定义\s*\d*|定理\s*\d*|引理\s*\d*|推论\s*\d*|证明|解\s*[:：]|注\s*[:：]?"
    r"|\(?\d+[)）、.]|第[一二三四五六七八九十百零0-9]+[章节]|习题|复习题|练习)"
)

CHAPTER_PAT = re.compile(
    r"^第\s*(?P<number>[一二三四五六七八九十百零0-9]+)\s*章"
)
SECTION_PAT = re.compile(r"^第\s*[一二三四五六七八九十百零0-9]+\s*节")
SUB1_PAT = re.compile(r"^[一二三四五六七八九十]+、")
SUB2_PAT = re.compile(r"^\d+\s*[.、]\s*\S")

BACK_MATTER_PAT = re.compile(
    r"^(部分习题答案|习题答案|参考答案|习题参考答案|参考文献|索引|名词索引|"
    r"中英文名词|按拼音字母序|郑重声明|防伪查询|读者意见反馈)"
)

CJK = r"一-鿿㐀-䶿"

LEVEL_MAP = {  # book_id 数字前缀 -> 学段
    **{i: "大学" for i in (1, 2, 5, 11, 12, 13, 14, 15, 16, 17)},
    **{i: "高中" for i in (6, 7, 8, 9, 10)},
    **{i: "初中" for i in (3, 4, 18, 19, 20, 21)},
}

# ---------------------------------------------------------------- 工具函数


def chapter_key(text: str) -> str | None:
    """把中文或阿拉伯数字章号规范为同一键，便于识别书末重复章标题。"""
    match = CHAPTER_PAT.match(text.strip())
    if match is None:
        return None
    number = match.group("number")
    if number.isdigit():
        return str(int(number))

    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    total = 0
    current = 0
    for char in number:
        if char in digits:
            current = digits[char]
        elif char == "十":
            total += (current or 1) * 10
            current = 0
        elif char == "百":
            total += (current or 1) * 100
            current = 0
    return str(total + current)


def brace_delta(s: str) -> int:
    """结构大括号净值(剔除 \\{ \\}); >0 表示未闭合的左括号多。
    分段函数 \\left\\{ \\begin{array} ... \\right. 不会被误判。"""
    t = s.replace(r"\{", "").replace(r"\}", "")
    return t.count("{") - t.count("}")


def escaped_brace_delta(s: str) -> int:
    """转义大括号 \\{ 与 \\} 的净值; 用于识别集合记号的跨行断裂。"""
    return s.count(r"\{") - s.count(r"\}")


def is_split_open(f: str) -> bool:
    """疑似断裂公式的左半: 有未闭合 \\{ 且不是分段函数/array 环境。"""
    if r"\begin{array}" in f or r"\left" in f or r"\cases" in f:
        return False
    return escaped_brace_delta(f) > 0 or brace_delta(f) > 0


def is_split_close(f: str) -> bool:
    """疑似断裂公式的右半。"""
    if r"\end{array}" in f or r"\right" in f or r"\cases" in f:
        return False
    return escaped_brace_delta(f) < 0 or brace_delta(f) < 0


def normalize_latex(s: str) -> str:
    """轻度规范化 LaTeX: 折叠多余空格、修复 _ ^ 与括号间的空格。不改动语义。"""
    s = re.sub(r"\s+", " ", s).strip()
    # R _ {f} -> R_{f} ; x ^ {2} -> x^{2}
    s = re.sub(r"([A-Za-z0-9}\)])\s+_\s*\{", r"\1_{", s)
    s = re.sub(r"([A-Za-z0-9}\)])\s+\^\s*\{", r"\1^{", s)
    s = re.sub(r"_\s+([A-Za-z0-9])", r"_\1", s)
    s = re.sub(r"\^\s+([A-Za-z0-9])", r"^\1", s)
    # f (x) -> f(x)
    s = re.sub(r"\b([a-zA-Z])\s+\(", r"\1(", s)
    # 修复 OCR 残缺的公式编号 tag: \tag{\( (4-2') } -> \tag{4-2'}; 光秃的 \tag{\( 删除
    s = re.sub(r"\\tag\{\s*\\\(\s*\(?\s*([0-9]+(?:[-–][0-9]+)?')\s*\)?\s*\\\)?\s*\}*\s*$",
               r"\\tag{\1}", s)
    s = re.sub(r"\s*\\tag\{\s*\\\(\s*$", "", s)
    return s


def split_math(text: str):
    """把文本切成 [(is_math, segment), ...]，$...$ 为数学段。"""
    parts, pos = [], 0
    for m in re.finditer(r"\$[^$]*\$", text):
        if m.start() > pos:
            parts.append((False, text[pos:m.start()]))
        parts.append((True, m.group(0)))
        pos = m.end()
    if pos < len(text):
        parts.append((False, text[pos:]))
    return parts


def normalize_prose(seg: str) -> str:
    """中文语境标点统一(仅非数学段): CJK 旁的半角标点转全角。"""
    seg = re.sub(rf"(?<=[{CJK}])\s*,\s*", "，", seg)
    seg = re.sub(rf"(?<=[{CJK}])\s*;\s*", "；", seg)
    seg = re.sub(rf"(?<=[{CJK}])\s*:\s*", "：", seg)
    seg = re.sub(rf"(?<=[{CJK}])\s*\?\s*", "？", seg)
    seg = re.sub(rf"(?<=[{CJK}])\s*!\s*", "！", seg)
    # CJK 后的句号: 后面跟 CJK 或在段尾 (不碰小数/编号, 那些前面是数字)
    seg = re.sub(rf"(?<=[{CJK}])\.(?=\s*[{CJK}])", "。", seg)
    seg = re.sub(rf"(?<=[{CJK}])\.(?=\s*$)", "。", seg)
    # 数学段/英文后的逗号等, 若后接中文则转全角
    seg = re.sub(rf",(?=\s*[{CJK}])", "，", seg)
    seg = re.sub(rf";(?=\s*[{CJK}])", "；", seg)
    seg = re.sub(rf":(?=\s*[{CJK}])", "：", seg)
    seg = re.sub(r"[ \t]{2,}", " ", seg)
    return seg


def normalize_text(text: str) -> str:
    s = "".join(seg if is_math else normalize_prose(seg)
                for is_math, seg in split_math(text)).strip()
    # 相邻行内公式之间空白被压缩后粘连成 $$ -> 补回空格
    s = re.sub(r"\$\$+", "$ $", s)
    return s


def apply_watermark(text: str) -> tuple:
    """返回 (清理后文本, 命中次数)。只做子串擦除，避免误伤粘连的正文。"""
    hits = 0
    for pat in WATERMARK_PATTERNS:
        text, n = pat.subn("", text)
        hits += n
    return re.sub(r"[ \t]{2,}", " ", text).strip(), hits


def clean_book_name(name: str) -> str:
    name = re.sub(r"\s*\((?:[^()]*(?:z-?library|1lib|z-lib)[^()]*)\)\s*", " ", name, flags=re.I)
    name = re.sub(r"\s*\([^()]*\)\s*$", "", name)  # 残余尾部括号
    return re.sub(r"\s{2,}", " ", name).strip()

# ---------------------------------------------------------------- 块重建


def items_to_text(items) -> str:
    """paragraph_content 等列表 -> 文本, 行内公式包 $...$; 同时合并跨段断裂的公式。"""
    out, i = [], 0
    merged_formulas = 0
    while i < len(items):
        it = items[i]
        if not isinstance(it, dict):
            i += 1
            continue
        typ, content = it.get("type"), it.get("content", "")
        if typ == "equation_inline":
            formula = normalize_latex(content)
            # 跨段公式断裂: 当前段左括号未闭合, 向后找闭合段(允许中间夹 <=4 字符的文本)
            if is_split_open(formula):
                j = i + 1
                buf = []
                while j < len(items):
                    nit = items[j]
                    if not isinstance(nit, dict):
                        break
                    ntyp, nc = nit.get("type"), nit.get("content", "")
                    if ntyp == "text" and len(nc.strip()) <= 4 and is_split_open(formula):
                        buf.append(nc)
                        j += 1
                        continue
                    if ntyp == "equation_inline" and is_split_close(nc):
                        formula = formula + "".join(buf) + normalize_latex(nc)
                        merged_formulas += 1
                        i = j
                    break
            out.append(f" ${formula}$ ")
        elif typ == "text":
            out.append(content)
        else:
            out.append(str(content))
        i += 1
    text = "".join(out)
    text = re.sub(r"\s*\$\s*", lambda m: " $ " if m.group(0).strip() == "$" else "$", text)
    return text, merged_formulas


def content_items(raw: dict, key: str):
    return raw.get("content", {}).get(key) or []


def build_source_ref(rec: dict) -> dict:
    """从原始 OCR 记录构造可稳定校验的精确 block 引用。"""
    raw = rec.get("raw", {})
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "source_page": rec.get("source_page"),
        "mineru_page_index": rec.get("mineru_page_index"),
        "block_index": rec.get("block_index"),
        "source_chunk_id": rec.get("chunk_id", ""),
        "source_pdf": rec.get("source_pdf", ""),
        "element_type": rec.get("element_type", ""),
        "bbox": raw.get("bbox", []),
        "raw_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }


def extract_chapter_header_hint(rec: dict) -> tuple[int, str] | None:
    """从将被丢弃的页眉中提取章号，用于补偿正文章标题 OCR 缺失。"""
    if rec.get("element_type") != "page_header":
        return None
    items = content_items(rec.get("raw", {}), "page_header_content")
    text = "".join(
        str(item.get("content", "")) for item in items if isinstance(item, dict)
    ).strip()
    key = chapter_key(text)
    page = rec.get("source_page")
    if key is None or not isinstance(page, int):
        return None
    return page, key


def rebuild_block(rec: dict, report: dict):
    """从 raw 重建块的 markdown 文本。返回 (kind, text, extra) 或 None(丢弃)。"""
    etype = rec.get("element_type")
    raw = rec.get("raw", {})
    content = raw.get("content", {})

    if etype in DROP_TYPES:
        report[f"dropped_{etype}"] += 1
        return None

    if etype in ("text", "title", "page_footnote"):
        key = {"text": "paragraph_content", "title": "title_content",
               "page_footnote": "page_footnote_content"}[etype]
        text, nm = items_to_text(content_items(raw, key))
        report["formulas_merged_inline"] += nm
        return etype, text, {}

    if etype == "equation_interline":
        math = normalize_latex(content.get("math_content", ""))
        img = (content.get("image_source") or {}).get("path", "")
        return etype, math, {"image": img}

    if etype == "table":
        html = content.get("html", "")
        if not re.search(r"<td[^>]*>\s*\S", html):
            report["dropped_empty_table"] += 1
            return None
        cap = " ".join(x.get("content", "") for x in content.get("table_caption", [])
                       if isinstance(x, dict))
        return etype, html, {"caption": cap.strip()}

    if etype in ("image", "chart"):
        cap_key = "image_caption" if etype == "image" else "chart_caption"
        cap = " ".join(x.get("content", "") for x in content.get(cap_key, [])
                       if isinstance(x, dict)).strip()
        path = (content.get("image_source") or {}).get("path", "")
        bbox = raw.get("bbox", [0, 0, 0, 0])
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        page = rec.get("source_page", 99)
        # 装饰图: 前4页满版大图(封面扫描) / 近似正方形小图(二维码)
        if page <= 4 and w * h > 0.40 * 1000 * 1400:
            report["dropped_cover_image"] += 1
            return None
        if w < 220 and h < 220 and 0.7 < (w / max(h, 1)) < 1.4:
            report["dropped_qr_image"] += 1
            return None
        return etype, path, {"caption": cap, "bbox": bbox}

    if etype == "code":
        code = "\n".join(x.get("content", "") for x in content.get("code_content", [])
                         if isinstance(x, dict))
        return etype, code, {"lang": content.get("code_language", "")}

    if etype == "algorithm":
        text, nm = items_to_text(content.get("algorithm_content", []))
        report["formulas_merged_inline"] += nm
        return etype, text, {}

    if etype == "list":
        lines = []
        for item in content.get("list_items", []):
            t, _ = items_to_text(item.get("item_content", []))
            lines.append(t)
        return etype, "\n".join(lines), {}

    return etype, "", {}

# ---------------------------------------------------------------- 结构处理


ACTIVITY_PAT = re.compile(
    r"^(探究|思考|观察|归纳|练习|做一做|议一议|想一想|说一说|试一试|复习巩固|综合运用|拓广探索)\s*$")

NUM3_PAT = re.compile(r"^\d+\.\d+\.\d+")   # 7.1.1 小节
NUM2_PAT = re.compile(r"^\d+\.\d+\s*\S")   # 7.1 节


def heading_level(title: str, mineru_level: int) -> int:
    t = title.strip()
    if CHAPTER_PAT.match(t):
        return 1
    if SECTION_PAT.match(t):
        return 2
    if ACTIVITY_PAT.match(t):
        return 4
    if NUM3_PAT.match(t):
        return 3
    if NUM2_PAT.match(t):
        return 2
    if SUB1_PAT.match(t):
        return 3
    if SUB2_PAT.match(t):
        return 4
    return max(1, min(mineru_level or 2, 4))


def assign_sections(blocks, chapter_header_hints=()):
    """打 front_matter / body / back_matter 标签。"""
    section = "front_matter"
    max_page = max((b.get("source_page", 0) for b in blocks), default=0)
    first_chapter = None
    first_header_hint = min(chapter_header_hints, key=lambda item: item[0], default=None)
    for b in blocks:
        if (
            section == "front_matter"
            and first_header_hint is not None
            and b.get("source_page", 0) >= first_header_hint[0]
        ):
            # 页眉不会写入 clean 数据，只用于恢复缺失的首章正文边界。
            section = "body"
            first_chapter = first_header_hint[1]
        if b["kind"] == "title":
            t = b["text"].strip()
            current_chapter_key = chapter_key(t)
            if section == "front_matter" and current_chapter_key is not None:
                section = "body"
                first_chapter = current_chapter_key
            elif section == "body":
                repeated_first_chapter = (
                    current_chapter_key is not None
                    and current_chapter_key == first_chapter
                    and b.get("source_page", 0) >= max_page * 0.7
                )
                if BACK_MATTER_PAT.match(t) or repeated_first_chapter:
                    section = "back_matter"
        b["section"] = section
    # 没找到章标题的书: 全部归入 body
    if all(b["section"] == "front_matter" for b in blocks):
        for b in blocks:
            b["section"] = "body"
    return blocks


def dedupe_duplicate_pages(blocks, report):
    """front_matter 中相邻页内容完全相同的(重复扫描的封面) -> 去掉后一页。"""
    pages = {}
    for b in blocks:
        if b["kind"] in ("text", "title") and b["text"].strip():
            pages.setdefault(b["source_page"], []).append(b["text"].strip())
    dup_pages = set()
    nums = sorted(pages)
    for a, b_ in pairwise(nums):
        if b_ - a <= 1 and pages[a] == pages[b_]:
            dup_pages.add(b_)
    if not dup_pages:
        return blocks
    report["duplicate_pages_dropped"] += len(dup_pages)
    return [b for b in blocks if b["source_page"] not in dup_pages]


def merge_paragraphs(blocks, report):
    """跨页/跨块段落合并: 前块足够长(排除封面短行)、无终止标点,
    且后块不像新段落起点 -> 合并。"""
    out = []
    for b in blocks:
        if (out and b["kind"] == "text" and out[-1]["kind"] == "text"
                and out[-1]["section"] == b["section"]):
            prev = out[-1]["text"].rstrip()
            if (len(prev) >= 30 and prev[-1] not in TERMINAL_PUNCT
                    and not NEW_PARA_START.match(b["text"])):
                out[-1]["text"] = prev + b["text"]
                out[-1]["end_page"] = b["source_page"]
                out[-1]["source_refs"].extend(b["source_refs"])
                report["paragraphs_merged"] += 1
                continue
        out.append(b)
    return out


def merge_split_display_formulas(blocks, report):
    """相邻两个 equation_interline 括号断裂 -> 合并。"""
    out, i = [], 0
    while i < len(blocks):
        b = blocks[i]
        if (b["kind"] == "equation_interline" and i + 1 < len(blocks)
                and blocks[i + 1]["kind"] == "equation_interline"
                and is_split_open(b["text"]) and is_split_close(blocks[i + 1]["text"])):
            nb = dict(b)
            nb["text"] = b["text"] + " " + blocks[i + 1]["text"]
            nb["end_page"] = blocks[i + 1]["source_page"]
            nb["source_refs"] = [*b["source_refs"], *blocks[i + 1]["source_refs"]]
            out.append(nb)
            report["formulas_merged_interline"] += 1
            i += 2
            continue
        out.append(b)
        i += 1
    return out

# ---------------------------------------------------------------- 渲染


def render_block(b) -> str:
    k, t = b["kind"], b["text"]
    if k == "title":
        return "#" * b["level"] + " " + t
    if k == "equation_interline":
        s = f"$$\n{t}\n$$"
        if b.get("extra", {}).get("image"):
            s += f"\n<!-- formula_image: {b['extra']['image']} -->"
        return s
    if k == "table":
        cap = b["extra"].get("caption")
        return (f"**{cap}**\n\n" if cap else "") + t
    if k in ("image", "chart"):
        cap = b["extra"].get("caption") or "图"
        bbox = b["extra"].get("bbox", [])
        return f"![{cap}]({t})\n<!-- page={b['source_page']} bbox={bbox} -->"
    if k == "code":
        return f"```{b['extra'].get('lang', '')}\n{t}\n```"
    if k == "page_footnote":
        return f"> 脚注: {t}"
    if k == "algorithm":
        return f"> {t}"
    if k == "list":
        return t
    return t


def render_md(blocks, book_name: str, section: str) -> str:
    lines = [f"<!-- book: {book_name} | section: {section} -->\n"]
    for b in blocks:
        if b["section"] != section:
            continue
        txt = render_block(b)
        if txt.strip():
            lines.append(txt)
    return "\n\n".join(lines) + "\n"

# ---------------------------------------------------------------- 主流程


def process_book(book_dir: Path, outroot: Path):
    report = json.loads(json.dumps({
        k: 0 for k in [
            "dropped_page_header", "dropped_page_footer", "dropped_page_number",
            "dropped_page_aside_text", "dropped_empty_table", "dropped_cover_image",
            "dropped_qr_image", "watermark_hits", "watermark_blocks_emptied",
            "empty_blocks_dropped", "paragraphs_merged", "formulas_merged_inline",
            "formulas_merged_interline", "latex_normalized_blocks",
            "punct_normalized_blocks", "duplicate_pages_dropped", "blocks_in", "blocks_out",
        ]
    }))
    counts = report

    raw_meta = json.loads((book_dir / "book.json").read_text())
    book_id = raw_meta["book_id"]
    book_name = clean_book_name(raw_meta.get("book_name", book_id))
    try:
        level = LEVEL_MAP[int(book_id.split("_")[0])]
    except Exception:
        level = "未知"

    blocks = []
    chapter_header_hints = []
    with open(book_dir / "content_list.jsonl") as f:
        for line in f:
            rec = json.loads(line)
            counts["blocks_in"] += 1
            header_hint = extract_chapter_header_hint(rec)
            if header_hint is not None:
                chapter_header_hints.append(header_hint)
            r = rebuild_block(rec, counts)
            if r is None:
                continue
            kind, text, extra = r

            if kind in ("text", "title", "page_footnote", "algorithm", "list"):
                text, wm = apply_watermark(text)
                counts["watermark_hits"] += wm
                if kind == "text" and not text.strip():
                    counts["watermark_blocks_emptied" if wm else "empty_blocks_dropped"] += 1
                    continue
                new = normalize_text(text)
                if new != text:
                    counts["punct_normalized_blocks"] += 1
                text = new
            elif kind == "equation_interline":
                before = text
                if before != (rec.get("raw", {}).get("content", {}).get("math_content", "")):
                    counts["latex_normalized_blocks"] += 1

            blocks.append({
                "kind": kind, "text": text, "extra": extra,
                "source_page": rec.get("source_page"),
                "end_page": rec.get("source_page"),
                "block_index": rec.get("block_index"),
                "source_refs": [build_source_ref(rec)],
                "mineru_level": (rec.get("raw", {}).get("content", {}) or {}).get("level"),
            })
    counts["blocks_out_raw"] = len(blocks)

    blocks.sort(key=lambda b: (b["source_page"] or 0, b["block_index"] or 0))

    for b in blocks:
        if b["kind"] == "title":
            b["level"] = heading_level(b["text"], b.pop("mineru_level") or 2)
        else:
            b.pop("mineru_level", None)

    blocks = assign_sections(blocks, chapter_header_hints)
    blocks = dedupe_duplicate_pages(blocks, counts)
    blocks = merge_paragraphs(blocks, counts)
    blocks = merge_split_display_formulas(blocks, counts)
    counts["blocks_out"] = len(blocks)

    # 章节统计
    sec_stat = {}
    for b in blocks:
        sec_stat[b["section"]] = sec_stat.get(b["section"], 0) + 1
    counts["section_blocks"] = sec_stat
    counts["formula_blocks_unbalanced_left"] = sum(
        1 for b in blocks if b["kind"] == "equation_interline"
        and (brace_delta(b["text"]) != 0 or is_split_open(b["text"])))

    outdir = outroot / book_id
    outdir.mkdir(parents=True, exist_ok=True)

    (outdir / "clean.md").write_text(render_md(blocks, book_name, "body"), encoding="utf-8")
    (outdir / "front_matter.md").write_text(
        render_md(blocks, book_name, "front_matter"), encoding="utf-8")
    (outdir / "back_matter.md").write_text(
        render_md(blocks, book_name, "back_matter"), encoding="utf-8")

    toc = [{"level": b["level"], "title": b["text"], "page": b["source_page"]}
           for b in blocks if b["kind"] == "title" and b["section"] == "body"]
    (outdir / "toc.json").write_text(
        json.dumps({"book_id": book_id, "book_name": book_name, "level": level,
                    "toc": toc}, ensure_ascii=False, indent=2), encoding="utf-8")

    with open(outdir / "clean_content_list.jsonl", "w", encoding="utf-8") as f:
        for b in blocks:
            f.write(json.dumps({
                "book_id": book_id, "book_name": book_name, "grade_level": level,
                "source_page": b["source_page"],
                "source_page_end": b.get("end_page", b["source_page"]),
                "source_refs": b["source_refs"],
                "section": b["section"],
                "element_type": b["kind"],
                **({"level": b["level"]} if b["kind"] == "title" else {}),
                "text": b["text"], "extra": b.get("extra", {}),
            }, ensure_ascii=False) + "\n")

    report.update({"book_id": book_id, "book_name": book_name, "grade_level": level,
                   "toc_entries": len(toc)})
    (outdir / "cleaning_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", help="book_id 或其数字前缀, 如 16")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--inroot", default="ocr_text")
    ap.add_argument("--outroot", default="clean_text")
    args = ap.parse_args()

    inroot, outroot = Path(args.inroot), Path(args.outroot)
    book_dirs = sorted(p for p in inroot.iterdir()
                       if p.is_dir() and (p / "content_list.jsonl").exists())
    if not args.all:
        if not args.book:
            ap.error("需要 --book 或 --all")
        book_dirs = [p for p in book_dirs
                     if p.name == args.book or p.name.startswith(args.book.zfill(2) + "_")]
        if not book_dirs:
            sys.exit(f"未找到书籍: {args.book}")

    reports = []
    for d in book_dirs:
        r = process_book(d, outroot)
        reports.append(r)
        print(f"[OK] {r['book_id']}: blocks {r['blocks_in']} -> {r['blocks_out']}, "
              f"水印 {r['watermark_hits']}, 段合并 {r['paragraphs_merged']}, "
              f"公式合并 {r['formulas_merged_inline'] + r['formulas_merged_interline']}")

    outroot.mkdir(exist_ok=True)
    (outroot / "cleaning_summary.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"汇总已写入 {outroot}/cleaning_summary.json")


if __name__ == "__main__":
    main()
