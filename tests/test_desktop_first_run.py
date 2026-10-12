"""desktop 首跑落地逻辑的单元测试（干净目录可开箱的兜底）。

冻结环境无法在单测里真实构造（FROZEN 为 hub.paths 模块级常量），
故直接测可注入路径的 _seed_user_file 三条语义：
缺失从模板落地、已存在不覆盖、模板缺失不抛错（打日志返回 False）。
"""
from pathlib import Path

import desktop


def test_seed_creates_from_template(tmp_path):
    template = tmp_path / "tpl.txt"
    template.write_text("政治\n赌博\n", encoding="utf-8")
    target = tmp_path / "out" / "blockwords.txt"
    target.parent.mkdir()

    assert desktop._seed_user_file(target, template) is True
    assert target.read_text(encoding="utf-8") == "政治\n赌博\n"


def test_seed_keeps_existing_file(tmp_path):
    template = tmp_path / "tpl.txt"
    template.write_text("模板内容", encoding="utf-8")
    target = tmp_path / "blockwords.txt"
    target.write_text("用户已改过的词库", encoding="utf-8")

    assert desktop._seed_user_file(target, template) is False
    assert target.read_text(encoding="utf-8") == "用户已改过的词库"  # 不覆盖用户数据


def test_seed_missing_template_is_not_fatal(tmp_path):
    target = tmp_path / "blockwords.txt"
    # 模板不存在（打包异常的极端情况）：不抛错、不落地、返回 False
    assert desktop._seed_user_file(target, tmp_path / "nope.txt") is False
    assert not target.exists()


def test_seed_template_roundtrip_keeps_bom(tmp_path):
    """现有 blockwords.txt 带 BOM（utf-8-sig 保存）：模板经 seed 后 BOM 保留。"""
    template = tmp_path / "tpl.txt"
    template.write_bytes("# 敏感词库\n政治\n".encode("utf-8-sig"))
    target = tmp_path / "blockwords.txt"

    desktop._seed_user_file(target, template)
    assert target.read_bytes() == template.read_bytes()
    from hub.safety import load_words

    assert load_words(target) == ["政治"]  # BOM 不影响词条解析
