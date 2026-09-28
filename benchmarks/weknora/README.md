# WeKnora 官方样例检索评测

本地 `samples/` 已从 `Tencent/WeKnora` 的 `dataset/samples/` 下载原始 Parquet 文件；
该目录被 Git 忽略，其他环境用 `fetch` 命令重建。固定上游提交为
`9114e4e4f905be71976a77c684d3f92731e6f9a6`，来源：
https://github.com/Tencent/WeKnora/tree/9114e4e4f905be71976a77c684d3f92731e6f9a6/dataset/samples 。
运行 `inspect` 会输出每个文件的 SHA-256，报告也会记录校验值。

**这组文件只能用于评测管线冒烟测试。**当前快照只有 1 个问题、4 个语料段落；
该问题的 4 个段落全部被标为相关，没有负例。Precision、排序指标和总体胜负
都无法从它得出有意义的结论。脚本会在报告中将 `suitable_for_comparison` 设为
`false`。正式比较需要另备有足够问题和正负标注的冻结测试集，可以沿用相同的
五文件格式及计分器。

## 运行

在含 `pyarrow` 和本项目依赖的 Python 环境中：

```sh
python scripts/weknora_retrieval_benchmark.py fetch
python scripts/weknora_retrieval_benchmark.py inspect
```

先通过项目 UI 创建一个**空的、专用的测试知识库**，绑定要测的 Embedding 模型，
记下知识库 ID。启动 API、解析 Worker 以及对应的数据库、消息队列和检索服务。
使用已登录用户的 Bearer JWT，放入环境变量 `BENCHMARK_API_TOKEN`（不要写入命令、
报告或 Git）：

```sh
python scripts/weknora_retrieval_benchmark.py ingest \
  --base-url http://127.0.0.1:8000 --dataset-id 123
python scripts/weknora_retrieval_benchmark.py run \
  --base-url http://127.0.0.1:8000 --dataset-id 123 --k 10 \
  --output /tmp/enerledger-weknora-sample.json
```

`ingest` 将每个原始 passage 作为单独的 Markdown 文档入库，等待全部 `READY`；
如果知识库混入其他文档会停止。`run` 调用 `/api/v1/recall`，将返回的文档 ID
映射回原始 passage ID，同一 passage 的多个 chunk 只计一次。任何检索路失败会
使本次运行失败。当前测量位置是**融合后的候选结果、重排之前**，报告明确记录
这一点；API 的 Top K 来自知识库配置，所以应使候选窗口至少覆盖本次 `--k`。

另一个系统如能导出 `{ "问题ID": ["按名次排列的passage ID", ...] }` JSON，
可用同一个计分器，避免各自实现指标造成口径偏差：

```sh
python scripts/weknora_retrieval_benchmark.py score \
  --predictions /tmp/weknora-predictions.json --k 10 \
  --output /tmp/weknora-scored.json
```

Precision 使用前 K 个**实际返回且去重**的 passage 作分母；Recall 使用所有
正相关 passage 作分母；NDCG 为二值相关性的 NDCG@K；MRR 取第一个相关 passage
的倒数排名；MAP 是逐题 AP@K 的宏平均，AP 以所有正相关 passage 数归一化。
空结果计零。该口径与 WeKnora 文档中的指标定义相近，但正式系统对比仍须统一
检索阶段、K、模型配置和入库方式，并用同一份导出结果重算两边分数。

上游源码在这个版本还有两处特别口径：MAP 把 AP 除以**本次命中的相关段落数**，
NDCG 的理想排序长度被**实际返回数**限制；这两项与上面的标准口径在漏召回时
可能不同。报告的 `weknora_formula_variants` 单独列出按上游公式计算的这两个值，
不要把它们和 `metrics` 混成同一张比较表。源码：
[`map.go`](https://github.com/Tencent/WeKnora/blob/9114e4e4f905be71976a77c684d3f92731e6f9a6/internal/application/service/metric/map.go)、
[`ndcg.go`](https://github.com/Tencent/WeKnora/blob/9114e4e4f905be71976a77c684d3f92731e6f9a6/internal/application/service/metric/ndcg.go)。

当前官方样例中的 `queries/corpus/qrels/qas` ID 列实际是字符串；同版本的 WeKnora
[`dataset.go`](https://github.com/Tencent/WeKnora/blob/9114e4e4f905be71976a77c684d3f92731e6f9a6/internal/application/service/dataset.go)
按 `int64` 读取。脚本保留原文件、以字符串 ID 计分；在尝试 WeKnora 内置评测前，
应先实测其加载行为，必要时复制样例并无损转换 ID 列。未验证其内置评测可以
直接读取这五个原始文件。
