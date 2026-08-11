-- EnerLedger AI 基础模型配置 SQL（按当前 MySQL 配置脱敏生成）
--
-- 安全约束：llm_config.api_key 存储的是应用使用 AES-256-GCM 加密后的 Base64 密文，
-- 不是明文 API Key。密文必须使用部署环境中相同的 LLM_API_KEY_ENCRYPTION_KEY 生成。
-- 推荐通过“模型配置”页面或 POST /api/v1/llm/configs 写入，由应用自动加密。
-- 如果确实需要直接执行本 SQL，请先把下面四个 NULL 改成对应的合法加密密文。
-- 变量保持 NULL 时，相应 INSERT 会安全跳过；已有配置的 api_key 永远不会被本脚本覆盖。

USE `tolink_rag_db`;
SET NAMES utf8mb4;

SET @OWNER_USER_ID = 1;
SET @QWEN_API_KEY_CIPHERTEXT = NULL;
SET @DEEPSEEK_API_KEY_CIPHERTEXT = NULL;
SET @DOUBAO_API_KEY_CIPHERTEXT = NULL;
SET @KIMI_API_KEY_CIPHERTEXT = NULL;

INSERT INTO `llm_config` (
  `scope`, `owner_user_id`, `provider_id`, `provider_type`, `model_name`,
  `display_name`, `capability`, `protocol`, `api_base_url`, `api_key`,
  `is_active`, `snapshot_version`
)
SELECT
  'USER', @OWNER_USER_ID, 1, 'qwen', 'qwen3.7-text-embedding',
  'Qwen3.7 Text Embedding', 'EMBEDDING', 'openai',
  'https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings',
  @QWEN_API_KEY_CIPHERTEXT, 1, 1
WHERE @QWEN_API_KEY_CIPHERTEXT IS NOT NULL
ON DUPLICATE KEY UPDATE
  `display_name` = VALUES(`display_name`),
  `protocol` = VALUES(`protocol`),
  `api_base_url` = VALUES(`api_base_url`),
  `is_active` = VALUES(`is_active`);

INSERT INTO `llm_config` (
  `scope`, `owner_user_id`, `provider_id`, `provider_type`, `model_name`,
  `display_name`, `capability`, `protocol`, `api_base_url`, `api_key`,
  `is_active`, `snapshot_version`
)
SELECT
  'USER', @OWNER_USER_ID, 1, 'deepseek', 'deepseek-v4-flash',
  'DeepSeek V4 Flash', 'CHAT', 'openai',
  'https://api.deepseek.com/chat/completions',
  @DEEPSEEK_API_KEY_CIPHERTEXT, 1, 1
WHERE @DEEPSEEK_API_KEY_CIPHERTEXT IS NOT NULL
ON DUPLICATE KEY UPDATE
  `display_name` = VALUES(`display_name`),
  `protocol` = VALUES(`protocol`),
  `api_base_url` = VALUES(`api_base_url`),
  `is_active` = VALUES(`is_active`);

INSERT INTO `llm_config` (
  `scope`, `owner_user_id`, `provider_id`, `provider_type`, `model_name`,
  `display_name`, `capability`, `protocol`, `api_base_url`, `api_key`,
  `is_active`, `snapshot_version`
)
SELECT
  'USER', @OWNER_USER_ID, 1, 'doubao', 'doubao-embedding-vision-251215',
  'Doubao Sparse Embedding', 'SPARSE_EMBEDDING', 'doubao_vision',
  'https://ark.cn-beijing.volces.com/api/v3/embeddings/multimodal',
  @DOUBAO_API_KEY_CIPHERTEXT, 1, 1
WHERE @DOUBAO_API_KEY_CIPHERTEXT IS NOT NULL
ON DUPLICATE KEY UPDATE
  `display_name` = VALUES(`display_name`),
  `protocol` = VALUES(`protocol`),
  `api_base_url` = VALUES(`api_base_url`),
  `is_active` = VALUES(`is_active`);

INSERT INTO `llm_config` (
  `scope`, `owner_user_id`, `provider_id`, `provider_type`, `model_name`,
  `display_name`, `capability`, `protocol`, `api_base_url`, `api_key`,
  `is_active`, `snapshot_version`
)
SELECT
  'USER', @OWNER_USER_ID, 1, 'kimi', 'kimi-k2.6',
  'Kimi K2.6', 'VISION', 'openai',
  'https://api.kimi.com/coding/v1/chat/completions',
  @KIMI_API_KEY_CIPHERTEXT, 1, 1
WHERE @KIMI_API_KEY_CIPHERTEXT IS NOT NULL
ON DUPLICATE KEY UPDATE
  `display_name` = VALUES(`display_name`),
  `protocol` = VALUES(`protocol`),
  `api_base_url` = VALUES(`api_base_url`),
  `is_active` = VALUES(`is_active`);

-- 脱敏验证：不查询 api_key。
SELECT
  `id`, `scope`, `owner_user_id`, `provider_type`, `model_name`,
  `display_name`, `capability`, `protocol`, `api_base_url`,
  `is_active`, `snapshot_version`
FROM `llm_config`
WHERE `scope` = 'USER' AND `owner_user_id` = @OWNER_USER_ID
ORDER BY `capability`, `provider_type`, `model_name`;
