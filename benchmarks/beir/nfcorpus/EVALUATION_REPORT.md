# EnerLedger 与 WeKnora 检索系统对照评测报告

**报告版本：** 1.0

**实验日期：** 2026 年 9 月 28 日

**评测对象：** EnerLedger 与腾讯开源项目 WeKnora 的本地隔离部署

**数据集：** BEIR NFCorpus，test 划分

**评测范围：** 源文档级检索排序质量；不包含答案生成、响应时延或资源成本

## 结论摘要

在本次指定配置下，EnerLedger 的五项 `@10` 指标点估计均高于 WeKnora。
两边使用同一个 `qwen3.7-text-rerank` 模型时，EnerLedger 的 Precision@10
为 **0.3183**，WeKnora 为 **0.2960**；二值 NDCG@10 分别为 **0.4263**
和 **0.4088**。这两项差值的逐题配对 bootstrap 名义 95% 区间均高于零。
Recall@10 分别为 **0.2040** 和 **0.2038**，差值区间跨零；MRR@10、
MAP@10 的差值区间也跨零。因此，本实验支持“该配置下 EnerLedger 的
Precision@10 和二值 NDCG@10 较高”，不足以证明另外三项指标有稳定优势，
更不能推论中文能碳业务中的总体效果。[逐题配对结果](results/compare-ltr-rerank-k10.json)
保留了各指标的原始差值和区间。

**Recall@10 约 0.20 并非单一故障造成。** NFCorpus 每题有多篇相关文档，
而指标只允许前 10 篇命中；按本次 test 标注计算，Recall@10 的全语料
理论宏平均上限是 **0.6146**。同时，重排前的前 20 个分块并未覆盖足够多
相关源文档：在固定候选集合上做理想排序时，EnerLedger 和 WeKnora 的
Recall@10 上限分别只有 **0.2342** 和 **0.2097**。因此，这轮实验的主要
改进空间在候选覆盖、分块去重与窗口设置，而不仅是最后一步重排。

## 1. 评测目的与系统边界

本报告回答的是：两套系统在同一份公开英文语料、同一批测试问题、同一套
相关性标注和源文档级评分器下，能把相关文档排到前 10 位的程度。BEIR
将 NFCorpus 列为生物医学检索任务，语料和问题围绕营养事实、科学文章，
语言为英文。它为可复核的横向对照提供数据基础，但与 EnerLedger 的中文
能碳业务存在领域差异。[BEIR 数据集列表](https://github.com/beir-cellar/beir)、
[NFCorpus 数据卡](https://huggingface.co/datasets/BeIR/nfcorpus)。

本报告是**本地独立实验**，不是腾讯发布的 WeKnora 官方成绩，也不是
BEIR 官方榜单分数。比较对象是本次配置下的两条产品检索排序链路；
两边的分块、关键词/稀疏检索及融合机制并未统一，故不能把全部差异
解释为某个单独算法的因果收益。

## 2. 数据、完整性与实验环境

| 项目 | 实际使用值 |
| --- | --- |
| 公开来源 | BEIR 官方 `nfcorpus.zip`，上游 MD5 `a89dba18a62ef92f7d323ec890a0d38d` |
| 测试集合 | 3,633 篇源文档、323 个 test 问题、12,334 条正相关标注 |
| 标注等级 | 11,758 条为等级 1；576 条为等级 2；本报告统一按“分数 > 0”计为相关 |
| 单题相关文档数 | 平均 38.19、中位数 16、最少 1、最多 475；194/323 题超过 10 篇 |
| 数据完整性 | 两边均确认 3,633/3,633 篇解析完成后才执行检索 |
| 评分单位 | 原始 BEIR 源文档 ID；分块排序后对源文档去重，再按前 10 篇评分 |
| 统计方法 | 323 题逐题计算，宏平均；差值按同一问题配对 bootstrap 10,000 次，随机种子 `20260928` |

下载包 MD5 与 [BEIR 官方仓库](https://github.com/beir-cellar/beir)所列值一致。
解压后 `corpus.jsonl`、`queries.jsonl`、`qrels/test.tsv` 的 SHA-256 分别为：

```text
10cc83ef1826b1425e6a87090b5140b39b27755d5a27e48215a88611c899991f
d024e6621b84925d485ae473d316a0c3af31c62c8068a59fb29d22f7613aef2a
f8fba6ef3d4dd9c3a242a8ba4ae38276fc3622fce7dcbae764766d564542fd2a
```

实验在本机两个隔离 Docker Compose 项目中完成。EnerLedger API 镜像摘要为
`sha256:5b2ce0ecbd6a01c8a95115ad51f1be18020f994cb21a2d9c34db987241c4fcc5`，
对应本轮原始检索记录的源码修订 `a5558a175d8e6bddacb5ffa5749954cd90601ff6`；
WeKnora 应用镜像摘要为
`sha256:08e7b6a01fad18e92835147a8a1ad9be2c9cefd4415eb2302b504fe647d8eea2`，
对照源码修订为 `3e8b0bfc80b845b2d4b2ed683994748741450a97`。本项目
和 WeKnora 的 Dense Embedding 均采用 `qwen3.7-text-embedding` 的 2048 维
输出。详细的模型配置、文档状态及索引规模见[原始检索记录](RESULTS.md)。

NFCorpus 有 40 对内容完全相同但 ID 不同的文档。为避免 WeKnora 按相同
文件类型与正文哈希拒绝第二篇，适配器将每对第二篇以 `.txt` 上传；正文
字节和独立 BEIR ID 均保留。这保证评分集合完整，同时也意味着少量文件
走了不同扩展名的解析路径。

## 3. 两轮实验的检索与重排协议

| 阶段 | EnerLedger | WeKnora |
| --- | --- | --- |
| 原始检索 | BM25、Sparse、Dense 三路加权融合；`/api/v1/recall` 返回前 64 个分块 | 官方 `hybrid-search`，向量与关键词检索并融合；`match_count=64` |
| 重排实验 | 同样的三路召回，使用 `blind_v5_candidate_routing_v1` 冻结候选契约；`candidate-difference-v3-20260728-final33` LambdaMART 对完整可见候选池排序；取前 20 个分块 | 向量与关键词的 `hybrid-search` 直接返回前 20 个分块 |
| 共同模型 | 对各自 20 个分块调用同一个 `qwen3.7-text-rerank` | 同左；由共同测评执行器调用，不是 WeKnora 聊天链内的模型配置 |
| 检索阈值及扩展 | 按各自候选契约；三路执行失败则重排实验中止 | 本次请求显式设定 `vector_threshold=0`、`keyword_threshold=0`、`skip_context_enrichment=true` |
| 评分前处理 | 依据最终分块顺序对源文档 ID 去重 | 同左 |

WeKnora 测试知识库实际启用向量与关键词索引，图谱与 Wiki 索引关闭；
它属于两路混合召回，并非与 EnerLedger 完全相同的三路。WeKnora 的
512 字符、50 字符重叠是本实验显式设置，**不应写成未经配置的官方默认值**。
该配置产生 18,237 个向量分块；EnerLedger 使用 512 token 上限、64 token
重叠，产生 4,024 个分块。因此，虽然源文档、Dense 模型和评分器一致，
分块尺度并不一致。另设 WeKnora 2,048 字符、256 字符重叠知识库，
产生 7,768 个分块，仅用于**原始检索**的分块敏感性对照。

重排实验的同一模型通过[阿里云百炼原生 text-rerank 接口](https://help.aliyun.com/en/model-studio/text-rerank-api)
调用。任何召回路失败、LambdaMART 降级或模型输出不完整，测评脚本都会
中止而不是把兜底顺序计入成绩。323 题最终全部完成，LambdaMART 的记录
均为 `ltr` 或 `ltr_short_low_confidence`，两者都使用模型输出顺序。
这一串联位于隔离测评执行器；EnerLedger 当前生产 RAG 的 `active` 模式
仍由 LambdaMART 取代远程 rerank，不能将本报告结果冒称为现有生产问答
链路的在线成绩。WeKnora 的聊天链也未参与本次重排实验。

## 4. 指标定义与数值上限

设单题正相关源文档集合为 $R_q$，去重后的结果列表为 $L_q$。本报告
采用二值相关性并对所有 323 题取算术平均：

| 指标 | 本报告计算方式 | 解读时须注意 |
| --- | --- | --- |
| Precision@10 | 前 10 位命中数除以 10 | 即使返回不足 10 篇，分母仍是 10；若单题标注相关文档少于 10 篇，也不可能达到 1 |
| Recall@10 | 前 10 位命中数除以该题全部正相关文档数 $\lvert R_q\rvert$ | 每题只展示 10 篇，无法覆盖大量正相关文档 |
| 二值 NDCG@10 | 按相关/不相关的折损增益除以二值理想排名的增益 | 等级 1 与 2 被折叠；不能直接等同保留分级标注的 NDCG |
| MRR@10 | 前 10 位第一篇相关文档名次的倒数；无命中为 0 | 只关心第一篇相关文档，不衡量其余相关文档覆盖 |
| MAP@10 | 前 10 位各相关命中处 Precision 的和，除以该题**全部**正相关文档数 | `AP@10` 分母不是 `min(10, 相关文档数)`，因此与 Recall@10 一样受截断上限限制 |

本次 qrels 的分级值为 1 与 2，评分器均转为相关。未列在正相关标注中的
返回文档按基准口径记为未命中；这不等于断言其在医学上绝对无关。
指标具体实现见 [`score_nfcorpus`](../../../app/rag/evaluation/beir_nfcorpus.py)。

仅考虑数据集标注数量，即使任何系统都能把相关文档理想地排在前面，
Recall@10 与 MAP@10 的宏平均上限仍是
$\frac{1}{323}\sum_q\frac{\min(10,\lvert R_q\rvert)}{\lvert R_q\rvert}=\mathbf{0.6146}$；
Precision@10 的相应上限为 **0.7746**。NDCG@10 与 MRR@10 不受这一
分母上限约束，理想值仍可为 1。以上均是**本次二值、源文档级、宏平均
评分规则的数学上限**，不是系统实测分数。

## 5. 实测结果

### 5.1 原始检索：尚未调用重排模型

| 指标 @10 | EnerLedger | WeKnora 512/50 | WeKnora 2048/256 |
| --- | ---: | ---: | ---: |
| Precision | 0.3080 | 0.2904 | 0.2895 |
| Recall | 0.2014 | 0.1986 | 0.1948 |
| 二值 NDCG | 0.4142 | 0.3961 | 0.3918 |
| MRR | 0.6218 | 0.6078 | 0.6000 |
| MAP | 0.1606 | 0.1576 | 0.1511 |

对 WeKnora 512/50 配置，EnerLedger 减 WeKnora 的 Precision@10 差值为
`+0.0176`（名义 95% 区间 `[+0.0056, +0.0300]`），二值 NDCG@10 差值为
`+0.0181`（`[+0.0044, +0.0317]`）；另外三项区间跨零。WeKnora 改为
2048/256 分块后，五项指标的点估计没有优于 512/50；两个 WeKnora
分块配置之间的五项差值区间均跨零。这只是原始检索的分块敏感性检查，
**没有**在 2048/256 配置上执行相同的重排对照。完整区间见
[512/50 比较](results/compare-512chars-k10.json)、
[2048/256 比较](results/compare-2048chars-k10.json)与
[WeKnora 分块比较](results/compare-weknora-chunking-k10.json)。

### 5.2 指定的 LambdaMART / 同模型重排实验

| 指标 @10 | EnerLedger | WeKnora 512/50 | 差值（EnerLedger − WeKnora） | 名义 95% 配对 bootstrap 区间 |
| --- | ---: | ---: | ---: | ---: |
| Precision | **0.3183** | 0.2960 | +0.0223 | [+0.0111, +0.0337] |
| Recall | **0.2040** | 0.2038 | +0.0002 | [−0.0114, +0.0106] |
| 二值 NDCG | **0.4263** | 0.4088 | +0.0174 | [+0.0052, +0.0295] |
| MRR | **0.6352** | 0.6183 | +0.0169 | [−0.0067, +0.0409] |
| MAP | **0.1683** | 0.1654 | +0.0029 | [−0.0061, +0.0110] |

Precision 与二值 NDCG 的名义区间未跨零；Recall、MRR、MAP 的区间跨零。
这些是五项分别计算的名义 95% 区间，**未做多指标检验校正**。原始检索
与本节重排实验的候选窗口和 EnerLedger 候选契约也不同，因此两节分数之差
不能解释为“仅增加 rerank 模型的净增益”。

## 6. Recall 及其他指标的诊断解释

**Recall@10 的第一层限制来自数据分布。** 323 题中有 194 题的标注相关
文档超过 10 篇，单题最多 475 篇。对有 40 篇相关文档的问题，即使前 10 位
全部正确，Recall@10 也只能是 `10/40=0.25`。因此，把约 0.20 直接理解
为“系统只找到了全部相关信息的 20%，模型明显失效”，会忽略 `@10` 截断
与宏平均的影响；但理论上限 0.6146 也说明，低分不完全由指标造成。

**第二层限制来自前 20 个分块的相关源文档覆盖。** 下表的“候选内理想
Recall@10”假设不增加任何候选、只把已有候选中的相关源文档排到最前面。
它是当前固定候选集合上的排序上限，不是扩大召回预算后的系统上限。

| 诊断量，323 题平均或计数 | EnerLedger | WeKnora 512/50 |
| --- | ---: | ---: |
| 送入同模型重排的分块数 | 20 | 20 |
| 前 20 分块覆盖的不同源文档数，平均 | 19.32 | 12.08 |
| 候选内正相关源文档数，平均 | 4.54 | 3.22 |
| 候选内没有任何相关源文档的问题数 | 61 | 71 |
| 候选内理想 Recall@10 | 0.2342 | 0.2097 |
| 最终实际 Recall@10 | 0.2040 | 0.2038 |

EnerLedger 在现有前 20 候选集合中即使理想重排也只能到 0.2342，
实测已达 0.2040；WeKnora 则为理想 0.2097、实测 0.2038。
这表明当前固定窗口下，最后一步重排可挽回的 Recall 空间小于扩大
相关源文档覆盖的空间。由于两边分块尺度和初排机制不同，不能把
0.2342 与 0.2097 的差距单独归因于 LambdaMART。

**MAP@10 较低也有同类数学原因。** 它用单题全部相关文档数作分母，
与 Recall@10 具有相同的 0.6146 数据集上限；另外还惩罚相关文档
在前 10 位中出现得晚。Precision@10 的理论宏平均上限为 0.7746：
129/323 题的相关文档不超过 10 篇，某些题即使全部找齐也无法得到
Precision@10=1。WeKnora 有 59 题在对前 20 个分块去重后不足 10 篇
不同源文档，空位仍计入 Precision@10 分母；这也是跨系统比较时必须
记录的候选窗口效应。

**NDCG 与 MRR 不宜按 Recall 的方式解释。** NDCG@10 主要评价相关
文档在前 10 位中的相对位置，且本报告将等级 1、2 合并为二值；
它的 0.4263/0.4088 不具有 Recall 的 0.6146 数学上限。MRR@10 只看
首个相关命中：EnerLedger 与 WeKnora 分别有 180/323、171/323 题在
第 1 位命中，但仍分别有 72、73 题在前 10 位完全无命中。这解释了
MRR 约 0.62—0.64 可以与 Recall 约 0.20 同时成立。

## 7. 统计判断、公平性与适用范围

配对 bootstrap 对相同 323 个问题有放回抽样 10,000 次，报告差值分布
的 2.5% 与 97.5% 分位数。它反映本次**问题集合上的配对不确定性**，
不涵盖重新抽取语料、不同业务领域、模型版本更换或重复运行的波动。
五项指标同时被查看，区间没有多重比较校正。报告结论以方向和区间
为准，不据此宣称某系统在所有检索任务中普遍更优。

两边控制了公开语料、test 问题、Dense Embedding 模型与维度、共同
重排模型、文档 ID 粒度以及评分代码；但以下差异仍进入结果：

- EnerLedger 为 BM25/Sparse/Dense 三路及本地 LambdaMART；WeKnora 为
  向量与关键词两路及自己的融合。它们不是等结构的单算法消融。
- 分块单位为 token 对字符，512/50 的 WeKnora 产生的分块数约为
  EnerLedger 的 4.53 倍；同为“前 20 分块”并不等于“前 20 篇文档”。
- 重排输入使用各系统返回的分块正文，文本边界及标题是否进入分块
  仍可能不同。WeKnora 的 40 对同正文文件还经过 `.txt` 适配。
- 重排实验采用共同执行器直接调用 `qwen3.7-text-rerank`；没有把
  WeKnora 聊天链的 query expansion、MMR 或上下文合并算入结果，也
  没有修改 EnerLedger 生产 RAG 的 `active` 模式。
- 本次未测答案正确率、幻觉率、响应时间、吞吐量、模型费用、资源
  消耗及中文能碳领域效果，不能用本报告替代上述验收。

## 8. 后续验证建议

若目标是判断“哪个系统在真实业务中更好”，应先在两套系统上固定同一
源文档集合和评分规则，补测 `Recall@20/@50/@100` 及重排前候选覆盖；
再做“先按源文档去重、补足 20 篇不同文档后重排”的受控实验，以区分
分块重复占位与模型排序的作用。对 WeKnora 2048/256 分块也应运行
相同的重排协议，而不是用原始检索结果代替。最后需要构建经人工核验
的中文能碳问题与相关性标注，单独验证业务迁移性；若要与保留分级
相关性的外部 NDCG 成绩比较，则应另算 graded NDCG 并注明标注口径。

## 9. 复核材料与引用

- [原始检索实验记录](RESULTS.md)与[重排实验记录](RERANK_RESULTS.md)。
- [EnerLedger 原始排名](results/enerledger-k10.json)、[WeKnora 512/50 原始排名](results/weknora-k10.json)、[WeKnora 2048/256 原始排名](results/weknora-2048chars-k10.json)。
- [EnerLedger 重排排名](results/enerledger-ltr-rerank-k10.json)、[WeKnora 重排排名](results/weknora-hybrid-rerank-k10.json)、[重排配对区间](results/compare-ltr-rerank-k10.json)。以上 JSON 保留 323 题逐题得分与排名、数据 SHA-256，不含 API 密钥。
- [数据读取及评分实现](../../../app/rag/evaluation/beir_nfcorpus.py)、[重排对照执行器](../../../scripts/nfcorpus_ltr_rerank_benchmark.py)、[配对 bootstrap 实现](../../../scripts/compare_nfcorpus_reports.py)。
- 外部资料：[BEIR 官方仓库](https://github.com/beir-cellar/beir)、[NFCorpus 数据卡](https://huggingface.co/datasets/BeIR/nfcorpus)、[阿里云 text-rerank API 文档](https://help.aliyun.com/en/model-studio/text-rerank-api)、[WeKnora 固定修订的混合检索实现](https://github.com/Tencent/WeKnora/blob/3e8b0bfc80b845b2d4b2ed683994748741450a97/internal/application/service/knowledgebase_search.go)。

任何读者均可用上述 JSON 的 `predictions` 与本仓库评分函数重新计算五项
指标；不需要重启 Docker 或重新调用付费模型。报告没有把原始检索与
重排实验混成一份排名，也没有使用外部系统公布但口径不同的分数。

在仓库根目录可用以下命令复算本报告的主要重排结果及配对区间；输出写入
`/tmp`，不会改动已保存的正式报告：

```sh
python3 scripts/nfcorpus_retrieval_benchmark.py score \
  --predictions benchmarks/beir/nfcorpus/results/enerledger-ltr-rerank-k10.json \
  --k 10 --output /tmp/enerledger-rerank-rescored.json
python3 scripts/nfcorpus_retrieval_benchmark.py score \
  --predictions benchmarks/beir/nfcorpus/results/weknora-hybrid-rerank-k10.json \
  --k 10 --output /tmp/weknora-rerank-rescored.json
python3 scripts/compare_nfcorpus_reports.py \
  --left /tmp/enerledger-rerank-rescored.json \
  --right /tmp/weknora-rerank-rescored.json \
  --samples 10000 --seed 20260928 \
  --output /tmp/nfcorpus-rerank-comparison-rescored.json
```
