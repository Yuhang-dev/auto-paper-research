from __future__ import annotations

import argparse
from html import escape
from pathlib import Path


W, H = 1280, 720
BG = "#F8FAFC"
SECONDARY_BG = "#EAF0F6"
PRIMARY = "#123B5D"
ACCENT = "#E76F2E"
TEAL = "#2A9D8F"
TEXT = "#17212B"
MUTED = "#52616F"
DIVIDER = "#B9C8D4"
WHITE = "#FFFFFF"
FONT_BODY = "Microsoft YaHei, Aptos, Arial, sans-serif"
FONT_TITLE = "Microsoft YaHei, Georgia, serif"


def tx(
    x: float,
    y: float,
    value: str,
    size: int = 24,
    fill: str = TEXT,
    weight: str = "400",
    anchor: str = "start",
    family: str | None = None,
    spacing: float | None = None,
) -> str:
    attrs = [
        f'x="{x}"',
        f'y="{y}"',
        f'font-size="{size}"',
        f'fill="{fill}"',
        f'font-weight="{weight}"',
        f'text-anchor="{anchor}"',
    ]
    if family:
        attrs.append(f'font-family="{family}"')
    if spacing is not None:
        attrs.append(f'letter-spacing="{spacing}"')
    return f'<text {" ".join(attrs)}>{escape(value)}</text>'


def lines(
    x: float,
    y: float,
    values: list[str],
    size: int = 24,
    leading: int = 34,
    fill: str = TEXT,
    weight: str = "400",
    anchor: str = "start",
    family: str | None = None,
) -> str:
    return "".join(
        tx(x, y + index * leading, value, size, fill, weight, anchor, family)
        for index, value in enumerate(values)
    )


def rect(x: float, y: float, w: float, h: float, fill: str, rx: float = 0, stroke: str | None = None, sw: float = 1) -> str:
    outline = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"{outline}/>'


def circle(cx: float, cy: float, r: float, fill: str, stroke: str | None = None, sw: float = 1, opacity: float = 1) -> str:
    outline = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" fill-opacity="{opacity}"{outline}/>'


def line(x1: float, y1: float, x2: float, y2: float, color: str = DIVIDER, sw: float = 2, dash: str | None = None) -> str:
    dashed = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{sw}"{dashed}/>'


def group(group_id: str, bounds: tuple[int, int, int, int], content: str, role: str | None = None) -> str:
    role_attr = f' data-pptx-role="{role}"' if role else ""
    bound_text = " ".join(str(v) for v in bounds)
    return f'<g id="{group_id}" data-pptx-bounds="{bound_text}"{role_attr}>{content}</g>'


def pill(x: float, y: float, w: float, label: str, fill: str, color: str = WHITE, size: int = 16) -> str:
    return rect(x, y, w, 34, fill, 17) + tx(x + w / 2, y + 23, label, size, color, "700", "middle")


def status_dot(x: float, y: float, color: str) -> str:
    return circle(x, y, 6, color) + circle(x, y, 11, "none", color, 2)


def halftone(x: int, y: int, cols: int, rows: int, step: int, color: str, opacity: float = 0.13) -> str:
    dots: list[str] = []
    for row in range(rows):
        for col in range(cols):
            radius = 1.5 + ((row + col) % 3) * 0.7
            dots.append(circle(x + col * step, y + row * step, radius, color, opacity=opacity))
    return "".join(dots)


def poster_decor(page: int, accent: str = ACCENT) -> str:
    if page % 3 == 0:
        content = circle(1192, 92, 112, accent, opacity=0.92) + circle(1192, 92, 58, BG) + halftone(1080, 602, 8, 5, 18, PRIMARY)
    elif page % 3 == 1:
        content = circle(1206, 604, 122, PRIMARY, opacity=0.97) + circle(1206, 604, 72, BG) + halftone(1100, 70, 8, 5, 18, accent)
    else:
        content = rect(1162, 0, 118, 720, SECONDARY_BG) + circle(1174, 126, 82, accent) + halftone(1095, 585, 9, 5, 17, PRIMARY)
    return group(f"poster-decor-{page:02d}", (1040, 0, 240, 720), content, "decoration")


def header(page: int, section: str, title_lines: list[str], title_size: int = 38, tag_color: str = ACCENT) -> str:
    tag = pill(64, 45, 178, section, tag_color, WHITE, 15)
    title = lines(64, 122, title_lines, title_size, title_size + 10, PRIMARY, "700", family=FONT_TITLE)
    rule_y = 136 + (len(title_lines) - 1) * (title_size + 10)
    content = tag + title + rect(64, rule_y, 784, 4, TEAL)
    return group(f"header-{page:02d}", (64, 45, 900, rule_y - 37), content)


def footer(page: int, label: str) -> str:
    content = line(64, 672, 1216, 672, DIVIDER, 1) + tx(64, 699, label, 13, MUTED) + tx(1216, 699, f"{page:02d} / 16", 13, MUTED, "700", "end", FONT_TITLE)
    return group(f"footer-{page:02d}", (64, 670, 1152, 36), content, "footer")


def svg_page(page: int, role: str, groups: list[str], footer_label: str, include_decor: bool = True) -> str:
    body = [rect(0, 0, W, H, BG)]
    if include_decor:
        body.append(poster_decor(page))
    body.extend(groups)
    body.append(footer(page, footer_label))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'data-pptx-page-role="{role}" font-family="{FONT_BODY}">'
        + "".join(body)
        + "</svg>"
    )


def card(x: int, y: int, w: int, h: int, title: str, body: list[str], accent: str, tag: str | None = None) -> str:
    content = rect(x, y, w, h, WHITE, 14, DIVIDER, 1.5)
    content += rect(x, y, 12, h, accent, 6)
    if tag:
        content += pill(x + 28, y + 20, min(150, 50 + len(tag) * 12), tag, accent, WHITE, 14)
        title_y = y + 82
    else:
        title_y = y + 44
    content += tx(x + 28, title_y, title, 24, PRIMARY, "700")
    content += lines(x + 28, title_y + 40, body, 18, 28, MUTED)
    return content


def slide_01() -> str:
    art = (
        circle(1048, 218, 246, "#F2A079")
        + circle(1018, 246, 208, ACCENT)
        + circle(1036, 264, 122, BG)
        + rect(852, 430, 428, 122, PRIMARY)
        + circle(1174, 524, 84, TEAL)
        + rect(780, 574, 500, 34, PRIMARY)
        + halftone(894, 70, 9, 6, 20, PRIMARY, 0.16)
        + halftone(1006, 610, 10, 4, 18, ACCENT, 0.16)
    )
    title = (
        tx(72, 158, "RESEARCH HARNESS · PILOT 01", 18, ACCENT, "700", family=FONT_TITLE, spacing=2)
        + lines(72, 232, ["稀疏化模型在", "长上下文领域的", "性能与瓶颈"], 42, 54, PRIMARY, "700", family=FONT_TITLE)
    )
    subtitle = rect(72, 414, 520, 4, TEAL) + tx(72, 458, "结构化调研 · 非共识证据 · LoopEngineer", 24, TEXT, "400")
    badges = pill(72, 518, 132, "性能", PRIMARY) + pill(222, 518, 132, "瓶颈", ACCENT) + pill(372, 518, 132, "证据", TEAL)
    groups = [
        group("cover-art", (780, 0, 500, 620), art, "decoration"),
        group("cover-title", (72, 120, 690, 270), title),
        group("cover-subtitle", (72, 408, 650, 74), subtitle),
        group("cover-badges", (72, 514, 510, 62), badges),
    ]
    return svg_page(1, "cover", groups, "Auto Paper Research · 2026.08", include_decor=False)


def slide_02() -> str:
    body = card(64, 220, 350, 354, "具体拓扑影响质量", ["LongMixed：PG19 PPL 8.73", "SCCA flow：9.47，反而更差", "论文内受控比较"], TEAL, "VERIFIED")
    body += card(438, 220, 350, 354, "工程加速仍缺证据", ["只有复杂度与硬件描述", "缺 latency / throughput", "缺 peak-memory 对照"], ACCENT, "INSUFFICIENT")
    body += card(812, 220, 350, 354, "真实任务外推不足", ["当前仅验证 perplexity", "不能代表 QA / retrieval", "不能代表多跳推理"], ACCENT, "INSUFFICIENT")
    groups = [header(2, "01 · 初步判断", ["初步调研得到 3 个判断，", "但只有 1 个已有论文内受控证据"], 34), group("judgment-cards", (64, 220, 1098, 354), body)]
    return svg_page(2, "content", groups, "技术谱系与证据边界")


def slide_03() -> str:
    tracks = [
        ("01", "静态训练期扩展", "attention edge", "PPL / task quality", "当前样本集中", TEAL),
        ("02", "动态 sparse prefill", "query–key blocks", "TTFT / latency", "Q03 待补", ACCENT),
        ("03", "KV / token 稀疏化", "cached tokens", "decode memory", "Q04 待补", ACCENT),
        ("04", "Kernel-aware 执行", "block / kernel", "throughput / util.", "Q06 待补", ACCENT),
    ]
    content = ""
    y = 218
    for number, name, obj, metric, state, color in tracks:
        content += rect(64, y, 1080, 84, WHITE, 12, DIVIDER, 1.5)
        content += rect(64, y, 88, 84, color, 12)
        content += tx(108, y + 53, number, 30, WHITE, "700", "middle", FONT_TITLE)
        content += tx(182, y + 34, name, 23, PRIMARY, "700")
        content += tx(182, y + 64, f"稀疏对象：{obj}", 16, MUTED)
        content += tx(592, y + 34, "必须观察", 14, MUTED, "700")
        content += tx(592, y + 64, metric, 20, TEXT, "700")
        content += pill(946, y + 25, 168, state, color, WHITE, 14)
        y += 100
    groups = [header(3, "02 · 技术谱系", ["稀疏长上下文不是一类问题，", "而是 4 条不同技术主线"], 34), group("technical-tracks", (64, 218, 1080, 384), content)]
    return svg_page(3, "content", groups, "阶段 × 稀疏对象 × 指标")


def slide_04() -> str:
    content = rect(64, 210, 224, 122, SECONDARY_BG, 14, PRIMARY, 2) + tx(176, 252, "S2 / LongLoRA", 23, PRIMARY, "700", "middle") + tx(176, 286, "shifted local groups", 16, MUTED, "400", "middle") + pill(114, 304, 124, "DRAFT", PRIMARY, WHITE, 13)
    nodes = [
        ("SCCA fixed", "固定 K/V 跨块", 376, 198, TEAL),
        ("SCCA flow", "分头位移", 376, 344, TEAL),
        ("SDA-2 / SDA-4", "扩张位置", 700, 198, TEAL),
        ("LongMixed", "fixed + SDA heads", 700, 344, TEAL),
    ]
    for name, desc, x, y, color in nodes:
        content += rect(x, y, 260, 104, WHITE, 14, color, 2)
        content += tx(x + 130, y + 42, name, 22, PRIMARY, "700", "middle")
        content += tx(x + 130, y + 72, desc, 16, MUTED, "400", "middle")
        content += status_dot(x + 232, y + 24, color)
    content += line(288, 270, 376, 250, PRIMARY, 3) + line(288, 270, 376, 396, PRIMARY, 3)
    content += line(636, 250, 700, 250, PRIMARY, 3) + line(636, 396, 700, 396, PRIMARY, 3)
    content += line(506, 302, 830, 344, DIVIDER, 2, "8 8")
    content += rect(64, 500, 896, 82, PRIMARY, 12) + tx(88, 536, "当前证据偏置", 16, "#A7D8F2", "700") + tx(88, 568, "已验证分支集中在 1K–8K 的静态训练期 PPL；动态 prefill、decode 与 kernel 尚未闭环。", 19, WHITE, "700")
    groups = [header(4, "03 · 方法地图", ["当前证据集中在静态训练期分支"], 40), group("method-map", (64, 198, 896, 384), content)]
    return svg_page(4, "content", groups, "S2 → SCCA / SDA / LongMixed")


def slide_05() -> str:
    cx, cy = 610, 400
    content = circle(cx, cy, 104, PRIMARY) + circle(cx, cy, 72, BG) + tx(cx, cy - 8, "ATTENTION", 16, ACCENT, "700", "middle", FONT_TITLE) + tx(cx, cy + 22, "TOPOLOGY", 24, PRIMARY, "700", "middle", FONT_TITLE)
    variables = [
        ("张量位移", "Q 与 K/V 谁移动？", 74, 230, ACCENT),
        ("Schedule", "fixed / per-head / grouped", 420, 208, TEAL),
        ("Head 分配", "local / cross / dilated 占比", 805, 230, PRIMARY),
        ("Pattern mixing", "模式如何组合？", 124, 506, PRIMARY),
        ("退化 variant", "同设置是否反而变差？", 796, 506, ACCENT),
    ]
    for name, detail, x, y, color in variables:
        w = 284
        content += rect(x, y, w, 86, WHITE, 14, color, 2)
        content += tx(x + 20, y + 34, name, 22, PRIMARY, "700")
        content += tx(x + 20, y + 64, detail, 16, MUTED)
        ex = x + w / 2
        ey = y + 43
        content += line(cx, cy, ex, ey, color, 2, "6 6")
    content += pill(466, 568, 288, "Wiki schema 必须显式记录", ACCENT, WHITE, 15)
    groups = [header(5, "04 · 结构变量", ["决定质量的是具体拓扑变量，", "不只是“更全局”或“更稀疏”"], 34), group("topology-radial", (64, 208, 1025, 410), content)]
    return svg_page(5, "content", groups, "后续跨论文比较的最小结构单元")


def slide_06() -> str:
    data = [("LongMixed", 8.73, TEAL, "−7.2%"), ("SCCA fixed", 9.17, PRIMARY, "−2.6%"), ("S2 baseline", 9.41, MUTED, "基线"), ("SCCA flow", 9.47, ACCENT, "+0.6%")]
    content = tx(64, 224, "PG19 perplexity · 越低越好", 18, MUTED, "700")
    y = 258
    for name, value, color, delta in data:
        width = 240 + (9.6 - value) * 450
        content += tx(64, y + 28, name, 20, PRIMARY, "700")
        content += rect(254, y, 600, 42, SECONDARY_BG, 8)
        content += rect(254, y, width, 42, color, 8)
        content += tx(884, y + 30, f"{value:.2f}", 26, color, "700", family=FONT_TITLE)
        content += pill(976, y + 4, 112, delta, color, WHITE, 14)
        y += 70
    content += rect(64, 560, 1024, 74, PRIMARY, 12)
    content += tx(88, 590, "同一设置", 15, "#A7D8F2", "700")
    content += tx(88, 620, "LLaMA2-7B · PI + LoRA · RedPajama 子集 · 8K context", 19, WHITE, "700")
    content += tx(1064, 620, "SCCA Table 2", 14, "#A7D8F2", "700", "end")
    groups = [header(6, "05 · 论文内结果", ["相同 8K 条件下，LongMixed 最好；", "SCCA flow 反而略差"], 34), group("ppl-bars", (64, 220, 1024, 414), content)]
    return svg_page(6, "content", groups, "论文内受控证据 · SCCA PDF p.5–6")


def slide_07() -> str:
    content = circle(600, 398, 132, PRIMARY) + circle(600, 398, 92, BG)
    content += tx(600, 378, "非共识线索", 18, ACCENT, "700", "middle")
    content += lines(600, 413, ["更大全局感受野", "≠ 更好质量"], 23, 32, PRIMARY, "700", "middle")
    statuses = [
        ("论文内数值", "VERIFIED", 92, 248, TEAL),
        ("跨论文共识", "INSUFFICIENT", 836, 248, ACCENT),
        ("工程性能", "INSUFFICIENT", 92, 500, ACCENT),
        ("真实任务外推", "INSUFFICIENT", 836, 500, ACCENT),
    ]
    for label, state, x, y, color in statuses:
        content += rect(x, y, 264, 92, WHITE, 14, color, 2)
        content += tx(x + 22, y + 35, label, 20, PRIMARY, "700")
        content += pill(x + 22, y + 50, 164, state, color, WHITE, 13)
        content += line(600, 398, x + 132, y + 46, color, 2, "6 6")
    content += tx(600, 618, "能否定过强直觉，但不能宣称普遍规律", 20, MUTED, "700", "middle")
    groups = [header(7, "06 · 非共识边界", ["证据强度决定结论边界"], 40), group("consensus-map", (92, 240, 1008, 390), content)]
    return svg_page(7, "content", groups, "verified ≠ consensus")


def slide_08() -> str:
    bands = [
        ("01", "语言建模质量", "PG19 / Proof-pile PPL · 1K–8K", "当前 VERIFIED", TEAL),
        ("02", "真实长文任务", "QA · retrieval · 多跳推理 · 摘要 · 代码", "待验证", ACCENT),
        ("03", "工程效率", "TTFT · latency · throughput · peak memory · KV memory", "待验证", ACCENT),
    ]
    content = ""
    y = 228
    for number, label, metrics, state, color in bands:
        content += rect(64, y, 1060, 118, WHITE, 14, DIVIDER, 1.5)
        content += rect(64, y, 102, 118, color, 14)
        content += tx(115, y + 72, number, 34, WHITE, "700", "middle", FONT_TITLE)
        content += tx(198, y + 42, label, 24, PRIMARY, "700")
        content += tx(198, y + 80, metrics, 18, MUTED)
        content += pill(916, y + 42, 176, state, color, WHITE, 14)
        y += 138
    groups = [header(8, "07 · Benchmark 地图", ["Perplexity 不能代表完整长文任务能力"], 40), group("metric-bands", (64, 228, 1060, 394), content)]
    return svg_page(8, "content", groups, "Quality / Task / Engineering 三条证据轴")


def slide_09() -> str:
    content = rect(64, 245, 210, 126, PRIMARY, 16) + tx(169, 290, "理论承诺", 20, "#A7D8F2", "700", "middle") + tx(169, 334, "线性复杂度", 30, WHITE, "700", "middle", FONT_TITLE)
    frictions = [("不规则访存", 334), ("索引 / select", 516), ("kernel launch", 698), ("硬件利用率", 880)]
    for label, x in frictions:
        content += rect(x, 245, 150, 126, WHITE, 14, ACCENT, 2)
        content += circle(x + 75, 274, 14, ACCENT)
        content += tx(x + 75, 324, label, 17, PRIMARY, "700", "middle")
        content += line(x - 60, 308, x, 308, DIVIDER, 3)
    content += rect(1060, 245, 156, 126, TEAL, 16) + tx(1138, 288, "真实收益", 18, WHITE, "700", "middle") + lines(1138, 324, ["TTFT", "throughput"], 18, 26, WHITE, "700", "middle")
    content += rect(64, 430, 520, 154, SECONDARY_BG, 14) + pill(84, 450, 124, "PREFILL", PRIMARY, WHITE, 14) + tx(84, 508, "主瓶颈：attention compute", 22, PRIMARY, "700") + tx(84, 546, "必须实测：TTFT / latency", 18, MUTED)
    content += rect(608, 430, 520, 154, SECONDARY_BG, 14) + pill(628, 450, 124, "DECODE", ACCENT, WHITE, 14) + tx(628, 508, "主瓶颈：KV memory", 22, PRIMARY, "700") + tx(628, 546, "必须实测：token selection / memory", 18, MUTED)
    groups = [header(9, "08 · 工程瓶颈", ["理论复杂度穿过 kernel 与硬件，", "才可能成为真实加速"], 34), group("engineering-friction", (64, 245, 1152, 339), content)]
    return svg_page(9, "content", groups, "理论 FLOPs ≠ wall-clock speedup")


def slide_10() -> str:
    items = [("OWNER", "官方归属"), ("LICENSE", "许可"), ("COMMIT", "固定版本"), ("ENTRY", "运行入口"), ("KERNEL", "真实稀疏路径"), ("COMMAND", "硬件固定命令")]
    content = tx(600, 245, "OPEN-SOURCE", 23, ACCENT, "700", "middle", FONT_TITLE) + tx(600, 285, "≠ REPRODUCIBLE", 35, PRIMARY, "700", "middle", FONT_TITLE)
    positions = [(64, 222), (64, 376), (344, 476), (736, 476), (1016, 376), (1016, 222)]
    for (code, label), (x, y) in zip(items, positions):
        content += circle(x + 82, y + 50, 50, WHITE, PRIMARY, 2)
        content += tx(x + 82, y + 44, code, 13, ACCENT, "700", "middle", FONT_TITLE)
        content += tx(x + 82, y + 70, label, 15, PRIMARY, "700", "middle")
        content += line(600, 314, x + 82, y + 50, DIVIDER, 2, "6 6")
    content += rect(240, 590, 720, 52, PRIMARY, 12)
    content += tx(600, 623, "NEXT · Q03 dynamic prefill · Q04 KV/token · Q06 kernel · Q07 failure baseline", 16, WHITE, "700", "middle")
    groups = [header(10, "09 · 开源与复现", ["开源项目不是一个 URL，", "而是一条可复现证据链"], 34), group("opensource-wheel", (64, 218, 1116, 424), content)]
    return svg_page(10, "content", groups, "当前仓库证据仍未进入 verified Wiki")


def slide_11() -> str:
    content = rect(438, 240, 400, 160, PRIMARY, 18) + tx(638, 280, "RESEARCH HARNESS", 18, "#A7D8F2", "700", "middle", FONT_TITLE) + tx(638, 326, "LangChain + LangGraph", 27, WHITE, "700", "middle") + tx(638, 366, "Inner tools × Outer control", 18, WHITE, "400", "middle")
    nodes = [
        ("CLI / 用户", 74, 246, ACCENT),
        ("DeepXiv SDK", 74, 430, PRIMARY),
        ("Markdown / YAML", 438, 492, TEAL),
        ("D: SQLite", 686, 492, TEAL),
        ("Error Book", 934, 492, ACCENT),
        ("Outer Loop", 934, 246, PRIMARY),
    ]
    for label, x, y, color in nodes:
        content += rect(x, y, 210, 82, WHITE, 14, color, 2) + tx(x + 105, y + 49, label, 20, PRIMARY, "700", "middle")
    content += line(284, 287, 438, 287, ACCENT, 3) + line(838, 287, 934, 287, PRIMARY, 3)
    content += line(178, 430, 438, 364, PRIMARY, 2, "7 7")
    content += line(638, 400, 543, 492, TEAL, 3) + line(638, 400, 791, 492, TEAL, 3) + line(838, 364, 1039, 492, ACCENT, 2, "7 7")
    content += tx(638, 630, "领域真源 / 可恢复状态 / 研究控制互相分离", 20, MUTED, "700", "middle")
    groups = [header(11, "10 · Harness 架构", ["领域真源、运行状态与研究控制分开"], 40), group("architecture", (74, 236, 1070, 400), content)]
    return svg_page(11, "content", groups, "Markdown 真源 · SQLite 状态 · Gap-directed control")


def slide_12() -> str:
    columns = [
        ("SKILL", "操作协议", ["search-paper", "ingest-paper", "verify-evidence", "revise / analyze"], PRIMARY),
        ("PYTHON", "确定性执行", ["ID / schema", "去重 / backlink", "coverage / Done", "预算 / 脱敏"], TEAL),
        ("LLM", "语义判断", ["论文语义", "claim 分类", "歧义判断", "跨证据综合"], ACCENT),
    ]
    content = ""
    x = 64
    for code, subtitle, items, color in columns:
        content += rect(x, 220, 340, 356, WHITE, 16, color, 2)
        content += rect(x, 220, 340, 76, color, 16)
        content += tx(x + 26, 255, code, 22, WHITE, "700", family=FONT_TITLE)
        content += tx(x + 314, 255, subtitle, 16, WHITE, "700", "end")
        yy = 334
        for item in items:
            content += circle(x + 34, yy - 7, 6, color) + tx(x + 58, yy, item, 20, PRIMARY, "700")
            yy += 58
        x += 366
    content += rect(64, 602, 1072, 42, PRIMARY, 10) + tx(600, 629, "能计算、能校验、会重复的工作，不交给模型猜", 18, WHITE, "700", "middle")
    groups = [header(12, "11 · 责任边界", ["Skill 定义协议，脚本守住确定性边界"], 40), group("responsibility-columns", (64, 220, 1072, 424), content)]
    return svg_page(12, "content", groups, "Skill ≠ prompt file")


def slide_13() -> str:
    cx, cy = 610, 408
    content = circle(cx, cy, 94, PRIMARY) + circle(cx, cy, 62, BG) + tx(cx, cy - 5, "RESEARCH", 15, ACCENT, "700", "middle", FONT_TITLE) + tx(cx, cy + 24, "TRUTH", 24, PRIMARY, "700", "middle", FONT_TITLE)
    nodes = [
        ("OBSERVE", "artifact + metrics", 516, 220, PRIMARY),
        ("CLASSIFY", "schema / evidence", 826, 300, ACCENT),
        ("CHANGE", "script / Skill / query", 780, 516, TEAL),
        ("RERUN", "isolated canary", 318, 516, PRIMARY),
        ("MEASURE", "progress + recurrence", 248, 300, ACCENT),
    ]
    centers: list[tuple[float, float]] = []
    for name, detail, x, y, color in nodes:
        content += rect(x, y, 244, 80, WHITE, 40, color, 2)
        content += tx(x + 122, y + 32, name, 17, color, "700", "middle", FONT_TITLE)
        content += tx(x + 122, y + 59, detail, 15, MUTED, "400", "middle")
        centers.append((x + 122, y + 40))
    for index, (x1, y1) in enumerate(centers):
        x2, y2 = centers[(index + 1) % len(centers)]
        content += line(x1, y1, x2, y2, DIVIDER, 3, "8 8")
    content += pill(454, 622, 312, "失败必须改变下一轮", ACCENT, WHITE, 16)
    groups = [header(13, "12 · LoopEngineer", ["每个失败都要形成可执行改进，", "而不是只重试模型"], 34), group("loop-engineer", (248, 212, 822, 444), content)]
    return svg_page(13, "content", groups, "Observe → classify → change → rerun → measure")


def slide_14() -> str:
    content = rect(64, 220, 360, 354, PRIMARY, 20)
    content += tx(244, 290, "386", 116, WHITE, "700", "middle", FONT_TITLE)
    content += tx(244, 340, "SECONDS", 22, "#A7D8F2", "700", "middle", FONT_TITLE)
    content += tx(244, 406, "真实联网 Canary", 23, WHITE, "700", "middle")
    content += pill(128, 472, 232, "FORMAL WIKI UNCHANGED", TEAL, WHITE, 13)
    stages = [("SEARCH", "1 query", PRIMARY), ("SCREEN", "5 → 3", ACCENT), ("INGEST", "1 paper", PRIMARY), ("VERIFY", "40 / 40", TEAL)]
    x = 482
    for index, (name, metric, color) in enumerate(stages):
        content += rect(x, 248, 150, 112, WHITE, 14, color, 2)
        content += tx(x + 75, 285, name, 15, color, "700", "middle", FONT_TITLE)
        content += tx(x + 75, 329, metric, 24, PRIMARY, "700", "middle")
        if index < len(stages) - 1:
            content += line(x + 150, 304, x + 174, 304, DIVIDER, 3)
        x += 174
    metrics = [("40", "entities"), ("1", "schema repair"), ("0", "unresolved")]
    x = 482
    for value, label in metrics:
        content += rect(x, 410, 208, 136, SECONDARY_BG, 14)
        content += tx(x + 104, 464, value, 42, PRIMARY, "700", "middle", FONT_TITLE)
        content += tx(x + 104, 508, label, 16, MUTED, "700", "middle")
        x += 230
    content += tx(810, 602, "小规模纵切面足以暴露结构化输出、隔离与验证问题", 18, MUTED, "700", "middle")
    groups = [header(14, "13 · Canary", ["一次小规模运行打通 search → ingest → verify"], 34), group("canary-dashboard", (64, 185, 1108, 425), content)]
    return svg_page(14, "content", groups, "reverification-20260830-235346")


def slide_15() -> str:
    problems = [
        ("SCHEMA", "claim scope 为空", "repair 后通过"),
        ("COVERAGE", "facet tag ≠ metric evidence", "仍需规则"),
        ("GRANULARITY", "1 表 → 24 records", "计数膨胀"),
        ("ALIAS", "method / concept collision", "待 canonicalize"),
    ]
    content = ""
    y = 220
    for code, problem, state in problems:
        content += rect(64, y, 566, 82, WHITE, 12, ACCENT, 1.5)
        content += pill(82, y + 17, 132, code, ACCENT, WHITE, 12)
        content += tx(232, y + 34, problem, 19, PRIMARY, "700")
        content += tx(232, y + 61, state, 15, MUTED)
        y += 96
    steps = [("1", "recorded", TEAL), ("2", "aggregate", DIVIDER), ("3", "detect recurrence", DIVIDER), ("4", "propose rule", DIVIDER), ("5", "update Skill", DIVIDER)]
    y = 220
    for number, label, color in steps:
        content += circle(746, y + 24, 22, color) + tx(746, y + 31, number, 16, WHITE if color != DIVIDER else PRIMARY, "700", "middle", FONT_TITLE)
        content += tx(790, y + 31, label, 20, PRIMARY if number == "1" else MUTED, "700")
        if number != "5":
            content += line(746, y + 46, 746, y + 66, DIVIDER, 3)
        y += 72
    content += rect(682, 578, 424, 64, PRIMARY, 12) + tx(894, 618, "当前：可审计日志，不是自动修复器", 18, WHITE, "700", "middle")
    groups = [header(15, "14 · Error Book", ["已能记录问题，但尚未形成自动优化闭环"], 40), group("error-book-state", (64, 220, 1042, 422), content)]
    return svg_page(15, "content", groups, "README + errors.jsonl + 3 recurrence keys")


def slide_16() -> str:
    content = pill(64, 218, 238, "RESEARCH · 4 SAMPLES", PRIMARY, WHITE, 14)
    research = [("Q03", "dynamic prefill", "TTFT / latency"), ("Q04", "KV / token", "decode memory"), ("Q06", "kernel-aware", "same hardware"), ("Q07", "counter-evidence", "strong baseline")]
    x = 64
    for code, name, metric in research:
        content += rect(x, 272, 250, 124, WHITE, 14, PRIMARY, 1.5)
        content += pill(x + 18, 288, 70, code, ACCENT, WHITE, 13)
        content += tx(x + 18, 344, name, 19, PRIMARY, "700")
        content += tx(x + 18, 374, metric, 15, MUTED)
        x += 270
    content += pill(64, 430, 238, "ENGINEERING · 2 LOOPS", ACCENT, WHITE, 14)
    content += rect(64, 484, 520, 106, SECONDARY_BG, 14) + tx(88, 525, "Error Book aggregator", 22, PRIMARY, "700") + tx(88, 558, "recurrence · severity · rule proposal · test binding", 16, MUTED)
    content += rect(610, 484, 520, 106, SECONDARY_BG, 14) + tx(634, 525, "Evidence-grounded synthesis", 22, PRIMARY, "700") + tx(634, 558, "formal report · technical map · presentation", 16, MUTED)
    content += line(330, 608, 600, 644, TEAL, 3) + line(870, 608, 600, 644, TEAL, 3)
    content += pill(458, 626, 284, "CROSS-FAMILY ASSESSMENT", TEAL, WHITE, 14)
    groups = [header(16, "15 · 下一轮", ["先补 4 类研究证据，", "再闭环 Error Book 与自动总结"], 34), group("roadmap", (64, 218, 1066, 446), content)]
    return svg_page(16, "content", groups, "下一轮 Gate：配置与 measurement 分开计数")


SLIDES = {
    1: ("01_cover.svg", slide_01, "cover + poster geometry"),
    2: ("02_three_findings.svg", slide_02, "three evidence cards"),
    3: ("03_technical_tracks.svg", slide_03, "four-track taxonomy"),
    4: ("04_method_map.svg", slide_04, "method lineage map"),
    5: ("05_topology_variables.svg", slide_05, "radial topology variables"),
    6: ("06_ppl_result.svg", slide_06, "controlled PPL comparison"),
    7: ("07_nonconsensus_boundary.svg", slide_07, "evidence boundary map"),
    8: ("08_metric_map.svg", slide_08, "three-axis metric map"),
    9: ("09_engineering_bottleneck.svg", slide_09, "kernel friction pipeline"),
    10: ("10_opensource_audit.svg", slide_10, "reproducibility audit wheel"),
    11: ("11_harness_architecture.svg", slide_11, "layered Harness architecture"),
    12: ("12_skill_script_llm.svg", slide_12, "responsibility boundary"),
    13: ("13_loopengineer.svg", slide_13, "five-stage feedback loop"),
    14: ("14_canary.svg", slide_14, "Canary evidence dashboard"),
    15: ("15_error_book.svg", slide_15, "problem tickets + maturity ladder"),
    16: ("16_next_loop.svg", slide_16, "research/engineering roadmap"),
}


NOTES = {
    1: "这不是一次性生成综述，而是一次可审计调研 Harness 的首期汇报。全套内容分为调研结果和工程复盘，两者约为三比二。",
    2: "先给三条判断。只有第一条由同一论文、同一设置下的受控比较支持；工程收益与真实任务能力目前都只能标记为证据不足。",
    3: "这里先拆概念。训练期稀疏、动态 prefill、decode 的 KV/token 稀疏和 kernel-aware 执行分别对应不同瓶颈，不能放在同一个排行榜里。",
    4: "首期真正完成验证的是 SCCA 分支。LongLoRA 的 S2 基线仍是 draft；动态 prefill、decode 和 kernel 三条线尚未进入独立验证闭环。",
    5: "后续跨论文比较不能只写局部或全局。需要记录张量位移、schedule、head 分配、pattern mixing，以及同条件下是否出现退化 variant。",
    6: "这是当前最硬的一页证据。同为 LLaMA2-7B、PI 加 LoRA、RedPajama 子集和 8K 上下文，LongMixed 的 PG19 PPL 最低，而 SCCA flow 比 S2 略差。",
    7: "这支持一个非共识线索：更大的全局感受野并不自动带来更好的质量。但这仍是论文内证据，不能扩展成跨模型共识，也不能声称工程加速。",
    8: "当前 verified 的指标只有 PG19 与 Proof-pile perplexity。真实长文任务和工程效率是另外两条证据轴，不能用 PPL 替代。",
    9: "理论线性复杂度到真实加速之间隔着访存、索引、kernel launch 和硬件利用率。prefill 与 decode 的瓶颈也不同，因此必须分别测量。",
    10: "开源证据不能只保存仓库链接。至少需要官方归属、许可、固定 commit、入口、真实稀疏 kernel 路径和硬件固定复现命令。当前这一链条还没进入 verified Wiki。",
    11: "系统把三类状态分开：Markdown 和 YAML 保存科学真源，D 盘 SQLite 保存可恢复运行状态，Outer Loop 根据可计算真源选择下一动作。",
    12: "Skill 是动作协议，不是单篇论文。确定性脚本负责 ID、schema、去重、backlink、coverage、Done 和预算；LLM 只处理真正需要语义判断的部分。",
    13: "LoopEngineer 不是失败后再问一次模型，而是把问题分类，修改脚本、Skill、schema 或检索词，在隔离环境重跑，并比较进展和复发率。",
    14: "这次小规模 Canary 用 386 秒打通检索、筛选、摄取和验证。它处理一篇论文，创建四十个实体，验证四十比四十，并保持正式 Wiki 不变。",
    15: "Error Book 目前只到记录层：README、JSONL 和三个 recurrence key 已有，但聚合、复发检测、规则建议、测试绑定和 Skill 自动更新都没有完成。",
    16: "下一轮研究侧补动态 prefill、KV/token、kernel 和反例基线；工程侧补 Error Book 聚合和 evidence-grounded synthesis。两条轨道汇合后才进入跨家族正式综述。",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("page", type=int)
    args = parser.parse_args()
    if args.page not in SLIDES:
        raise SystemExit(f"page {args.page} is not implemented")
    filename, builder, modules = SLIDES[args.page]
    output_dir = args.project / "svg_output"
    notes_dir = args.project / "notes"
    output_dir.mkdir(parents=True, exist_ok=True)
    notes_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / filename
    target.write_text(builder(), encoding="utf-8")
    notes_target = notes_dir / f"{Path(filename).stem}.md"
    notes_target.write_text(NOTES[args.page] + "\n", encoding="utf-8")
    print(f"P{args.page:02d} modules: core, vintage-poster, {modules}, speaker-notes")
    print(target)


if __name__ == "__main__":
    main()
