# BEIR NFCorpus 检索基准

采用 [BEIR 官方 NFCorpus 下载包](https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/nfcorpus.zip)，
上游 MD5 为 `a89dba18a62ef92f7d323ec890a0d38d`。脚本只读取 `qrels/test.tsv`
对应的 323 个测试问题，语料为 3,633 篇文档。原始文件保存在被 Git 忽略的 `data/`，
报告会记录解压后文件的 SHA-256。

```sh
python scripts/nfcorpus_retrieval_benchmark.py fetch
python scripts/nfcorpus_retrieval_benchmark.py inspect
```

在项目中创建空的专用测试知识库，并绑定用于实验的 Embedding 模型。启动 API、
解析 Worker、数据库、消息队列和检索服务，设置已登录用户的 Bearer JWT 到
`BENCHMARK_API_TOKEN`，不要把 Token 写入命令或报告。完整语料须全部入库；
`ingest` 可从已上传的部分继续。

```sh
python scripts/nfcorpus_retrieval_benchmark.py ingest \
  --base-url http://127.0.0.1:8000 --dataset-id 123
python scripts/nfcorpus_retrieval_benchmark.py run \
  --base-url http://127.0.0.1:8000 --dataset-id 123 --k 10 \
  --output /tmp/enerledger-nfcorpus.json
```

每篇 BEIR 文档作为一份 Markdown 入库。`run` 通过 `/api/v1/recall` 获取融合候选，
按源文档 ID 去重后评分；结果属于**检索阶段**，不代表生成答案质量。设置知识库的
召回候选窗口，使它至少覆盖 `--k` 个不同源文档。两套系统需要使用相同语料、
test 问题、K、源文档粒度和评分器，并记录各自模型和检索配置。另一个系统的
预测结果可导出为 `{ "query-id": ["按名次排列的corpus-id", ...] }` JSON：
`score` 也可直接读取本脚本或 WeKnora 适配脚本生成的完整报告 JSON。

```sh
python scripts/nfcorpus_retrieval_benchmark.py score \
  --predictions /tmp/other-nfcorpus-predictions.json --k 10 \
  --output /tmp/other-nfcorpus-scored.json
```

对 WeKnora 可直接使用同一评分器的适配脚本。先创建独立知识库、配置模型，
把该环境的 JWT 放入 `WEKNORA_BENCHMARK_TOKEN`，再导入和检索。脚本按原始
BEIR 文档 ID 映射 WeKnora 的 `knowledge_id`，并拒绝语料外文档或未完成解析的语料。

```sh
python scripts/weknora_nfcorpus_benchmark.py ingest \
  --base-url http://127.0.0.1:18080 --kb-id YOUR_KB_ID
python scripts/weknora_nfcorpus_benchmark.py run \
  --base-url http://127.0.0.1:18080 --kb-id YOUR_KB_ID \
  --weknora-checkout /path/to/WeKnora --match-count 64 --k 10 \
  --output /tmp/weknora-nfcorpus.json
```

两边分别调用项目公开的检索入口，取前 64 个分块候选，在源文档层去重，再计算
`@10` 指标。这个控制了测试数据、问题、排名粒度和评分口径；两套产品的切分、
索引、融合和词法检索实现仍然不同，比较应视作端到端检索配置的结果。报告须记录
模型、版本、候选窗口和解析成功数量，不应与官方 BEIR 榜单的数值直接等同。

官方语料有 40 对正文完全相同但 ID 不同的文档。WeKnora 按相同文件类型与正文
哈希去重，因此适配脚本把每对第二篇保存为 `.txt`，第一篇保持 `.md`。两篇的
正文原始字节和独立的 BEIR ID 都保留；这 40 篇的解析器路径可能与 `.md` 不同，
分析差异时应把这个限制算进去。Qwen `qwen3.7-text-embedding` 在 WeKnora
模型目录的默认维度为 1024；若本项目使用 2048 维，需在 WeKnora 模型配置中
把 `embedding_parameters.dimension` 设为 2048 且启用
`embedding_parameters.supports_dimension_override`，确认两边都真正索引 2048 维。

Precision@K 分母固定为 K；Recall@K 与 AP@K 分母为该问题全部相关文档数；
NDCG 使用二值相关性；MRR 取前 K 位首个相关文档的倒数名次。指标逐题计算后
取算术平均，空位视作不相关。该口径对齐 BEIR 使用的 `pytrec_eval` 二值指标。

NFCorpus 是英文营养/生物医学语料，可用于公开检索基准对比；中文能碳领域的
实际效果仍需独立的领域测试集验证。

本机完整实验的配置、原始排名和结果见 [RESULTS.md](RESULTS.md)。两份
`run` 报告可用 `scripts/compare_nfcorpus_reports.py` 做逐题配对 bootstrap：

```sh
python scripts/compare_nfcorpus_reports.py \
  --left benchmarks/beir/nfcorpus/results/enerledger-k10.json \
  --right benchmarks/beir/nfcorpus/results/weknora-k10.json \
  --output benchmarks/beir/nfcorpus/results/compare-512chars-k10.json
```
