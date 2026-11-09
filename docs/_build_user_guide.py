# -*- coding: utf-8 -*-
"""生成《广东电力数据采集系统使用说明》Word 文档。

面向业务使用人员，按当前软件界面编写。灰色方框是截图占位。
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor, Twips

OUT = Path(__file__).resolve().parent / "广东电力数据采集系统-使用说明.docx"
CONTENT_WIDTH = Cm(17)
FONT = "微软雅黑"
BLUE = "1F5FFF"
INK = "1A1D21"
MUTED = "5A6068"
LINE = "E7E8E3"


def font_run(run, size: float, bold: bool = False, color: str = INK, italic: bool = False) -> None:
    """设置一个文字片段的中文字体。

    Args:
        run: python-docx 的文字片段。
        size (float): 字号，单位磅。
        bold (bool): 是否加粗。
        color (str): 六位十六进制颜色，不含 ``#``。
        italic (bool): 是否斜体。

    Returns:
        None: 只修改 ``run``，无返回值。

    Examples:
        >>> from docx import Document
        >>> run = Document().add_paragraph().add_run("采集")
        >>> font_run(run, 11, bold=True, color="1F5FFF")
        >>> run.bold
        True
    """
    run.font.name = FONT
    run.bold = bold
    run.italic = italic
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor.from_string(color)
    r_pr = run._element.get_or_add_rPr()
    r_fonts = r_pr.find(qn("w:rFonts"))
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.append(r_fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        r_fonts.set(qn(attr), FONT)


def font_style(style, size: float, bold: bool = False, color: str = INK) -> None:
    """把段落样式的中西文字体设成微软雅黑。

    Args:
        style: python-docx 段落样式。
        size (float): 字号，单位磅。
        bold (bool): 是否加粗。
        color (str): 六位十六进制颜色，不含 ``#``。

    Returns:
        None: 只修改样式。

    Examples:
        >>> from docx import Document
        >>> style = Document().styles["Normal"]
        >>> font_style(style, 11)
        >>> style.font.size.pt
        11.0
    """
    style.font.name = FONT
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = RGBColor.from_string(color)
    r_pr = style.element.get_or_add_rPr()
    r_fonts = r_pr.find(qn("w:rFonts"))
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.append(r_fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        r_fonts.set(qn(attr), FONT)


def shade_cell(cell, fill: str) -> None:
    """给表格单元格铺底色。

    Args:
        cell: python-docx 单元格。
        fill (str): 六位十六进制颜色，不含 ``#``。

    Returns:
        None: 只修改单元格。

    Examples:
        >>> from docx import Document
        >>> cell = Document().add_table(1, 1).cell(0, 0)
        >>> shade_cell(cell, "F7F8FA")
        >>> cell._tc.tcPr.xml.find("F7F8FA") > 0
        True
    """
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def border_cell(cell, color: str = LINE, size: str = "4", accent: str | None = None) -> None:
    """给单元格加细边框，左边可以换成强调色。

    Args:
        cell: python-docx 单元格。
        color (str): 四边默认颜色。
        size (str): 边框粗细，单位是八分之一磅。
        accent (str | None): 左边框颜色。给出时左边加粗。

    Returns:
        None: 只修改单元格。

    Examples:
        >>> from docx import Document
        >>> cell = Document().add_table(1, 1).cell(0, 0)
        >>> border_cell(cell, accent="1F5FFF")
        >>> "1F5FFF" in cell._tc.tcPr.xml
        True
    """
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for edge in ("top", "left", "bottom", "right"):
        element = OxmlElement(f"w:{edge}")
        element.set(qn("w:val"), "single")
        element.set(qn("w:space"), "0")
        if edge == "left" and accent:
            element.set(qn("w:sz"), "16")
            element.set(qn("w:color"), accent)
        else:
            element.set(qn("w:sz"), size)
            element.set(qn("w:color"), color)
        borders.append(element)
    tc_pr.append(borders)


def set_cell_margins(cell, twips: int = 80) -> None:
    """设置单元格内边距。

    Args:
        cell: python-docx 单元格。
        twips (int): 四边内边距，单位缇（1 磅 = 20 缇）。

    Returns:
        None: 只修改单元格。

    Examples:
        >>> from docx import Document
        >>> cell = Document().add_table(1, 1).cell(0, 0)
        >>> set_cell_margins(cell, 100)
        >>> "100" in cell._tc.tcPr.xml
        True
    """
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_mar = OxmlElement("w:tcMar")
    for edge in ("top", "left", "bottom", "right"):
        node = OxmlElement(f"w:{edge}")
        node.set(qn("w:w"), str(twips))
        node.set(qn("w:type"), "dxa")
        tc_mar.append(node)
    tc_pr.append(tc_mar)


def tighten(paragraph, before: float = 0, after: float = 6, line: float = 1.15) -> None:
    """收紧段落间距。

    Args:
        paragraph: python-docx 段落。
        before (float): 段前间距，单位磅。
        after (float): 段后间距，单位磅。
        line (float): 行距倍数。

    Returns:
        None: 只修改段落。

    Examples:
        >>> from docx import Document
        >>> paragraph = Document().add_paragraph("行距")
        >>> tighten(paragraph, after=8)
        >>> paragraph.paragraph_format.space_after.pt
        8.0
    """
    fmt = paragraph.paragraph_format
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    fmt.line_spacing = line
    fmt.line_spacing_rule = WD_LINE_SPACING.MULTIPLE


def add_runs(paragraph, parts: list[tuple[str, dict]]) -> None:
    """往段落里追加若干已经设好字体的文字片段。

    Args:
        paragraph: python-docx 段落。
        parts (list[tuple[str, dict]]): 每项是文字和 ``font_run`` 的关键字参数。

    Returns:
        None: 只修改段落。

    Examples:
        >>> from docx import Document
        >>> paragraph = Document().add_paragraph()
        >>> add_runs(paragraph, [("任务", {"size": 11, "bold": True})])
        >>> paragraph.text
        '任务'
    """
    for text, options in parts:
        font_run(paragraph.add_run(text), **options)


def body(doc: Document, text: str, *, bold: bool = False, size: float = 11, color: str = INK, after: float = 6) -> None:
    """写一段正文。

    Args:
        doc (Document): 目标文档。
        text (str): 段落文字。
        bold (bool): 是否整段加粗。
        size (float): 字号，单位磅。
        color (str): 文字颜色。
        after (float): 段后间距，单位磅。

    Returns:
        None: 段落已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> body(doc, "打开程序")
        >>> doc.paragraphs[-1].text
        '打开程序'
    """
    paragraph = doc.add_paragraph()
    tighten(paragraph, after=after)
    font_run(paragraph.add_run(text), size, bold=bold, color=color)


def goal(doc: Document, text: str) -> None:
    """写章节要完成的事。

    Args:
        doc (Document): 目标文档。
        text (str): 这一节要完成的事。

    Returns:
        None: 段落已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> goal(doc, "导入初始数据")
        >>> "导入初始数据" in doc.paragraphs[-1].text
        True
    """
    paragraph = doc.add_paragraph()
    tighten(paragraph, after=10)
    add_runs(paragraph, [
        ("这一节要完成的事　", {"size": 11, "bold": True, "color": BLUE}),
        (text, {"size": 11, "color": INK}),
    ])


def callout(doc: Document, title: str, lines: list[str], fill: str, accent: str) -> None:
    """写一个带左边色条的提示框。

    Args:
        doc (Document): 目标文档。
        title (str): 提示标题。
        lines (list[str]): 提示正文，每项一段。
        fill (str): 底色。
        accent (str): 左边条颜色。

    Returns:
        None: 表格已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> callout(doc, "请注意", ["导入后点取消"], "F3F6FF", "1F5FFF")
        >>> doc.tables[-1].cell(0, 0).text.startswith("请注意")
        True
    """
    table = doc.add_table(rows=1, cols=1)
    table.autofit = False
    table.columns[0].width = CONTENT_WIDTH
    cell = table.cell(0, 0)
    shade_cell(cell, fill)
    border_cell(cell, color=fill, accent=accent)
    set_cell_margins(cell, 120)
    cell.text = ""
    first = cell.paragraphs[0]
    tighten(first, after=2)
    font_run(first.add_run(title), 11, bold=True, color=accent)
    for line in lines:
        paragraph = cell.add_paragraph()
        tighten(paragraph, after=2)
        font_run(paragraph.add_run(line), 10.5, color=INK)
    spacer(doc, 8)


def note(doc: Document, lines: list[str]) -> None:
    """写给使用人员的注意框。

    Args:
        doc (Document): 目标文档。
        lines (list[str]): 注意内容。

    Returns:
        None: 提示框已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> note(doc, ["不要点恢复出厂设置"])
        >>> "请注意" in doc.tables[-1].cell(0, 0).text
        True
    """
    callout(doc, "请注意", lines, "F3F6FF", BLUE)


def draft_note(doc: Document, lines: list[str]) -> None:
    """写发给客户前要删掉的编写说明。

    Args:
        doc (Document): 目标文档。
        lines (list[str]): 编写说明。

    Returns:
        None: 提示框已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> draft_note(doc, ["贴上截图"])
        >>> "发给客户前删除" in doc.tables[-1].cell(0, 0).text
        True
    """
    callout(doc, "编写说明（发给客户前删除本框）", lines, "FFF7E6", "C47D09")


def spacer(doc: Document, after: float = 6) -> None:
    """插入一个空段，用来隔开表格和正文。

    Args:
        doc (Document): 目标文档。
        after (float): 段后间距，单位磅。

    Returns:
        None: 空段已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> spacer(doc)
        >>> doc.paragraphs[-1].text
        ''
    """
    paragraph = doc.add_paragraph()
    tighten(paragraph, after=after)


def steps(doc: Document, items: list[str], start: int = 1) -> None:
    """写编号步骤。

    Args:
        doc (Document): 目标文档。
        items (list[str]): 按顺序的步骤，不含编号。
        start (int): 第一条的编号，从 1 起。

    Returns:
        None: 步骤已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> steps(doc, ["打开程序"], start=2)
        >>> doc.paragraphs[-1].text.startswith("2")
        True
    """
    for index, text in enumerate(items, start=start):
        paragraph = doc.add_paragraph()
        tighten(paragraph, after=5)
        paragraph.paragraph_format.left_indent = Cm(0.85)
        paragraph.paragraph_format.first_line_indent = Cm(-0.85)
        add_runs(paragraph, [
            (f"{index}    ", {"size": 11, "bold": True, "color": BLUE}),
            (text, {"size": 11, "color": INK}),
        ])


def figure(doc: Document, number: int, caption: str) -> None:
    """插入一张截图占位和客户能看懂的图注。

    Args:
        doc (Document): 目标文档。
        number (int): 图号，从 1 起。
        caption (str): 图注。其中说明红框该框住什么。

    Returns:
        None: 占位和题注已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> figure(doc, 1, "红框标出齿轮")
        >>> "在此粘贴截图" in doc.tables[-1].cell(0, 0).text
        True
    """
    table = doc.add_table(rows=1, cols=1)
    table.autofit = False
    table.columns[0].width = CONTENT_WIDTH
    cell = table.cell(0, 0)
    shade_cell(cell, "F4F5F7")
    border_cell(cell, color="C9CED6", size="8")
    set_cell_margins(cell, 140)
    tr_pr = table.rows[0]._tr.get_or_add_trPr()
    height = OxmlElement("w:trHeight")
    height.set(qn("w:val"), "1700")
    height.set(qn("w:hRule"), "atLeast")
    tr_pr.append(height)
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tighten(paragraph, before=10, after=2)
    font_run(paragraph.add_run("在此粘贴截图"), 11, color="8A8F95")
    caption_p = doc.add_paragraph()
    caption_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tighten(caption_p, before=3, after=12)
    font_run(caption_p.add_run(f"图 {number}　{caption}"), 9.5, color=MUTED)


def grid(doc: Document, headers: list[str], rows: list[list[str]], widths: list[float] | None = None) -> None:
    """写一张带表头的表。

    Args:
        doc (Document): 目标文档。
        headers (list[str]): 表头。
        rows (list[list[str]]): 数据行。
        widths (list[float] | None): 各列宽度，单位厘米。省略时均分。

    Returns:
        None: 表格已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> grid(doc, ["符号", "日期"], [["D", "运行当天"]])
        >>> doc.tables[-1].cell(1, 0).text
        'D'
    """
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.autofit = False
    table.style = "Table Grid"
    if widths is None:
        widths = [17 / len(headers)] * len(headers)
    for index, width in enumerate(widths):
        table.columns[index].width = Cm(width)
    for index, header in enumerate(headers):
        cell = table.cell(0, index)
        shade_cell(cell, BLUE)
        set_cell_margins(cell, 70)
        cell.text = ""
        paragraph = cell.paragraphs[0]
        tighten(paragraph, after=0)
        font_run(paragraph.add_run(header), 10.5, bold=True, color="FFFFFF")
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            cell = table.cell(row_index + 1, col_index)
            if row_index % 2 == 1:
                shade_cell(cell, "F7F8FA")
            set_cell_margins(cell, 70)
            cell.text = ""
            paragraph = cell.paragraphs[0]
            tighten(paragraph, after=0)
            font_run(paragraph.add_run(value), 10.5, color=INK)
    spacer(doc, 8)


def page_field(paragraph) -> None:
    """在段落中插入当前页码。

    Args:
        paragraph: 页脚段落。

    Returns:
        None: 页码域已插入。

    Examples:
        >>> from docx import Document
        >>> paragraph = Document().add_paragraph()
        >>> page_field(paragraph)
        >>> "PAGE" in paragraph._p.xml
        True
    """
    run = paragraph.add_run()
    font_run(run, 9, color="8A8F95")
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(end)


def heading(doc: Document, text: str, level: int) -> None:
    """写标题。

    Args:
        doc (Document): 目标文档。
        text (str): 标题文字。
        level (int): 1 或 2。

    Returns:
        None: 标题已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> heading(doc, "第一次打开", 1)
        >>> doc.paragraphs[-1].text
        '第一次打开'
    """
    paragraph = doc.add_heading(text, level=level)
    tighten(paragraph, before=0 if level == 1 else 12, after=8)
    for run in paragraph.runs:
        font_run(run, 18 if level == 1 else 14, bold=True, color=BLUE if level == 1 else INK)


def configure(doc: Document) -> None:
    """设置纸张、页眉页脚和默认字体。

    Args:
        doc (Document): 目标文档。

    Returns:
        None: 文档版式已设置。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> configure(doc)
        >>> abs(doc.sections[0].page_width.cm - 21) < 0.01
        True
    """
    font_style(doc.styles["Normal"], 11)
    font_style(doc.styles["Heading 1"], 18, bold=True, color=BLUE)
    font_style(doc.styles["Heading 2"], 14, bold=True, color=INK)
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.left_margin = Cm(2.0)
    section.right_margin = Cm(2.0)
    section.top_margin = Cm(1.9)
    section.bottom_margin = Cm(1.7)
    section.header_distance = Cm(0.6)
    section.footer_distance = Cm(0.5)
    section.different_first_page_header_footer = True
    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    tighten(header, after=0)
    font_run(header.add_run("广东电力数据采集　使用说明"), 9, color="8A8F95")
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tighten(footer, after=0)
    page_field(footer)
    props = doc.core_properties
    props.title = "广东电力数据采集系统使用说明"
    props.subject = "业务人员操作手册"
    props.category = "使用说明"
    props.comments = "按当前软件界面编写。截图占位待补。"


def cover(doc: Document) -> None:
    """写封面。

    Args:
        doc (Document): 目标文档。

    Returns:
        None: 封面已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> cover(doc)
        >>> "广东电力数据采集" in doc.paragraphs[-1].text or any(
        ...     "广东电力数据采集" in p.text for p in doc.paragraphs)
        True
    """
    banner = doc.add_table(rows=1, cols=1)
    banner.autofit = False
    banner.columns[0].width = CONTENT_WIDTH
    cell = banner.cell(0, 0)
    shade_cell(cell, BLUE)
    border_cell(cell, color=BLUE)
    set_cell_margins(cell, 220)
    tr_pr = banner.rows[0]._tr.get_or_add_trPr()
    height = OxmlElement("w:trHeight")
    height.set(qn("w:val"), "3200")
    height.set(qn("w:hRule"), "atLeast")
    tr_pr.append(height)
    cell.text = ""
    label = cell.paragraphs[0]
    tighten(label, before=6, after=8)
    font_run(label.add_run("使用说明"), 12, color="D6E2FF")
    title = cell.add_paragraph()
    tighten(title, after=6)
    font_run(title.add_run("广东电力数据采集"), 28, bold=True, color="FFFFFF")
    sub = cell.add_paragraph()
    tighten(sub, after=0)
    font_run(sub.add_run("业务人员操作手册"), 16, color="E8EEFF")

    spacer(doc, 16)
    body(doc, "这份说明和程序、初始数据文件放在同一个文件夹里。", size=12)
    body(doc, "日常只需要会三件事：让任务跑起来，看懂采集的是哪一段日期，到文件中心找到文件。", size=12)
    spacer(doc, 8)
    grid(
        doc,
        ["一起交付的内容", "用来做什么"],
        [
            ["程序「广东电力数据采集」", "打开后按设定时间下载数据"],
            ["初始数据文件（JSON）", "第一次打开时导入，里面已有任务和指标"],
            ["本说明", "按想做的事查阅，不必从头读到尾"],
        ],
        [7.2, 9.8],
    )
    body(doc, "文档日期：2026年11月2日", color=MUTED, size=10.5)
    body(doc, "软件窗口标题：广东电力数据采集。左侧展开后，顶部文字是「电力采集」。", color=MUTED, size=10.5)


def intro(doc: Document) -> None:
    """写使用方式、菜单范围和截图编写说明。

    Args:
        doc (Document): 目标文档。

    Returns:
        None: 引言已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> intro(doc)
        >>> any("第 1 章" in cell.text for table in doc.tables for row in table.rows for cell in row.cells)
        True
    """
    doc.add_page_break()
    heading(doc, "先看这一页", 1)
    body(doc, "按你现在要做的事翻到对应的一章即可。")
    grid(
        doc,
        ["你想做的事", "看哪里"],
        [
            ["刚拿到程序，还是空的", "第 1 章，接着第 2 章"],
            ["跑一个已经配好的任务", "第 2 章"],
            ["新建一条定时任务", "第 3 章"],
            ["看懂 D、M、W，或改采集哪一段日期", "第 4 章"],
            ["找不到下载下来的文件", "第 5 章"],
            ["执行失败，要把日志发给运维", "第 6 章"],
            ["现有指标里没有要的那张表", "附录"],
        ],
        [9.2, 7.8],
    )
    heading(doc, "左侧菜单哪些要打开", 2)
    body(doc, "程序打开后，左侧是一条深色窄栏，只显示图标。点它右边顶栏最左侧的按钮，菜单才会展开文字。")
    grid(
        doc,
        ["菜单", "什么时候打开"],
        [
            ["任务配置", "每天都用。执行任务、改区间、改保存位置"],
            ["文件中心", "去看、打开已经下载的文件"],
            ["执行日志", "只有失败、需要发给运维时才打开"],
            ["指标配置", "查看、修改已有指标和工作流"],
            ["录制工坊", "只有现有指标里没有要的表时，才看附录"],
        ],
        [4.2, 12.8],
    )
    body(doc, "设置不在这五个菜单里。它是左侧最下方的齿轮。")
    draft_note(doc, [
        "灰色方框是截图占位。图注里写了红框该框住哪一个按钮，一张图只框一处。",
        "贴上截图后，删掉方框里的「在此粘贴截图」，并删除本框。",
        "正文按当前界面编写：窗口名「广东电力数据采集」；左侧菜单为任务配置、文件中心、执行日志、指标配置、录制工坊；设置在左侧底部齿轮。",
        "导入数据在「设置 → 关于」。导入后必须先点「取消」，重新打开设置，再改下载目录并保存。同一窗口里立刻保存会把未刷新的旧设置写回去。",
    ])


def chapter_first(doc: Document) -> None:
    """写第一次打开。

    Args:
        doc (Document): 目标文档。

    Returns:
        None: 第 1 章已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> chapter_first(doc)
        >>> any("导入数据" in p.text for p in doc.paragraphs)
        True
    """
    doc.add_page_break()
    heading(doc, "1　第一次打开", 1)
    goal(doc, "把初始数据装进软件，并指定结果文件存在这台电脑的哪个文件夹。")
    steps(doc, [
        "双击「广东电力数据采集」打开程序。",
        "点深色侧边栏右边、顶栏最左侧的按钮，把菜单展开。从上到下是：任务配置、文件中心、执行日志、指标配置、录制工坊。最下方是齿轮。",
        "点齿轮打开设置。设置打开后停在「基础」。",
        "点设置窗口左侧的「关于」。",
        "点「导入数据」，选择一起交付的初始数据文件（JSON）。看到「导入完成」即可。提示里如果带有「失败」或「错误」，先不要继续，把提示原文发给运维。",
        "点右下角「取消」关闭设置。这一次不要点「保存」。导入在上一步已经写入。同一窗口里再点「保存」，会把还没刷新的旧内容写回去，把刚导入的设置盖掉。",
        "再次点齿轮打开设置。在「基础」里看「下载目录」，点「浏览」，选这台电脑上用来放结果的文件夹，例如 D:\\电力采集。初始数据里可能带有别人电脑上的路径，这里要改成自己的。",
        "希望开机后程序自己打开时，把「开机自启动」拨到开。到了任务设定的时间，电脑要开着，本程序也不能退出，否则这一次不会自动跑，过后也不会补跑。",
        "点右下角「保存」，看到「设置已保存」。",
        "点左侧「文件中心」，再点回「任务配置」。这时应能看到已经配好的任务。如果仍是空的，点任务页右上角的刷新按钮，鼠标放上去显示「重新加载本地任务」。",
    ])
    note(doc, [
        "任务配置右上角也有一个「导入」。那是单独的任务文件，不是这次的整包初始数据。第一次安装只用设置里「关于」的「导入数据」。",
        "只在第一次安装时导入。这台电脑上已经改过任务或下载目录之后，不要再次导入同一份文件，否则同名任务和其中的设置会被盖掉。",
        "设置里还有网站信息、结果发送、路由等页面。第一次使用不要改它们。执行时如果提示缺少证书密码或账号密码，再打开「网站信息」填写，然后点「保存」。目标站点保持导入后的地址。",
    ])
    figure(doc, 1, "展开后的左侧菜单。红框只框住最下方的齿轮。")
    figure(doc, 2, "设置窗口的「关于」。红框只框住「导入数据」。")
    figure(doc, 3, "重新打开设置后的「基础」。红框只框住「下载目录」右侧的「浏览」。")


def chapter_run(doc: Document) -> None:
    """写执行已有任务。

    Args:
        doc (Document): 目标文档。

    Returns:
        None: 第 2 章已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> chapter_run(doc)
        >>> any("立即执行" in p.text for p in doc.paragraphs)
        True
    """
    doc.add_page_break()
    heading(doc, "2　执行一个已经配好的任务", 1)
    goal(doc, "手动跑一条任务，并在文件中心看到结果文件。")
    steps(doc, [
        "左侧点「任务配置」。每张卡片是一条任务，上面有「调度」「日期」「存储」「下次执行」。",
        "看卡片左下角。开关打开时显示「已启用」，到了时间会自动执行。开关关掉时，「下次执行」显示「已禁用」，到点不会自动跑；右下角的「立即执行」仍然可以手动跑一次。",
        "点「立即执行」。弹出的窗口标题旁会显示「运行中」，并列出正在做的步骤。",
        "窗口里出现暂停说明时，先读那一行字。若是让你在网页上登录、点查询或处理证书，按平时登录该网站的方式做完，再点「继续」。若是报错，例如证书连接失败，点「停止」。不要反复点「继续」。",
        "「关闭」只关掉进度窗口，采集还在进行。要停掉请点「停止」。",
        "跑完后会弹出结果窗口。标题是「执行完成」或「执行失败」，标题下面有「成功几项，失败几项」。对照下面的表看完，点「确认」。",
    ])
    grid(
        doc,
        ["结果窗口里看到", "表示什么", "接下来"],
        [
            ["执行完成，失败 0 项，文件为「已下载」", "这一次下到了", "去文件中心打开"],
            ["文件为「没数据」", "网站这一天没有这条数据", "不会生成文件，不必当成故障"],
            ["失败在 1 项及以上，或文件为「下载失败」", "有的表没有下下来", "按第 6 章把日志发给运维"],
            ["标题是「执行失败」", "这次没有跑完", "按第 6 章把日志发给运维"],
        ],
        [7.4, 5.2, 4.4],
    )
    steps(doc, [
        "左侧点「文件中心」，按第 5 章的文件夹逐层点进去。点文件名可以打开文件。",
    ], start=7)
    note(doc, [
        "自动执行成功或失败时，窗口右上角会弹出通知。通知只说明结果，文件仍到文件中心查看。",
        "文件中心如果显示「尚未设置下载目录」，回到第 1 章第 7 步，把下载目录设成这台电脑上的文件夹并保存。",
    ])
    figure(doc, 4, "一条任务卡片。红框只框住「立即执行」。")
    figure(doc, 5, "跑完后的结果窗口。红框只框住「成功几项，失败几项」这一行。")


def chapter_create(doc: Document) -> None:
    """写新增任务的三步。

    Args:
        doc (Document): 目标文档。

    Returns:
        None: 第 3 章已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> chapter_create(doc)
        >>> any("采集范围" in p.text for p in doc.paragraphs)
        True
    """
    doc.add_page_break()
    heading(doc, "3　新增一个任务", 1)
    goal(doc, "用已经准备好的指标，新建一条到点自动执行的任务。")
    body(doc, "在任务配置右上角点「新增任务」。窗口分三步：基本信息、采集范围、存储结构。新建时按「下一步」往下走。最后一步点「创建任务」。")
    body(doc, "改已有任务时，点卡片右上角的铅笔图标。三步可以来回点，改完点「保存」。旁边的垃圾桶会删掉这条任务，不会删掉已经下载到文件夹里的文件。")

    heading(doc, "3.1　基本信息", 2)
    steps(doc, [
        "任务名称写成自己认得的话，例如「每天采集现货出清」。",
        "登录方式选择和已经能跑通的任务相同的那一项，不要留空。",
        "采集指标只勾选这次要下载的表。这里是在已有指标里选择，不用新建指标。",
        "执行时间先选「每天」或「每周」，再选具体钟点。选「每周」后勾选星期，至少勾一天。",
        "状态保持「启用」。关掉后，到点不会自动执行。",
        "备注可以不填。确认后点「下一步」。",
    ])
    figure(doc, 6, "新建任务的「基本信息」。红框只框住「采集指标」。")

    heading(doc, "3.2　采集范围", 2)
    body(doc, "这一步决定每次运行时下载哪一段日期。每个指标一行，拖两端的滑块。行尾会写出结果，例如「D-2 到 D，共 3 天」。字母的意思见第 4 章。")
    body(doc, "这里的修改只影响当前任务。窗口上方也写着：不会更改指标配置。")
    note(doc, [
        "多数指标下面没有更多选项。如果出现「按导出另配」，保持「跟随」，这份表就和上面的滑块用同一段日期。只有某一份表要另外的日期时，才改成「单独」。",
    ])
    figure(doc, 7, "「采集范围」里的一条滑块。红框只框住滑块和行尾的「D-2 到 D，共 3 天」。")

    heading(doc, "3.3　存储结构", 2)
    body(doc, "这一步决定文件放在下载目录下面的哪一层。最上面的「根目录」就是第 1 章选的文件夹。左边是可以拖进去的标签，右边是将来的文件夹。")
    steps(doc, [
        "先看「根目录」是不是这台电脑上的文件夹。不是的话，先关掉窗口，按第 1 章改好下载目录并保存，再回来编辑任务。",
        "左边「文件」下列出这次要保存的表。点「剩下 N 个全部放入」，或逐个点进右边。全部放完后，左边显示「都已放入」。",
        "要按年月分文件夹时，在左边「位置」里点 {YYYY-MM}。还要按天再分，就再点 {YYYY-MM-DD}。这些日期是执行任务的那一天，不是表里面的业务日期，例子见第 5 章。",
        "要自己起文件夹名时，点「新文件夹」，在右边双击它，改成例如「现货」。文件夹里面还可以再放文件夹。",
        "如果提示两个同名文件会落在同一目录、互相覆盖，把它们拖进不同的文件夹。",
        "确认后点「创建任务」。编辑已有任务时点「保存」。",
    ])
    note(doc, [
        "左边还有「WiFi 名称」。只有希望不同网络下的文件自动分开时才放进去。放进去以后，文件夹名是执行时电脑连接的 Wi-Fi，窗口右上角的绿色标签能看到当前名称。换了网络，新文件会进另一个文件夹，旧文件仍在原来的位置。",
    ])
    figure(doc, 8, "「存储结构」。红框只框住左边的「剩下 N 个全部放入」。")


def chapter_window(doc: Document) -> None:
    """写区间含义。

    Args:
        doc (Document): 目标文档。

    Returns:
        None: 第 4 章已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> chapter_window(doc)
        >>> any("D-2 到 D" in cell.text for table in doc.tables for row in table.rows for cell in row.cells)
        True
    """
    doc.add_page_break()
    heading(doc, "4　区间是什么意思", 1)
    goal(doc, "看懂滑块上的 D、M、W，并能自己改成要采集的那一段。")
    body(doc, "滑块上的字母指的是「任务运行的那一天」，不是你打开这份说明的日子。下面全部用同一次运行来举例：任务在 2026年11月2日运行。")
    body(doc, "任务卡片上的「日期」一行就是当前各指标的区间。鼠标放上去，可以看到每一项。")

    heading(doc, "4.1　按日，字母 D", 2)
    body(doc, "带「按日」标记的指标用 D。D 是运行当天。减号往前，加号往后。滑块最远从 D-14 到 D+7。")
    grid(
        doc,
        ["滑块上的字", "这次实际是哪一天"],
        [
            ["D-2", "2026-10-31"],
            ["D-1", "2026-11-01"],
            ["D", "2026-11-02，运行当天"],
            ["D+1", "2026-11-03"],
        ],
        [6.0, 11.0],
    )
    grid(
        doc,
        ["滑块选成", "这次会下载"],
        [
            ["D-2 到 D", "10月31日、11月1日、11月2日，共 3 天"],
            ["D-1 到 D-1", "只有 11月1日"],
            ["D 到 D", "只有运行当天 11月2日"],
        ],
        [6.0, 11.0],
    )
    body(doc, "采集已经发生的数据时，右端一般停在 D 或 D-1。")

    heading(doc, "4.2　按月，字母 M", 2)
    body(doc, "带「按月」标记的指标用 M。M 是运行当天所在的整个月，不是单独某一天。每个月查一次整月。滑块最远到 M-12，右端只能到 M，不能选下个月。")
    grid(
        doc,
        ["滑块上的字", "这次实际是哪一段"],
        [
            ["M", "2026年11月整月"],
            ["M-1", "2026年10月整月"],
            ["M-1 到 M", "10月整月和 11月整月，各查一次"],
        ],
        [6.0, 11.0],
    )

    heading(doc, "4.3　按周，字母 W", 2)
    body(doc, "带「按周」标记的指标用 W。W 是运行当天所在的那一周，每周采集一次。W-1 是上一周。滑块最远从 W-12 到 W+4。")
    grid(
        doc,
        ["滑块选成", "这次会采集"],
        [
            ["W 到 W", "2026年11月2日所在的那一周"],
            ["W-1 到 W", "上一周和这一周，各采集一次"],
        ],
        [6.0, 11.0],
    )

    heading(doc, "4.4　临时补某几天", 2)
    body(doc, "错过了自动执行，或只想补某几天时，用任务配置右上角的「补数据」。它只跑一次，不改任务卡片上已经保存的区间。下次到点执行，仍按原来的滑块。")
    steps(doc, [
        "点「补数据」。窗口写着：仅执行一次，不会修改定时任务。",
        "第一步选登录方式和要补的指标，第二步点选具体日期。时间留空时，用该指标原来的采集范围。以下方预览为准。",
        "第三步确认下载路径和存储结构。默认沿用已保存任务的路径，也可以只改这一次，不会写回任务。",
        "点「开始补数」。结束后仍看「执行结果」，文件落在第三步确认的位置。",
    ])


def chapter_files(doc: Document) -> None:
    """写文件位置和文件中心。

    Args:
        doc (Document): 目标文档。

    Returns:
        None: 第 5 章已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> chapter_files(doc)
        >>> any("文件中心" in p.text for p in doc.paragraphs)
        True
    """
    doc.add_page_break()
    heading(doc, "5　文件存在哪里", 1)
    goal(doc, "知道结果落在哪个文件夹，并在文件中心或资源管理器里打开它。")
    body(doc, "文件位置分成两层。")
    grid(
        doc,
        ["叫什么", "在哪里设置", "决定什么"],
        [
            ["下载目录", "设置 → 基础", "这台电脑上的总文件夹"],
            ["存储结构", "新增或编辑任务、以及补数据的第三步", "总文件夹下面再怎么分层"],
        ],
        [3.6, 6.2, 7.2],
    )
    body(doc, "举例：下载目录是 D:\\电力采集，存储结构是「现货 / {YYYY-MM} / 文件」。任务在 2026年11月2日执行，文件在：")
    body(doc, "D:\\电力采集\\现货\\2026-11\\文件名", bold=True)
    note(doc, [
        "{YYYY}、{YYYY-MM}、{YYYY-MM-DD} 用的是执行任务的日期，不是表里的业务日期。",
        "仍用上面的例子：区间选的是 D-1，表里的数据是 11月1日，但 {YYYY-MM-DD} 文件夹仍是 2026-11-02。同一次执行下载的多天，会落在这一次执行对应的那组文件夹里。",
        "任务卡片上的「存储」如果写着「自定义结构（YAML）」，就是用第三步里摆好的文件夹。括号里的英文不用管。",
    ])
    heading(doc, "在文件中心查看", 2)
    steps(doc, [
        "左侧点「文件中心」。这里就是下载目录。顶部一行路径显示现在位于哪一层。",
        "点文件夹名进入下一层，点左上角返回上一级。点文件名打开文件。",
        "要在资源管理器里打开当前这一层：工具栏左起第三个按钮，鼠标放上去显示「在系统中打开当前文件夹」。第一个按钮「目录」用来更换下载目录，和设置里的是同一个位置。",
        "右上角可以按名字搜索。",
    ])
    note(doc, [
        "文件中心里的删除，会删掉电脑磁盘上的文件。还要留档的结果不要从这里删除。",
    ])
    figure(doc, 9, "文件中心。红框只框住工具栏上「在系统中打开当前文件夹」那个按钮。")


def chapter_logs(doc: Document) -> None:
    """写出问题时如何导出日志。

    Args:
        doc (Document): 目标文档。

    Returns:
        None: 第 6 章已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> chapter_logs(doc)
        >>> any("下载日志" in p.text for p in doc.paragraphs)
        True
    """
    doc.add_page_break()
    heading(doc, "6　出问题了", 1)
    goal(doc, "把失败那一次的日志文件交给运维。")
    body(doc, "平时不用打开这一页。执行结果里出现失败，或窗口里是报错暂停时，再按下面做。")
    steps(doc, [
        "左侧点「执行日志」。",
        "在「状态筛选」里选「失败」，再点「筛选」。找到对应的那一行，看名称、开始时间和「原因」。手动点的「立即执行」类型一般是「定时手动」，到点自动跑的是「定时自动」，补数据的类型是「补数据」。",
        "点这一行右侧、鼠标放上去显示「下载日志」的按钮。软件会把一个 txt 文件存到下载目录下的「执行日志」文件夹，并自动打开这个文件夹。",
        "把这个 txt 发给运维或开发。同时写上：哪一条任务、大约什么时间、屏幕上的报错原文。",
    ])
    note(doc, [
        "不要点「上传日志」和「上传诊断包」，除非运维明确要求。不要删除日志。",
        "眼睛图标是「查看详情」，可以在软件里看文字。发给运维时，仍以下载得到的 txt 为准。",
        "指标步骤由维护人员修改。不要在设置的「关于」里点「恢复出厂设置」，那会清空设置、任务和指标。",
    ])
    figure(doc, 10, "执行日志里失败的一行。红框只框住「下载日志」按钮。")


def chapter_workshop(doc: Document) -> None:
    """写录制工坊附录。

    Args:
        doc (Document): 目标文档。

    Returns:
        None: 附录已加入文档。

    Examples:
        >>> from docx import Document
        >>> doc = Document()
        >>> chapter_workshop(doc)
        >>> any("开始构建" in p.text for p in doc.paragraphs)
        True
    """
    doc.add_page_break()
    heading(doc, "附录　只有要新的指标时才看", 1)
    goal(doc, "现有指标里没有要的那张表时，把操作录成一个压缩包发给我们。")
    body(doc, "这一节不是日常操作。录完以后把 ZIP 发给我们，由我们做成新指标。")
    body(doc, "页面标题「录制工坊」旁边有一个问号，点开的说明和下面是同一套步骤。")
    steps(doc, [
        "先确认第 1 章的下载目录已经设好。压缩包会放在这个目录下的「录制工坊」文件夹。",
        "左侧点「录制工坊」。工作流名称填自己认得的话，例如「日清算概览导出」。这个名字会变成 ZIP 的文件名。描述可以写「需要先点查询再导出」。",
        "点「开始构建」。浏览器会打开设置里的网站，右侧出现步骤面板。",
        "鼠标移到要点的位置，元素会高亮。用键盘记下这一步：C 点击，H 悬停，D 日期，S 下拉，F 填入，E 导出，X 提取，W 等待。",
        "录完后，在浏览器旁边的面板里点「完成」。ZIP 保存到「录制工坊」文件夹，浏览器会自动关闭。",
        "在右侧历史记录里找到这一行。文件夹图标放上去显示「在系统中打开」，用它找到 ZIP，把文件发给我们。",
    ])
    note(doc, [
        "历史记录里的云图标放上去显示「提交」。目前以把 ZIP 发给我们为准，不要只点提交就当作已经交来。",
    ])
    figure(doc, 11, "录制工坊。红框只框住「开始构建」。")
    spacer(doc, 4)
    body(doc, "文档版本：2026年11月2日。界面若有改动，以软件上的按钮文字为准，并把对应截图换掉。", color=MUTED, size=10)


def build() -> Path:
    """生成使用说明并返回文件路径。

    Returns:
        Path: 写出的 docx 绝对路径。

    Examples:
        >>> path = build()
        >>> path.name.endswith(".docx")
        True
        >>> path.exists()
        True
    """
    doc = Document()
    configure(doc)
    cover(doc)
    intro(doc)
    chapter_first(doc)
    chapter_run(doc)
    chapter_create(doc)
    chapter_window(doc)
    chapter_files(doc)
    chapter_logs(doc)
    chapter_workshop(doc)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    return OUT


if __name__ == "__main__":
    print(build())
