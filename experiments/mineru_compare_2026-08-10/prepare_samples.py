"""实验样本准备模块：从原始数学教材中截取可重复的对照页段。"""

from pathlib import Path

from pypdf import PdfReader, PdfWriter


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = Path(__file__).resolve().parent / "input"

# 选择覆盖扫描公式、统计表格、立体几何和彩色混合教材四类版面的样本。
SAMPLES = [
    {
        "sample_id": "scan_formula",
        "source": "fudan_shufen_textbook_1.pdf",
        "start_page": 188,
        "end_page": 195,
        "description": "纯扫描数学分析页，包含 Taylor 公式和上下标",
    },
    {
        "sample_id": "probability_table",
        "source": "概率论与数理统计 第五版 (盛骤 , 谢式千 , 潘承毅) (z-library.sk, 1lib.sk, z-lib.sk).pdf",
        "start_page": 219,
        "end_page": 226,
        "description": "纯扫描概率统计页，包含概率公式和统计表格",
    },
    {
        "sample_id": "highschool_geometry",
        "source": "普通高中教科书·数学（A版）必修 第二册.pdf",
        "start_page": 132,
        "end_page": 139,
        "description": "高中立体几何页，包含彩色几何图和练习题",
    },
    {
        "sample_id": "hybrid_middle_school",
        "source": "（根据2022年版课程标准修订）义务教育教科书·数学八年级下册.pdf",
        "start_page": 130,
        "end_page": 137,
        "description": "混合型初中教材页，覆盖正文、公式和图形",
    },
]


def extract_range(sample: dict[str, object]) -> dict[str, object]:
    """将原始 PDF 的指定页段拆成独立 PDF，并写入页码映射信息。"""
    source = ROOT / "math_text" / str(sample["source"])
    output = OUTPUT_DIR / f"{sample['sample_id']}.pdf"
    reader = PdfReader(str(source), strict=False)
    writer = PdfWriter()
    start = int(sample["start_page"])
    end = int(sample["end_page"])
    for page_number in range(start, end + 1):
        writer.add_page(reader.pages[page_number - 1])
    with output.open("wb") as stream:
        writer.write(stream)
    return {
        **sample,
        "source_path": str(source),
        "sample_path": str(output),
        "page_count": end - start + 1,
    }


def main() -> None:
    """生成全部样本，并输出给后续 API 实验使用的清单。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = [extract_range(sample) for sample in SAMPLES]
    import json

    manifest_path = OUTPUT_DIR.parent / "sample_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(manifest_path)
    for item in manifest:
        print(f"{item['sample_id']}: {item['sample_path']} ({item['page_count']} pages)")


if __name__ == "__main__":
    main()
