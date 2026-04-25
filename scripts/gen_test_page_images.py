"""
生成若干张「整页多题 + 解析」风格的测试图片，用于联调 mm-qbank vlm-text / vlm-refine。

用法（在项目根目录）:
  python scripts/gen_test_page_images.py
输出目录默认: data/inbound/sample_pages/
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\msyhbd.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    ]
    for p in candidates:
        if p.is_file():
            return ImageFont.truetype(str(p), size=size)
    return ImageFont.load_default()


def _wrap_paragraph(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for para in text.strip().split("\n"):
        para = para.strip()
        if not para:
            lines.append("")
            continue
        lines.extend(textwrap.wrap(para, width=width) or [""])
    return lines


def _render_page(
    out_path: Path,
    *,
    title: str,
    body_lines: list[str],
    size: tuple[int, int] = (1200, 1700),
    font_size: int = 26,
    margin: int = 48,
    line_gap: float = 1.35,
) -> None:
    font = _load_font(font_size)
    im = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(im)
    y = margin
    x = margin
    line_h = int(font_size * line_gap)

    draw.text((x, y), title, font=font, fill=(20, 20, 20))
    y += line_h + 8

    for line in body_lines:
        draw.text((x, y), line, font=font, fill=(0, 0, 0))
        y += line_h
        if y > size[1] - margin:
            break

    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path, format="PNG")


def main() -> None:
    root = _project_root()
    out_dir = root / "data" / "inbound" / "sample_pages"

    # 页 A：标准「1. 2.」题号 + 文末解析块（含【解析】）
    page_a = """
护理综合测试页 A（样例）

1. 患者，男，68 岁，因慢性心力衰竭入院。护士测量心率时应首选？
A. 颈动脉
B. 桡动脉
C. 足背动脉
D. 肱动脉

2. 预防压疮时，为卧床患者翻身间隔一般不超过多少小时？
A. 1 小时
B. 2 小时
C. 3 小时
D. 4 小时

【解析】
第 1 题：心力衰竭患者测心率常用桡动脉，操作方便且安全。答案：B。
第 2 题：长期卧床者应定时翻身，一般每 2 小时翻身一次，必要时缩短间隔。答案：B。
""".strip()

    # 页 B：带「（1）（2）」样式题号（与 default.yaml 中 segment 模式之一匹配）
    page_b = """
护理综合测试页 B（括号题号样例）

（1）静脉输液中发现液体不滴，挤压输液管有阻力，松手后无回血，最可能的原因是？
A. 针头斜面紧贴血管壁
B. 压力过低
C. 静脉痉挛
D. 针头阻塞

（2）测量血压时，袖带过紧会导致测量值如何变化？
A. 偏高
B. 偏低
C. 无影响
D. 波动增大

解析：
（1）有阻力且无回血，多见于针头阻塞或折叠。答案：D。
（2）袖带过紧使血管在未充气前已受压，听诊柯氏音出现偏晚，测得值偏低。答案：B。
""".strip()

    # 页 C：题与解析分栏感（上为题、下为解析，仍是一页）
    page_c = """
护理综合测试页 C（题解析同页）

3. 糖尿病酮症酸中毒患者呼吸的典型表现为？
A. 潮式呼吸
B. 毕奥呼吸
C. 库斯莫尔呼吸
D. 浅快呼吸

4. 无菌操作中发现手套破损，护士应首先？
A. 用胶布粘贴破损处
B. 立即更换手套
C. 继续操作后处理
D. 用消毒液擦拭

参考答案与解析：
第 3 题：酮症酸中毒常出现深大而快的库斯莫尔呼吸。答案：C。
第 4 题：手套破损应立即更换，防止污染。答案：B。
""".strip()

    w = 34
    _render_page(
        out_dir / "sample_page_a.png",
        title="样例试卷 A",
        body_lines=_wrap_paragraph(page_a, w),
    )
    _render_page(
        out_dir / "sample_page_b.png",
        title="样例试卷 B",
        body_lines=_wrap_paragraph(page_b, w),
    )
    _render_page(
        out_dir / "sample_page_c.png",
        title="样例试卷 C",
        body_lines=_wrap_paragraph(page_c, w),
    )

    print(f"已生成 3 张 PNG：{out_dir}")


if __name__ == "__main__":
    main()
