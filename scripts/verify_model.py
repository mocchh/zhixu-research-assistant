#!/usr/bin/env python3
"""Small real-provider integration check; uses the saved key without printing it.

This explicitly calls the configured model (up to four requests) and is never
part of offline check.sh / CI. It does not grade semantic quality automatically.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import intelligence
from backend.answer_validation import citation_markers
from backend.config import ModelConfig
from backend.storage import Store
from backend.translation_routes import source_segments, translate_segments


async def verify(data, answers_only=False):
    config = ModelConfig(data)
    settings = config.get()
    intelligence._model_configuration(settings)
    store = Store(data)
    papers = store.papers()
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "model": config.public()["model"],
              "service_url": config.public()["base_url"], "checks": [],
              "limits": ["真实服务少量集成请求；不是系统性准确率测试，也不是独立专家验收。",
                         "来源字段与原文编号可自动检查，翻译和回答的语义需另行核对。",
                         "客户端仅记录耗时；当前兼容接口封装未采集实际token用量或费用。"]}
    specifications = [
        ("1706.03762v7.pdf", "Transformer 论文训练使用几块什么型号的 GPU，基础模型训练多久？请按原文报告单位，引用来源。"),
        ("2412.19437v1.pdf", "DeepSeek-V3 总参数量和每个 token 激活的参数量分别是多少？请保留原文 B 单位并引用来源。"),
    ]
    for filename, question in specifications:
        paper = next((p for p in papers if p["filename"] == filename and p["status"] == "ready"), None)
        if not paper:
            report["checks"].append({"kind": "answer", "filename": filename, "passed": False, "error": "所需论文未完整入库"})
            continue
        blocks = store.blocks([paper["id"]]); original = {b["id"]: b for b in blocks}
        started = time.perf_counter()
        result = await intelligence.answer_question(question, [paper], blocks, [], settings)
        identifiers = [number for _, _, group in citation_markers(result["answer"]) for number in group]
        checks = {"model_response": result["mode"] == "model", "nonempty_answer": bool(result["answer"].strip()),
                  "valid_citations": bool(identifiers) and all(1 <= i <= len(result["sources"]) for i in identifiers),
                  "source_integrity": bool(result["sources"]) and all(s["text"] == original[s["id"]]["text"] and s["bbox"] == original[s["id"]]["bbox"] for s in result["sources"])}
        report["checks"].append({"kind": "answer", "filename": filename, "question": question,
                                 "seconds": round(time.perf_counter() - started, 2), "passed": all(checks.values()),
                                 "checks": checks, "result": result})
    paper = next((p for p in papers if p["filename"] == "1706.03762v7.pdf"), None)
    if paper and not answers_only:
        block = next(b for b in store.blocks([paper["id"]]) if 'P100' in b['text'] and len(b['text']) < 2000)
        started = time.perf_counter()
        translation = await intelligence.translate_text(block['text'], 'zh', settings)
        report['checks'].append({'kind':'paragraph_translation','source':block,'translation':translation,
                                 'seconds':round(time.perf_counter()-started,2),'passed':bool(translation.strip()),'semantic_quality_verified':False})
        segments = source_segments({'page':block['page'],'blocks':[block]})
        started = time.perf_counter()
        translations = await translate_segments(segments, 'zh', settings)
        report['checks'].append({'kind':'aligned_translation','segments':[{**s,'translation':translations[s['id']]} for s in segments],
                                 'seconds':round(time.perf_counter()-started,2),'passed':set(translations)=={s['id'] for s in segments},'semantic_quality_verified':False})
    report['requested_checks'] = 2 if answers_only else 4
    report['integration_passed'] = len(report['checks']) == report['requested_checks'] and all(check['passed'] for check in report['checks'])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=ROOT/'data')
    parser.add_argument('--output', type=Path, default=ROOT/'test-results/model-integration.json')
    parser.add_argument('--preview', action='store_true', help='Save the exact proposed messages locally without contacting the provider')
    parser.add_argument('--approved-preview', type=Path, help='Send only messages identical to this reviewed preview; fail before any changed request')
    parser.add_argument('--answers-only', action='store_true', help='Only the two cited-answer checks; skip both translation requests')
    options = parser.parse_args()
    proposed = []
    if options.approved_preview and not options.preview:
        approved = json.loads(options.approved_preview.read_text())
        generate = intelligence._generate
        position = 0
        async def checked_generate(messages, settings, max_tokens=2200):
            nonlocal position
            expected = approved.get('proposed_requests', [])
            current = {'model':settings['model'],'messages':messages,'max_tokens':max_tokens}
            if (settings['base_url'] != approved.get('destination') or position >= len(expected) or current != expected[position]):
                raise intelligence.ConfigError('验收请求或目标已不同于授权预览，未发送该请求。请重新生成并核对预览。')
            position += 1
            return await generate(messages, settings, max_tokens=max_tokens)
        intelligence._generate = checked_generate
    if options.preview:
        async def capture(messages, settings, max_tokens=2200):
            proposed.append({'model':settings['model'],'messages':messages,'max_tokens':max_tokens})
            source = messages[-1]['content']
            try:
                segments = json.loads(source)
            except ValueError:
                segments = None
            if isinstance(segments, list):
                return json.dumps({'translations':[{'id':s['id'],'text':'PREVIEW ONLY: '+s['source']} for s in segments]})
            return 'PREVIEW ONLY [1]: '+source[:100]
        intelligence._generate = capture
    try:
        report = asyncio.run(verify(options.data.resolve(), options.answers_only))
    except (intelligence.ConfigError, intelligence.UpstreamError) as exc:
        report = {'integration_passed':False,'error':str(exc)}
    if options.preview:
        report = {'preview_only':True,'network_requests_sent':0,'destination':report.get('service_url'),
                  'model':report.get('model'),'proposed_requests':proposed,
                  'request_count':len(proposed),'message_characters':sum(len(m['content']) for r in proposed for m in r['messages']),
                  'papers':['Attention Is All You Need (1706.03762v7)', 'DeepSeek-V3 Technical Report (2412.19437v1)'],
                  'secret_included_in_preview':False,'integration_passed':False}
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'report':str(options.output),'integration_passed':report['integration_passed'],
                      'checks':[{'kind':c['kind'],'passed':c['passed'],'seconds':c['seconds']} for c in report.get('checks',[])],
                      'error':report.get('error')},ensure_ascii=False))
    return 0 if report['integration_passed'] or options.preview else 1


if __name__ == '__main__':
    raise SystemExit(main())
