# 本地科研助手接口

本地单用户应用。API 前缀 `/api`，业务错误为 `{detail: string}`；请求字段校验失败返回 422，`detail` 为校验错误数组。前端 Vite 将 `/api` 代理至 `http://127.0.0.1:8000`；生产构建由 FastAPI 提供静态资源。

## 数据

Paper: `{id, title, filename, authors: string[], abstract, arxiv_id: string|null, version: number|null, family_id, page_count, processed_pages, status: 'queued'|'processing'|'paused'|'cancelled'|'ready'|'partial'|'error', error: string|null, created_at, tags: string[], is_favorite: boolean, duplicate_of: string|null, warnings: string[], source: 'upload'|'sample'|'arxiv', text_chars, ocr_pages, parser_version}`。`paused/cancelled` 描述解析任务；后台翻译状态保存在 Job，不改变文献的解析状态。

Block: `{id, paper_id, page: number, text, kind: 'heading'|'paragraph'|'table'|'formula'|'reference', bbox: [x0,y0,x1,y1], section, order_index}`。页码 1 起，bbox 为旋转与裁剪后的显示页 PDF 点坐标，与渲染图一致。

Evidence: `{id, paper_id, paper_title, page, text, bbox, score}`；检索来源另含 filename、arxiv_id、version、authors、year/year_basis 与 locator 定位快照。答案标记 `[1]` 对应 sources[0]。

模型问答可额外返回 `citation_checks`，其中 `method:'lexical_rules'`、`semantic_entailment_verified:false`。`version:'claim-local-v4.1'` 按就近引用分句检查，支持 B/M/T/K、组合编号与 Markdown 列表。claims 包含 numbers/quantities/quotes/source_indices、matched_source_indices、joint_literal_source_indices、risk_codes，另有 citation_binding 和逐来源 relation_checks。关系检查核对数量附近是否出现基线、对照、消融等明确角色，以及稳定/发散及其明确否定；`relation_unverified` 表示当前片段未提供所需关系支持，不等于证明该陈述错误。warnings 最多包含 3 条具体诊断及额外风险数量，另附规则不验证语义的说明。

`/chat` 通过单次模型请求取得 `{claims:[{text,source_indices}],insufficient_evidence:boolean}`，每条只表达一个事实，最多 8 条、每条 600 字、1 至 3 个来源。来源标号由本地程序追加。事实条目不接受反引号代码标记、波浪号代码围栏或 Unicode 换行。每个结构化条目独立核验，并在诊断中附 `structured_claim_index`；非空条目没有产生可核对诊断时，记录 `unreviewed_claim_indices` 并降级，不能因格式被跳过而视为通过。结构/编号无效、字面证据缺失、跨来源拼接或关系未获支持等情况会返回 `mode:'extractive'`，并明确说明模型草稿未通过局部检查；没有自动付费重试。部分资料不足时保留已有支持的条目并显示固定不足说明。研究写作仍用原有自由格式。

模型已返回草稿的问答另含 `answer_audit: {status:'checked'|'fallback'|'insufficient',raw_response:string,draft:string,issues:string[],citation_checks:object,semantic_entailment_verified:false}`。`checked` 仅指通过本地规则。`raw_response` 保留完整返回，`draft` 为可读草稿；失败草稿可以在界面展开核对。客户端保存 ChatMessage 时须一并保存该字段；旧消息没有此字段仍可读取。任何状态都不构成语义正确性证明。

Job: `{id, kind:'parse'|'translate', status:'queued'|'processing'|'pause_requested'|'paused'|'cancel_requested'|'cancelled'|'done'|'error', progress: number 0..100, message, paper_id: string|null, created_at, updated_at, options: object, result: object, last_page: number}`。`options` 保存任务创建参数；翻译为 `{target_language,start_page,end_page}`，解析通常为 `{}`。`last_page` 是已保存的完整页检查点，初始为 0。翻译 `result` 可包含 `{target_language,start_page,end_page,translated_pages,current_page,completed_chunks,total_chunks}`；页内分块进度与完整页检查点分别记录。

## 接口

- GET `/health` -> `{status, model_configured, model, parser, parser_version, ocr_available}`
- GET `/stats` -> `{papers, ready, pages, processing, families}`
- GET `/papers?q=&status=&favorite=` -> `{papers: Paper[]}`
- POST `/papers/upload` multipart `files` 可多份 -> ImportResult（见下方批量导入说明）
- GET `/samples` -> `{files: {filename,size}[]}`
- POST `/samples/import` `{filenames?: string[]}`（省略导入默认示例）-> ImportResult
- GET `/papers/{id}` -> `{paper: Paper, sections: {title,page,block_id,level}[], versions: Paper[]}`
- PATCH `/papers/{id}` `{title?, is_favorite?, tags?}` -> Paper
- DELETE `/papers/{id}` -> `{ok:true}`（仅删除应用管理的副本，不删除项目原始文件）
- POST `/papers/{id}/retry` -> `{job_id}`，重新解析并复用已完成页。已有排队、运行、暂停或正在暂停/取消的解析任务时返回 409；暂停任务通过 `/jobs/{job_id}/resume` 继续。
- POST `/papers/{id}/reparse` → `{job_id}`。从首页重建派生页面/文本块，保留文献 ID、原 PDF、自定义标题/标签、笔记、会话和阅读记录；存在活动或暂停的解析/翻译任务时返回 409。旧缓存仅在原文指纹仍匹配时可复用。解析器版本变化时，恢复中的旧检查点也从首页重建，避免混合新旧解析。
- GET `/papers/{id}/file` -> 原始 PDF
- GET `/papers/{id}/pages/{page}` -> `{page,width,height,blocks: Block[],translation: string|null}`
- GET `/papers/{id}/pages/{page}/image?scale=1.5` -> PNG
- GET `/papers/{id}/text` -> 纯文本下载
- GET `/jobs?limit=20&cursor=...` -> `{jobs: Job[],active_jobs:Job[],history_jobs:Job[],next_cursor:string|null,history_total:number}`
- POST `/chat` `{question,paper_ids: string[],history?:{role:'user'|'assistant',content:string}[]}` -> `{answer, sources: Evidence[], mode:'model'|'extractive', warnings:string[], answer_audit?:AnswerAudit}`。不配置模型或生成结果未通过检查时可返回原文摘录；原因会在文字中说明。
- POST `/translate` `{paper_id,page?,text?,target_language?:'zh'|'en'}` -> `{translation,mode:'model',cached:boolean}`。未配置模型返回 503。page 用于缓存/同步；任意 text 不当作全文翻译。整页结果保存时在同一事务内检查原文指纹；生成期间原文改变或文献删除则返回 409，不覆盖新缓存。
- POST `/papers/{id}/translate` FullTranslation -> `{job_id}`。兼容接口，和下述 `/translation-jobs` 共用范围校验、任务去重与分块缓存。
- GET `/graph` -> `{nodes:{id,title,year?,citations:number}[],edges:{source,target,confidence:number,evidence:CitationEvidence[]}[],scope:string}`。
- POST `/research` `{paper_ids:string[],task:'summary'|'review'|'ideas'|'outline',idea?:string}` -> `{answer,sources:Evidence[],mode:'model'|'extractive',warnings:string[]}`。
- GET `/discover?q=...&limit=8` -> `{papers:{arxiv_id,title,authors:string[],abstract,published,url,pdf_url}[]}`
- POST `/discover/import` `{arxiv_id}` -> `{papers:Paper[],duplicates:object[]}`
- GET `/settings` -> `{model,base_url,has_api_key,timeout_seconds}`（永不返回密钥）
- PUT `/settings` `{model,base_url,api_key?:string,clear_api_key?:boolean}` -> 同GET。同一服务地址下，api_key 省略或空串保留已有密钥；更换协议、主机、有效端口或路径时必须显式提供新密钥，否则保存为空。仅主机大小写、默认端口或末尾斜线变化视为同址。`clear_api_key:true` 清除密钥，不能同时提供非空新密钥。清除后的空值覆盖环境变量，重启不会重新启用旧密钥。
- POST `/settings/test` -> `{ok,message}`
- POST `/charts` multipart `file` (CSV ≤2MiB), `kind` (line/bar), 可选 `x` 列名、`y` 逗号分隔最多3列 -> `{columns:string[],numeric_columns:string[],rows:number,preview:object[],svg:string,png_base64:string,caption:string,warnings:string[]}`。生成图表不依赖语言模型。

所有写操作完成后刷新列表/详情；`queued/processing/pause_requested/cancel_requested` 时轮询 jobs 与 papers，间隔约 2 秒。仅显示有实际后端对应的操作，不用模拟数据伪造结果。

### 批量导入结果

ImportResult: `{papers:Paper[],duplicates:{filename,paper_id,title}[],errors:{filename,status,detail}[],status:'ok'|'partial'|'failed',detail?:string}`。逐文件处理；成功与重复计为已处理，失败文件不会阻止后续文件。全部成功返回 200，部分成功返回 207；全部失败返回对应的 4xx/500，并保留 errors 和汇总 detail。请求整体不合法（如超过数量限制）仍返回普通业务错误。

文献记录与初始解析任务在同一 SQLite 事务中创建。事务失败时回滚并清理上传副本；事务成功后的调度失败保留 PDF 和任务，显示可重试错误。客户端无论收到成功、部分失败或错误都刷新列表；逐文件错误保留在导入结果面板。

## 会话持久化

Session: `{id,title,paper_ids:string[],messages:ChatMessage[],revision:number,created_at,updated_at}`。

ChatMessage: `{id,role:'user'|'assistant',content,paper_ids:string[],sources:Evidence[],mode?:string,warnings:string[],answer_audit?:AnswerAudit,status:'pending'|'completed'|'error',request_id?:string,created_at?:string}`。`id` 在同一会话内唯一；未提供 `created_at` 时由服务器补充。`paper_ids` 和 `sources` 保存该次问答的文献范围及来源快照，会话当前范围改变后不应改写旧消息。

- GET `/sessions` → `{sessions:[{id,title,paper_ids,revision,created_at,updated_at,message_count,preview}]}`。按更新时间倒序，最多 200 项；`preview` 为最后一条消息内容的前 160 字符。
- POST `/sessions` `{title?:string,paper_ids?:string[],messages?:ChatMessage[]}` → Session，201。默认标题“新对话”，初始 `revision=1`。
- GET `/sessions/{session_id}` → Session。
- PUT `/sessions/{session_id}` `{revision,title,paper_ids,messages}` → Session。四个字段均必需；整体替换可编辑内容，成功后 revision 加 1。
- DELETE `/sessions/{session_id}` → `{ok:true}`。

保存使用乐观锁：客户端提交上次读取的 revision，若服务器已有更新返回 409；客户端应重新读取、合并后保存。不存在的会话返回 404。单会话最多 500 条消息，每条正文最多 100,000 字符；每条消息最多 100 个文献 ID、100 项来源和 100 条警告，整个会话请求最多 4 MiB。标题去除两端空白后不能为空，最多 500 字符。

`/chat` 负责生成答案，客户端负责保存消息。建议先持久化 user 消息和带 `request_id` 的 pending assistant 消息，得到答案后再保存 completed 或 error 状态。`request_id` 用于客户端关联本次请求，服务器按原值保存，不是模型调用的幂等键。客户端处理异步返回时应绑定原会话 ID、请求 ID 和该次文献范围，避免切换会话后的响应写入当前会话。删除文献不会删除消息或其来源快照；已删除文献的原文链接会失效。

界面按会话与消息 ID 保留待保存的完整回答、来源及审查信息；同一页面内切换或重开会话会合并这些内容。“重试保存回答”仅写入会话，不重复生成。迟到响应不能使已加载会话的 revision 回退，也不能恢复已删除的会话。未保存队列保留在当前页面内存，关闭或刷新前提示保存；强制关闭不保证恢复。

## 研究笔记版本

Note: `{id,title,content,paper_ids:string[],sources:Evidence[],kind:'note'|'summary'|'review'|'ideas'|'outline',revision,created_at,updated_at,versions:[{revision,title,created_at}]}`。`versions` 按 revision 倒序排列。

- GET `/notes` → `{notes:[{id,title,paper_ids,kind,revision,created_at,updated_at}]}`，按更新时间倒序，最多 200 项。
- POST `/notes` `{title,content?:string,paper_ids?:string[],sources?:Evidence[],kind?:string}` → Note，201。默认空正文、空范围、空来源，`kind='note'`，初始 `revision=1`。
- GET `/notes/{note_id}` → Note，包含版本目录。
- PUT `/notes/{note_id}` `{revision,title,content?:string,paper_ids?:string[],sources?:Evidence[],kind?:string}` → Note。整体保存一个新版本；省略字段使用创建接口的默认值，保留旧值时需一并提交。
- GET `/notes/{note_id}/versions/{revision}` → `{note_id,revision,title,content,paper_ids,sources,kind,created_at}`，读取指定版本。
- DELETE `/notes/{note_id}` → `{ok:true}`，同时删除该笔记全部版本。

revision 冲突返回 409，不存在的笔记或版本返回 404。历史版本正文、范围和来源快照不可覆盖；删除文献仍保留笔记及版本。标题不能为空且最多 500 字符；正文最多 500,000 字符，最多 100 个文献 ID、100 项来源，单次笔记请求最多 4 MiB。`/research` 的生成结果需由客户端调用此接口保存。

界面在新建、切换笔记或恢复版本前提示处理未保存修改。保存请求发出后继续输入的文字会保留，已保存版本仅对应提交时的快照。有草稿或等待期间发生新编辑时，生成结果另存为笔记；生成成功但落盘失败时可查看并单独重试保存，不重复调用模型。未保存内容仍需在关闭页面前保存。

## 阅读位置

ReadingPosition: `{paper_id,page:number,scroll_ratio:number,revision:number,updated_at:string|null}`。page 为从 1 开始的物理页码；scroll_ratio 为该页阅读滚动位置的比例，范围 `[0,1]`。

- GET `/papers/{id}/reading` → ReadingPosition。尚未保存时返回 `{paper_id,page:1,scroll_ratio:0,revision:0,updated_at:null}`。
- PUT `/papers/{id}/reading` `{revision:number,page:number,scroll_ratio?:number}` → ReadingPosition。scroll_ratio 默认 0，保存成功后 revision 加 1。

初次保存提交 revision 0，之后提交最新 revision；冲突返回 409。超出文献页数返回 400，未知文献返回 404，无效字段返回 422。阅读位置保存至 SQLite，删除文献时一并删除。

## 后台任务控制

- GET `/jobs/{job_id}` → Job。
- GET `/jobs?limit=20&cursor=...`：`active_jobs` 全量包含 queued、processing、pause_requested、paused、cancel_requested 和可恢复的 error；不受历史页数限制。`history_jobs` 为 done/cancelled 历史，每页默认 20 项、范围 1–100，按创建时间与 ID 倒序。`next_cursor` 为下一页游标，末页为 null；非法游标返回 400。兼容字段 `jobs` 为本次 active_jobs 与 history_jobs 的合并。界面始终保留待处理任务，历史使用上一页/下一页导航。
- POST `/jobs/{job_id}/pause` → Job。
- POST `/jobs/{job_id}/resume` → Job。
- POST `/jobs/{job_id}/cancel` → Job。

| 操作 | 当前状态 | 返回或进入的状态 |
| --- | --- | --- |
| pause | queued | paused |
| pause | processing | pause_requested，安全检查点后为 paused |
| pause | paused / pause_requested | 保持当前状态 |
| resume | paused / error | queued，调度执行后为 processing |
| resume | queued / processing | 保持当前状态 |
| cancel | queued / paused / error | cancelled |
| cancel | processing / pause_requested | cancel_requested，安全检查点后为 cancelled |
| cancel | cancel_requested / cancelled | 保持当前状态 |

表外状态转换返回 409；例如 done 任务不能暂停/取消，cancelled 任务不能直接 resume。任务不存在返回 404，文献已删除时返回 409。同一文献同类任务存在另一项 queued、processing、paused、pause_requested 或 cancel_requested 时，恢复旧任务返回 409。

解析在当前页处理结束并入库后响应控制；翻译在当前分块调用返回并保存后响应控制。响应中的 `*_requested` 表示请求已记录，尚未停止。当前模型请求可能已提交给服务方，已产生的计算和费用以服务方实际记录为准。安全检查点保留完整页与已完成翻译分块，恢复时复用缓存。取消后可重新提交解析或翻译任务，已有结果仍可复用；删除文献会取消未完成任务并删除相应页面、缓存和阅读位置。

任务状态、创建参数、检查点均保存至 SQLite。进程重启后 processing 转回 queued 并继续，pause_requested 转为 paused，cancel_requested 转为 cancelled，paused 保持暂停。翻译的目标语言和页码范围随任务保留。暂停或取消解析请求尚在处理时，Paper 仍可能显示 processing，应以 Job 状态判断控制进度。

普通接口遇到 SQLite 操作错误时返回 503 和可重试提示；批量导入沿用逐文件结果协议。后台执行器即使无法保存失败状态，也会释放任务占用；存储恢复后，对没有执行器占用的残留 processing 任务执行暂停、取消或继续，会先恢复其持久化状态，无需重启。仍在运行的任务保持上述检查点语义。启动阶段清理未提交导入留下的应用临时 PDF，保留已登记文件、其他文件名及符号链接。

同一数据目录仅允许一个应用实例运行。系统文件锁在数据库初始化前检查，运行期间持续持有；第二实例启动失败，不会进入恢复或清理。退出时先等待后台任务结束再释放锁，进程被强制结束后由操作系统释放锁。`.workspace.lock` 文件会保留，不应通过删除它来绕过占用检查。

`GET /stats` 的 `pages` 表示已解析完成的页面数，与界面“已解析页面”一致；文献的总物理页数仍由各自 `page_count` 提供。

## 翻译范围与估算

FullTranslation: `{target_language?:'zh'|'en',start_page?:number,end_page?:number|null}`。默认语言 zh、起始页 1；end_page 省略或 null 时取文献最后一页。范围包含起止页，要求 `1 <= start_page <= end_page <= page_count`。

- POST `/papers/{id}/translation-estimate` FullTranslation → `{paper_id,target_language,start_page,end_page,pages,total_characters,pending_characters,estimated_input_tokens,cached_pages,blank_pages,warning}`。
- POST `/papers/{id}/translation-jobs` FullTranslation → `{job_id,job:Job}`。
- POST `/papers/{id}/translate` FullTranslation → `{job_id}`，保留原兼容响应。

估算读取已解析正文，不调用模型，也无需 API 密钥。`total_characters` 是所选范围正文字符数；`pending_characters` 扣除有效整页和分块缓存。`cached_pages` 为命中整页缓存的页数，`blank_pages` 为无正文且无解析警告的页数。`estimated_input_tokens` 按待处理文本中的 ASCII 字符数除以 4，再加非 ASCII 字符数，向上取整；不包含提示词和输出，也不返回推测金额。

所选页尚未解析，或空正文页带有解析/OCR 警告时返回 400；范围之外的页面不影响该任务。创建翻译任务需要配置模型 API 密钥，否则返回 503。未知文献返回 404，非法范围返回 400，非法语言/字段类型返回 422。

同一文献仅允许一个尚未结束的翻译任务：相同语言与范围复用 queued/processing/paused/pause_requested 任务；paused 不会因再次提交而自动继续，须调用 resume。不同语言或范围、以及正在取消的任务返回 409。cancelled/error/done 后重新提交可创建新任务，并复用匹配原文与目标语言的缓存。翻译按范围内各页处理，分块结果也会持久化；不会将部分译文当作整页已完成结果返回。

## 原文导航与导出

- GET `/papers/{id}/search?q=...&limit=40` → `{matches:Evidence[],total,query}`。全文子串/多词查找，保留原文、版本、页码和坐标。
- GET `/papers/{id}/references` → `{references:[{label,text,page,block_id,bbox,arxiv_id,version,doi,year,linked_paper_id?,linked_version?,match_rule,match_note,confidence,cross_evidence,linked_family_id?}]}`。明确指定的 arXiv 版本精确匹配，缺失时不以其他版本替代；未指定版本时可链接本地最新版本，并说明匹配规则。
- GET `/papers/{id}/pages/{page}/reference-links` → `{links:[{label,source:Evidence,target:Evidence}]}`。source 用于从参考文献返回正文；未识别的引用不伪造跳转。
- GET `/papers/{id}/pages/{page}/text-layer` → `{width,height,mode:'native'|'ocr',spans:[{text,bbox,font_size}]}`。扫描件文字层使用 OCR 行的估算位置。
- GET `/papers/{id}/compare/{other_id}` → `{left:Paper,right:Paper,similarity,changes:[{kind,left_page,right_page,before,after}],truncated,notes}`。仅比较同族且均为 ready 的论文，未完成解析返回 409；最多返回 500 处文本块差异，相似度不是语义等价概率。
- GET `/papers/{id}/citation?format=bibtex|json` → BibTeX 下载或引用元数据。缺失年份/作者不编造；arXiv 年份来自标识符。
- GET `/citations/export?paper_ids=id1,id2` → 批量 BibTeX。
- GET `/papers/{id}/translations/export?target_language=zh|en&format=md|html` → 导出有效缓存译文，明确标注未译页面；无译文时返回 409。
- GET `/graph/export?format=mermaid|dot` → 当前库内引用网络源码。表示文献引用关系，不冒充论文模型架构。

`/chat`、`/research`、`/translate` 会在客户端断开后取消本地等待与上游连接。已经到达外部模型服务的计算或计费不保证撤回。

## 句段对照翻译

- GET `/papers/{id}/pages/{page}/aligned-translation?target_language=zh|en` → AlignedTranslation。无需模型，返回原文句段及有效缓存。
- POST 同路径，JSON `{target_language?:'zh'|'en'}` → AlignedTranslation。无有效缓存时调用已配置模型；未配置返回 503。按固定源编号接收 JSON 配对，拒绝缺项、重复/未知编号及空译文；全部批次成功后一次写入。
- GET `/papers/{id}/pages/{page}/aligned-translation/export?target_language=zh|en&format=md|html` → 下载。需当前原文的完整有效配对，否则 409；HTML 中原文与译文可相互跳转，保留原文块坐标。

AlignedTranslation: `{paper_id,page,target_language,source_hash,cached,segments:[{id,block_id,page,start,end,source,bbox,kind,translation:string|null}],notes:string[]}`。`start/end` 为 Python Unicode 字符索引，采用左闭右开区间，指向源块的原始 text。每段保留源块 bbox，并不声称有精确的句子框。段落按句末边界切分，超长句子再切片；公式/表格保持整块，单块超过 4500 字符时生成返回 400，并提示改用整页翻译。页面上限为 180 段或 30,000 源字符。

缓存指纹涵盖块 ID、文字、类型、顺序和坐标；完成生成后在数据库事务内重新核对指纹。原文变化或删除则返回 409，旧结果不写入。配对编号检查仅保证对应结构，不能证明翻译语义正确。

## 引用边来源

CitationEvidence 包含参考文献条目的 `paper_id/block_id/page/bbox/text`，以及 `source_paper_id/source_version/linked_paper_id/linked_version/match_rule/match_note/confidence`。图谱节点按论文族合并，但每条边的来源仍指向实际引用版本，点击原文时不能改跳到代表节点的新版本。

引用列表与图谱共用规则：明确 arXiv ID/版本或真实 DOI 为 1.0；完整标题为 0.9；长标题近似且作者/已知年份交叉支持为 0.8。评分不是统计概率。同名歧义、互相冲突的标识符和缺失的指定版本不自动连接；不同参考条目不会拼接成一个标题，正文提及不会直接生成引用边。

无稳定标识且作者互斥的同名论文，即使历史存储的 family_id 相同，也不能自动连到同一个图谱节点；有唯一作者及已知年份支持时按该论文连接，无唯一支持则保留歧义提示。
