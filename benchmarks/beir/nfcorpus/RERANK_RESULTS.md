# NFCorpus 重排对照实验

本实验沿用 [原始召回实验](RESULTS.md) 中已经完成解析的 3,633 篇 BEIR
NFCorpus 文档、test split 的 323 个问题及相同的源文档级 `@10` 评分器。
与原始召回实验不同，本实验在两边各自的召回结果后调用**同一个**
`qwen3.7-text-rerank` 模型。模型通过阿里百炼原生 text-rerank 接口调用，
每题每边传入 20 个分块；任何模型调用失败或输出不完整都会使测评中止。

本项目使用已部署的 BM25、Sparse、Dense 三路召回和 LambdaMART 冻结候选
契约 `blind_v5_candidate_routing_v1`。LambdaMART 对完整的可见候选池排序，
取前 20 个分块后调用重排模型。强制启用 LambdaMART，发生超时或降级时
立即中止，不把加权融合顺序当作 LambdaMART 结果。WeKnora 使用官方
`hybrid-search` 接口，向量与关键词两路按它自己的配置融合，直接取前 20
个分块调用同一重排模型。两边都先对分块重排，再按原始源文档 ID 去重，
计算 Precision、Recall、NDCG、MRR、MAP @10。
WeKnora 本次知识库的 `indexing_strategy` 实际显示
`vector_enabled=true`、`keyword_enabled=true`、`graph_enabled=false`、
`wiki_enabled=false`，因此它是向量加关键词两路，并非与本项目完全相同的三路。
本组 WeKnora 分块为上一轮明确设置的 512 字符、50 字符重叠配置，
不能称为未改参数的官方默认配置。
本项目 LambdaMART bundle 版本为 `candidate-difference-v3-20260728-final33`。

这里比较的是指定的两套**端到端检索排序策略**，而非纯粹隔离出模型本身
效果的消融实验。两边的初排路线、融合、分块尺度和前 20 个分块覆盖的不同
文档数仍可能不同；它们都会影响文档级指标。WeKnora 此处使用公开
`hybrid-search` 接口和测评脚本的共同重排调用，没有借用聊天问答链中的
query expansion、上下文合并或其他后处理。本项目的生产 RAG `active` 模式
当前由 LambdaMART 取代远程 rerank；此处的串联只属于本次隔离测评执行器。

测评脚本为 [`scripts/nfcorpus_ltr_rerank_benchmark.py`](../../../scripts/nfcorpus_ltr_rerank_benchmark.py)。
它在隔离的本项目 API 容器中运行，使用该镜像已安装的召回实现和 LambdaMART
模型，且只从现有测试模型配置中读取百炼 API 密钥，不将密钥写入报告。
每题完成后写入 `checkpoint.json`，完整 323 题均成功后才输出两份指标报告。

## 实测结果

| 指标 @10 | 本项目：三路召回 → LambdaMART → top20 → 同模型重排 | WeKnora：向量+关键词 → top20 → 同模型重排 | 本项目减 WeKnora | 逐题配对 bootstrap 95% 区间 |
| --- | ---: | ---: | ---: | ---: |
| Precision | 0.3183 | 0.2960 | +0.0223 | [+0.0111, +0.0337] |
| Recall | 0.2040 | 0.2038 | +0.0002 | [−0.0114, +0.0106] |
| NDCG | 0.4263 | 0.4088 | +0.0174 | [+0.0052, +0.0295] |
| MRR | 0.6352 | 0.6183 | +0.0169 | [−0.0067, +0.0409] |
| MAP | 0.1683 | 0.1654 | +0.0029 | [−0.0061, +0.0110] |

区间以 323 个问题为配对单位，有放回重采样 10,000 次，种子 `20260928`。
本项目五项指标的点估计均更高；Precision 和 NDCG 的区间在零以上，
Recall、MRR、MAP 的区间跨零，所以不能据此认定后三项有稳定优势。
全部 323 题的 LambdaMART 模式均为 `ltr` 或
`ltr_short_low_confidence`，不存在加权融合兜底。前 20 个分块平均覆盖
本项目 19.32 篇、WeKnora 12.08 篇不同源文档；这是本次文档级指标差异的
一个重要解释因素，不能把总差值全部归因于 LambdaMART。

正式报告及逐题排名：

- [`results/enerledger-ltr-rerank-k10.json`](results/enerledger-ltr-rerank-k10.json)
- [`results/weknora-hybrid-rerank-k10.json`](results/weknora-hybrid-rerank-k10.json)
- [`results/compare-ltr-rerank-k10.json`](results/compare-ltr-rerank-k10.json)
