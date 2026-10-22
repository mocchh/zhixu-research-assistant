import base64

import pytest

from backend.charts import render_csv


def test_csv_renders_actual_data_and_descriptive_caption():
    result = render_csv("\ufeff轮次;准确率;损失\n1;0.5;2\n2;0.7;\n3;0.6;1\n".encode("utf-8"), y="准确率,损失")
    assert result["rows"] == 3
    assert result["columns"] == ["轮次", "准确率", "损失"]
    assert "<svg" in result["svg"]
    assert base64.b64decode(result["png_base64"]).startswith(b"\x89PNG")
    assert "从 0.5 到 0.6" in result["caption"]
    assert "最小值 0.5，最大值 0.7" in result["caption"]
    assert any("缺失 1" in warning for warning in result["warnings"])


@pytest.mark.parametrize("content,kwargs", [
    (b"x,y\n1,Infinity\n", {}),
    (b"x,y\n1,NaN\n", {}),
    (b"x,y\n1,2\n", {"y": "missing"}),
    (b"x,y\na,text\n", {"y": "y"}),
    (b"x,y\na,1\n", {"kind": "scatter"}),
    (b"x,x\n1,2\n", {}),
    (b"x,y\n" + b"1,2\n" * 10001, {}),
    (b"a" * (2 * 1024 * 1024 + 1), {}),
])
def test_csv_rejects_invalid_columns_numbers_and_resource_limits(content, kwargs):
    with pytest.raises(ValueError):
        render_csv(content, **kwargs)


def test_gb18030_tsv_uses_selected_column_and_preserves_literal_labels():
    result = render_csv("项目\t得分\t备注\n甲\t2\t普通文本\n乙\t3\t=do_not_execute()\n".encode("gb18030"), kind="bar", y="得分")
    assert result["numeric_columns"] == ["得分"]
    assert result["preview"][1]["备注"] == "=do_not_execute()"
    assert "GB18030" in result["warnings"][0]
    assert result["png_base64"]
