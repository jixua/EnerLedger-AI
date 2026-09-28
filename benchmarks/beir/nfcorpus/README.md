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

```sh
python scripts/nfcorpus_retrieval_benchmark.py score \
  --predictions /tmp/other-nfcorpus-predictions.json --k 10 \
  --output /tmp/other-nfcorpus-scored.json
```

Precision@K 分母固定为 K；Recall@K 与 AP@K 分母为该问题全部相关文档数；
NDCG 使用二值相关性；MRR 取前 K 位首个相关文档的倒数名次。指标逐题计算后
取算术平均，空位视作不相关。该口径对齐 BEIR 使用的 `pytrec_eval` 二值指标。

NFCorpus 是英文营养/生物医学语料，可用于公开检索基准对比；中文能碳领域的
实际效果仍需独立的领域测试集验证。
