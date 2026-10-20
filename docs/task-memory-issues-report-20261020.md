# 任务记忆问题分类与修复建议报告

日期：2026-10-20。依据：100 个场景、222 轮消息的真实模型运行（deepseek-v4-flash，324 次抽取/关联调用），及对应代码、数据库和原始模型输出。本报告只分析既有证据，没有重新跑模型或修改业务实现。

## 结论与统计口径

问题归为 **7 类实现问题 + 评分与覆盖问题**。主要集中在任务身份关联、语义更新和结构契约，现有结果不构成增加任务状态的理由：仍保留 **todo / done**；纠正、转交、重开属于更新动作，pending_review 属于处理状态。

| 口径 | 数量 | 解释 |
| --- | ---: | --- |
| 自动检查通过 | 197/222 条；77/100 场景 | 评分器已补读数据库待确认记录 |
| 自动失败 | 25 条 | 其中 2 条是评分规则误报 |
| 自动失败中的实现/契约问题 | 23 条 | 包含前一步错误造成的后续失败，不是23个独立根因 |
| 自动通过项的额外问题 | 3 条 | 抽查发现同义细节重复 |
| 已观察到实现/质量问题 | 26 条，涉及24个场景 | 未全量人工审查所有通过项，不能据此推算完整语义准确率 |

下表按每条消息的**主要原因唯一归类**；同条的次要问题不重复计数。`history_duplicates_09 / 3` 是前一步重复卡片的连锁影响；`temporal_10 / 3` 同时有上一步任务未建成和本步实例关联失败。P1 表示优先修复影响任务身份、状态或正常更新的问题；P2 表示时间规范化与重复细节，二者仍需验收。

## 七类实现问题

| 编号 | 专业分类 | 直白解释 | 涉及消息 | 优先级 |
| --- | --- | --- | ---: | --- |
| T1 | 任务身份关联不稳定 | 同一人、同一次任务换个说法，就找不到或重复建卡。 | 9 | P1 |
| T2 | 抽取输出违反结构契约 | 内容看懂了，但多输出一个不允许的字段，整条更新就被拒绝。 | 2 | P1 |
| T3 | 阻碍语义与细节替换错误 | 问题已经解决，系统仍说被阻碍；或者只解决一个问题，却无法更新。 | 4 | P1 |
| T4 | 任务抽取边界与多任务覆盖错误 | 该更新的任务漏了，不是任务的话却被当成任务。 | 3 | P1 |
| T5 | 周期时间未规范化 | 本周、下周没有稳定落到约定的日期范围。 | 2 | P2 |
| T6 | 明确纠正与责任转交更新路径缺失 | 用户明确说刚才记错了，或换个人负责，系统仍改不过来。 | 3 | P1 |
| T7 | 同义细节去重不足 | 同一条要求或阻碍换个说法，被重复记了。 | 3（漏检） | P2 |


**T1 任务身份关联不稳定**：李若宁改称若宁，原 todo 保留，又建了一张 done；周报延期后，日期范围相同但 instance 文字不同，更新失败。

已确认：records 先按主体字符串筛选，retrieve 又按 instance 字符串筛选；这会在关联模型判断前排除正确卡片。无匹配时 auto 可走新建，update/reopen 则待确认。 代码证据：[task_cards.py:108](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_cards.py:108)、[task_cards.py:146](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_cards.py:146)、[task_cards.py:165](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_cards.py:165)；[原始案例](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/subjects_04/steps.jsonl:2)。

修复方向：优先用来源指向的既有卡片 ID 定位；为有明确证据的同人称谓和同轮次建立稳定映射。展示文字与身份判定分离，仍保留不同人、不同轮次的隔离。 验收：简称、同义标题、同轮次改写和重复提醒均更新原卡；明确另一轮仍新增；多候选歧义仍待确认。


**T2 抽取输出违反结构契约**：恢复誊写乐谱时输出 entities；新航班值机输出 details.0.description_note，两条都进入待确认。

已确认：原始输出有额外字段，TaskInput/TaskDetailInput 使用 extra=forbid，候选保留校验错误并停止写入。旧提示后追加新任务提示是可能诱因，但未做对照实验，不能断言是唯一原因。 代码证据：[task_schema.py:7](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_schema.py:7)、[task_cards.py:61](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_cards.py:61)、[extractor.py:758](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/extractor.py:758)；[原始案例](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/lifecycle_06/steps.jsonl:3)。

修复方向：让任务抽取只有一套明确契约；优先约束模型输出。不要直接取消字段校验；若采用有限修正重试，应保留原输出与修正记录。 验收：相同语义能产出合法结构并保存；非法字段不会静默混入业务卡片；重试失败仍可追溯。


**T3 阻碍语义与细节替换错误**：“缺材料的阻碍已解除”被新增为 active blocker；“尚未安装”也被分成 blocker；网络恢复时跨类型替换被校验拒绝。

已确认：模型混淆“未完成”“曾经的阻碍”“当前阻碍”；部分关联计划把进展替换到阻碍细节。merge 会将新增细节设为 active，故即使旧阻碍已退役，错误的新 blocker 仍有效。 代码证据：[task_cards.py:194](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_cards.py:194)、[task_cards.py:238](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_cards.py:238)；[原始案例](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/progress_blockers_04/steps.jsonl:2)。

修复方向：明确 blocker 只表达当前阻止执行的原因；解除动作退役旧 blocker 并记录 progress/outcome。关联计划须验证替换下标及细节类型。 验收：解除唯一阻碍后没有有效 blocker；解除部分阻碍只保留未解决的一项；未开始/未完成本身不自动成为 blocker。


**T4 任务抽取边界与多任务覆盖错误**：“审核完成、发送未完成”只更新发送卡；“两次分别记录”生成第三个“记录清洁检查”任务；“没有租车计划”和引用体检文字产生无谓待确认。

已确认：抽取遗漏独立审核任务的完成候选；另外两例将记录指令、否定计划或引用内容输出为业务任务。后续关联校验只能拦住部分写入，仍造成额外卡片或待确认。 代码证据：[task_schema.py:58](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_schema.py:58)、[extractor.py:765](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/extractor.py:765)；[原始案例](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/identity_instances_04/steps.jsonl:3)。

修复方向：先区分实际行动、记忆管理表达、否定和引用；再逐项覆盖消息中每个已有任务的变化。用漏抽与多抽两类断言共同验证。 验收：审核 done/发送 todo 分别更新；“分别记录”不增加业务任务；否定计划和引用既不建卡，也不无故要求用户确认。


**T5 周期时间未规范化**：保存“2026-10-20所在周”或直接保存“上周/本周”，没有输出明确起止日期。

已确认：专属提示要求将相对轮次转为绝对范围，但 instance 是自由文本，缺少可验证的统一表示。这里是格式/规范化缺陷，不能把“某日所在周”直接说成日期算错。 代码证据：[task_schema.py:22](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_schema.py:22)、[task_schema.py:57](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_schema.py:57)；[原始案例](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/temporal_01/steps.jsonl:1)。

修复方向：按消息时间及时区，使用确定的周一至周日规则生成标准周期身份；保留原始时间表达供溯源。截止时间与任务轮次分别处理。 验收：周二、周日、周一及跨年得到正确范围；延期不改变原任务轮次；日期表达变化不会引起关联失败。


**T6 明确纠正与责任转交更新路径缺失**：转交哥哥后旧任务被取消，哥哥未接上；纠正误报完成后仍是 done；301 改 302 的纠正被错误的细节替换计划拒绝。

已确认：主体筛选阻断转交；done→todo 只允许 reopen，未区分事实纠正；会议室案例关联计划把 requirement 的 index 0 指向旧 time 细节。代码检查还发现 merge 不更新已有卡片的 task/subject，后者是潜在后续障碍，未追加到观测数量。 代码证据：[task_cards.py:207](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_cards.py:207)、[task_cards.py:220](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_cards.py:220)、[task_cards.py:225](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_cards.py:225)；[原始案例](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/subjects_09/steps.jsonl:2)。

修复方向：将明确纠正、重开、转交区分为更新动作，并验证原卡及证据；转交应完整更新负责人，避免只关闭旧责任。对象纠正需同步卡片身份、检索文字及历史。 验收：明确口误纠正恢复原卡 todo；301→302 保持同一任务且原时间不丢；转交后仍有一张正确负责人名下的 todo；普通补充不重开 done。


**T7 同义细节去重不足**：“需要低温运输”和“低温要求不变”同时有效；营业执照缺件被保存两条有效阻碍。

已确认：signature 用 kind、condition、description 的完全一致判断重复。语义相同但文字不同的新细节会追加；本次三条都通过了原自动断言。 代码证据：[task_cards.py:142](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_cards.py:142)、[task_cards.py:242](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/memory/task_cards.py:242)；[原始案例](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/details_05/steps.jsonl:2)。

修复方向：对明确“仍然/不变/重复”的信息复用旧细节 ID、追加来源；对同义细节做有条件的合并，不能合并条件不同的要求。 验收：重复说明后有效细节数量不增加、来源可追加；低温与避光各保留一条；条件不同的相似细节仍独立。


## 评分问题与测试盲区

| 问题 | 证据与影响 | 当前状态 / 建议 |
| --- | --- | --- |
| 待确认记录漏读 | references_04 第2步、references_07 第1步已入库，但返回results为空 | **已修复**；按原消息ID核对数据库。原195/222改为197/222，未重跑模型 |
| 类型断言过严 | details_01 的“不下雨”条件完整保存在time.condition，仍因没有requirement被判失败 | **未修复**；优先检查条件是否完整，只有业务明确要求时才强制kind |
| 字面禁词误报 | details_03 有效要求已是A5，A4只在“原要求作废”备注中出现 | **未修复**；区分当前有效要求、否定说明与历史，不对所有文字统一禁词 |
| 同义重复漏检 | details_05、details_08、history_duplicates_06 自动通过但存在重复细节 | **未修复**；增加有效细节数量、同义去重及来源合并断言 |
| 状态归属和查询覆盖不足 | 代码通常只比较statuses/closures集合，可能漏掉任务之间状态互换；查询只检查非空 | **覆盖缺口，非本次已证实故障**；增加按稳定任务身份的状态/原因断言，以及预期证据ID、过滤和召回检查 |

保留原分数，不把两处误报加回去就宣称“199/222准确”：通过项同样存在漏检，必须先完善判定方法。

## 建议实施顺序与验收

1. **先修评分口径并冻结回归集**：保留本次原始输出，修正两处误报，补充按任务归属及语义重复断言；再做新旧结果比较，避免分数变化来自尺子变化。
2. **修抽取契约与身份定位（T2、T1，结合T5）**：统一任务输出契约、稳定主体/轮次标识与来源定位。保留真正多候选时的确认，不能靠放松全部校验换通过率。
3. **修语义更新（T3、T4、T6）**：优先覆盖阻碍解除、多任务分别完成、明确纠正及转交；维持既有两状态模型和版本历史。
4. **修细节去重（T7），重跑全部100场景**：检查有效事实和来源，不只看卡片数量。再加未参与调试的新说法及必要的重复采样，评估稳定性。

本次无运行时处理/查询异常；271份卡片快照的两状态约束和来源可追溯检查通过。这些正向结果不能替代语义验收。最终回答生成、飞书送达、查询排名及完整召回仍未验证。

## 可追溯问题清单

每个链接指向原始JSONL的对应步骤，包含输入、模型输出、处理结果及保存后的卡片。原始输出在本地被Git忽略；分类摘要和下面的报告已保存到仓库。

| 分类 | 场景 / 步骤 | 发现方式 | 连锁影响说明 |
| --- | --- | --- | --- |
| T1 | [subjects_04 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/subjects_04/steps.jsonl:2) | 自动失败 | 本步观测到问题 |
| T1 | [subjects_07 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/subjects_07/steps.jsonl:2) | 自动失败 | 本步观测到问题 |
| T1 | [temporal_03 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/temporal_03/steps.jsonl:2) | 自动失败 | 本步观测到问题 |
| T1 | [temporal_10 / 3](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/temporal_10/steps.jsonl:3) | 自动失败 | 包含上一步未建成28日任务的连锁影响，同时21日任务仍存在instance文本关联失败。 |
| T1 | [history_duplicates_09 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/history_duplicates_09/steps.jsonl:2) | 自动失败 | 本步观测到问题 |
| T1 | [history_duplicates_09 / 3](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/history_duplicates_09/steps.jsonl:3) | 自动失败 | 前一步重复卡片导致本步卡片数继续超标，本步明确新增动作本身合理。 |
| T1 | [history_duplicates_10 / 3](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/history_duplicates_10/steps.jsonl:3) | 自动失败 | 本步观测到问题 |
| T1 | [reopen_new_round_02 / 3](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/reopen_new_round_02/steps.jsonl:3) | 自动失败 | 本步观测到问题 |
| T1 | [nonfacts_corrections_06 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/nonfacts_corrections_06/steps.jsonl:2) | 自动失败 | 本步观测到问题 |
| T2 | [lifecycle_06 / 3](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/lifecycle_06/steps.jsonl:3) | 自动失败 | 本步观测到问题 |
| T2 | [temporal_10 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/temporal_10/steps.jsonl:2) | 自动失败 | 本步观测到问题 |
| T3 | [progress_blockers_04 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/progress_blockers_04/steps.jsonl:2) | 自动失败 | 本步观测到问题 |
| T3 | [progress_blockers_05 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/progress_blockers_05/steps.jsonl:2) | 自动失败 | 本步观测到问题 |
| T3 | [progress_blockers_06 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/progress_blockers_06/steps.jsonl:2) | 自动失败 | 本步观测到问题 |
| T3 | [progress_blockers_08 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/progress_blockers_08/steps.jsonl:2) | 自动失败 | 本步观测到问题 |
| T4 | [identity_instances_04 / 3](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/identity_instances_04/steps.jsonl:3) | 自动失败 | 本步观测到问题 |
| T4 | [temporal_05 / 1](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/temporal_05/steps.jsonl:1) | 自动失败 | 本步观测到问题 |
| T4 | [nonfacts_corrections_10 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/nonfacts_corrections_10/steps.jsonl:2) | 自动失败 | 本步观测到问题 |
| T5 | [temporal_01 / 1](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/temporal_01/steps.jsonl:1) | 自动失败 | 本步观测到问题 |
| T5 | [temporal_02 / 1](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/temporal_02/steps.jsonl:1) | 自动失败 | 本步观测到问题 |
| T6 | [subjects_09 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/subjects_09/steps.jsonl:2) | 自动失败 | 本步观测到问题 |
| T6 | [nonfacts_corrections_07 / 3](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/nonfacts_corrections_07/steps.jsonl:3) | 自动失败 | 本步观测到问题 |
| T6 | [nonfacts_corrections_09 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/nonfacts_corrections_09/steps.jsonl:2) | 自动失败 | 本步观测到问题 |
| T7 | [details_05 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/details_05/steps.jsonl:2) | 自动通过但人工发现问题 | 本步观测到问题 |
| T7 | [details_08 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/details_08/steps.jsonl:2) | 自动通过但人工发现问题 | 本步观测到问题 |
| T7 | [history_duplicates_06 / 2](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/results/task-cards-100-live-20261020/history_duplicates_06/steps.jsonl:2) | 自动通过但人工发现问题 | 本步观测到问题 |

原测试报告：[task-cards-100-live-evaluation-20261020.md](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/docs/task-cards-100-live-evaluation-20261020.md)。机器可读分类及完整映射：[task-cards-100-issue-taxonomy-20261020.json](/Users/zhangchengjie/Projects/suixinji/task-memory-cards/eval/reports/task-cards-100-issue-taxonomy-20261020.json)。被测版本 `bb95406`；评分器修正及结果版本 `9accd89`。本轮仅增加报告与分类数据，未修改业务代码或线上数据。
