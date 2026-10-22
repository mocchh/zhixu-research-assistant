# 独立构题：检索检验 v2

本题集由 AI 辅助核对原 PDF 后构建，**不是人工盲测**。构题与检索改进由不同代理负责。实现代理在冻结代码前不会收到新题、文献范围、来源目标或基线分项结果；构题代理没有查看并行改进代码，也没有用新题调参。

## 内容与来源核对

- 4 篇 PDF、共 158 个物理页，均未进入原 31 题或后续 22 题的文献集合。
- 17 题：公式 3、数字 4、表格 5、机制 1、跨论文 2、缺失版本 1、缺失原论文 1。
- 19 个来源目标，展开 `all_of` 后为 22 个片段。物理页码从 1 开始。
- 先直接读取原 PDF，核对页码、精确文本及表格列名、单位、实验条件，再检查解析后的片段是否可用。冻结前未运行检索。
- 已看图核对 ZeRO 第 11 页、T5 第 24 页、Switch 第 6/9/10 页、DPO 第 9/20 页。相关原始文本、页图及解析片段保存在 `test-results/unseen-v2-baseline/`。

题目中的 `answer_outline` 是构题时核对的事实说明，评分程序不会用它生成回答。金标是 `expected` 中的文件、物理页和原文片段。对部分表格使用 `all_of`，要求同时找到列头和数据行；找到其他正确但未列入金标的段落仍可能计为未命中。测试不衡量模型回答正确性、公式理解、表格推理或事实蕴含。

## 冻结记录

- 题集：`retrieval_unseen_v2.json`
- 冻结清单：`retrieval_unseen_v2.freeze.json`
- 首次改前报告：`retrieval_unseen_v2.baseline.json`
- 题集 SHA256：`b19c1087592b9dd5ddae5165a92cb93dcb13ec392aec0cc3b512156bfb5d9c4d`
- 改前检索实现 SHA256：`ff65131c1acf06b194d9565e15a1e5fa213028038555a8b7cd62f6843a85f80f`
- 独立快照：`test-results/unseen-v2-baseline/intelligence.py`。与实现代理另存的改前快照摘要一致。

`scripts/evaluate_retrieval_snapshot.py` 在导入评估程序前，将指定快照加载为 `backend.intelligence`，因此改前运行不依赖正在改动的工作区检索代码。它会验证快照摘要，并拒绝覆盖已存在的输出报告。PDF 原件、旧题集、旧金标、工作区 `data/` 均未修改。

## 首次改前结果

| 指标 | 结果 |
| --- | --- |
| 通过题数 | 9 / 17 |
| Recall@8 | 10 / 19 = 52.63% |
| 解析后金标可用 | 19 / 19 |
| 原文定位字段完整性 | 100% |
| 文献范围/版本泄漏 | 0 |
| 缺资料拒答行为 | 1 / 2 |

这次基线的 8 道失败题原样保留。基线报告返回码为 1，表示存在真实未通过项。后续 `--baseline` 回归检查若通过，只表示没有丢失原来已通过的来源目标，不能写成所有题通过。

## 验证命令

在项目根目录核对题集、原 PDF 摘录和冻结摘要；此命令不运行检索：

```bash
.venv/bin/python scripts/validate_retrieval_unseen_v2.py \
  --freeze-file tests/fixtures/retrieval_unseen_v2.freeze.json
```

复现改前实现时另存报告，不覆盖首次记录：

```bash
.venv/bin/python scripts/evaluate_retrieval_snapshot.py \
  --retriever-snapshot test-results/unseen-v2-baseline/intelligence.py \
  --expected-retriever-sha256 ff65131c1acf06b194d9565e15a1e5fa213028038555a8b7cd62f6843a85f80f \
  --fixture tests/fixtures/retrieval_unseen_v2.json \
  --freeze-file tests/fixtures/retrieval_unseen_v2.freeze.json \
  --cache-dir test-results/unseen-v2-baseline/parsed-cache \
  --output test-results/unseen-v2-baseline/reproduction.json
```

首次改后评估由主代理在实现冻结后安排，应另存实现摘要、冻结清单和运行报告，继续使用同一份题集与 PDF。不要覆盖上面的改前冻结清单。运行后新题即成为已知回归集；若之后根据这些失败调整实现，后续分数应注明为同题回归，不能继续宣称为首次未见题验证。

## 已完成的首次改后验证

2026-10-02 18:12:21 UTC，主代理确认实现冻结后，隔离加载
`test-results/retrieval-v2-frozen.py` 执行了 1 次实际改后评估。

- 实现 SHA256：`79dbfa27ec461caa6d1fcab127f9263beff16270fe3d49a297c947a6e6c98ad9`
- 解析器 SHA256：`ea63f1f3d28c618d8eccad4e3afcade5f4d54d41250a1caefce29facf3523b2c`，与改前相同。
- 题集与原 PDF 摘要保持不变；未修改金标，也未向实现代理披露新题、原文目标或失败项用于调整。
- 改后冻结记录：`test-results/retrieval-unseen-v2-first-after.freeze.json`
- 完整结果：`test-results/retrieval-unseen-v2-first-after.json`
- 结果 SHA256：`a769cdd41bfc9f605e152c788091505bd3e0c9b9530c3b39a1c583d5051f5932`
- 比较记录：`test-results/retrieval-unseen-v2-comparison.json`

| 指标 | 改前 | 首次改后 |
| --- | --- | --- |
| 通过题数 | 9 / 17 | 10 / 17 |
| Recall@8 | 10 / 19 = 52.63% | 9 / 19 = 47.37% |
| 解析后金标可用 | 19 / 19 | 19 / 19 |
| 原文定位字段完整性 | 100% | 100% |
| 文献范围/版本泄漏 | 0 | 0 |
| 缺资料拒答行为 | 1 / 2 | 1 / 2 |

原先通过的题目没有回退，新增通过 1 题；部分仍失败题的证据命中减少，使总召回率下降。
因此，这组结果不能支持整体泛化质量提升的结论。此次仍有 7 题未通过，程序正常返回 1。
这里的来源核对由 AI 辅助完成，不是人工盲测；结果衡量前 8 条检索证据，不衡量模型回答、数学语义或 OCR 质量。

审计记录：首次命令误用了改前实现的冻结清单，被摘要检查拒绝并返回 `not_evaluated`；该调用未加载文档、未运行题目，也没有生成结果文件。随后另存改后冻结清单，执行了上面唯一一次实际评估。

## 最终解析器整合诊断

`test-results/retrieval-unseen-v2-final-integrated.json` 使用最终 `layout-2026-10-03.1` 解析器，严格评分为 8/17、7/19 目标命中、14/19 金标可用，基线回归失败。新表格文本使用 `|` 分隔单元格，5 个原来的精确短语因此失配；AI 逐页复核数字仍在，其中4个目标表已进入返回结果。此前发现的数值表行合并问题已通过通用行恢复规则和合成测试修复。诊断见 `test-results/retrieval-unseen-v2-parser-review.json` 及 `test-results/parser-v2-draft/zero-rows-fixed.json`。

金标、评测字符串规则均未调整；首轮旧解析器结果仍单独保留。检索实现除 graph 委托到引用模块之外与冻结版本 AST 相同（`test-results/retrieval-v2-final-integrity.json`）；本轮未按新题调整检索器。仍需改进真实漏召回，并在未来用另行冻结的、考虑表格单元格结构的评测协议验证；不能把本轮诊断直接替代首次分数。
