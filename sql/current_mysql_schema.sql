-- EnerLedger AI 当前 MySQL 完整建表 SQL
-- 来源：2026-08-11 运行中的 tolink_rag_db，使用 mysqldump --no-data 导出后去除实例 AUTO_INCREMENT 值。
-- 本文件只创建数据库和表，不包含业务数据，也不删除已有对象。

CREATE DATABASE IF NOT EXISTS `tolink_rag_db`
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE `tolink_rag_db`;
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS `alembic_version` (
  `version_num` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  PRIMARY KEY (`version_num`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `llm_config` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `scope` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'USER',
  `owner_user_id` bigint unsigned NOT NULL,
  `provider_id` bigint unsigned NOT NULL DEFAULT '1',
  `provider_type` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `model_name` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `display_name` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `capability` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `protocol` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `api_base_url` varchar(512) COLLATE utf8mb4_unicode_ci NOT NULL,
  `api_key` varchar(512) COLLATE utf8mb4_unicode_ci NOT NULL,
  `is_active` tinyint(1) NOT NULL DEFAULT '1',
  `snapshot_version` bigint unsigned NOT NULL DEFAULT '1',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_llm_config_owner_model`
    (`scope`,`owner_user_id`,`provider_type`,`model_name`,`capability`),
  KEY `idx_llm_config_owner_capability`
    (`owner_user_id`,`capability`,`is_active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `dataset` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `user_id` bigint unsigned NOT NULL,
  `name` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `description` varchar(512) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `status` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'ACTIVE',
  `dense_embedding_config_id` bigint unsigned NOT NULL,
  `sparse_embedding_config_id` bigint unsigned NOT NULL,
  `chat_config_id` bigint unsigned DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `vision_config_id` bigint unsigned DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_dataset_user_name` (`user_id`,`name`),
  KEY `idx_dataset_user_updated` (`user_id`,`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `document` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `dataset_id` bigint unsigned NOT NULL,
  `user_id` bigint unsigned NOT NULL,
  `filename` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `file_type` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `file_size` bigint unsigned NOT NULL,
  `content_type` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `raw_bucket` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `raw_object_key` varchar(512) COLLATE utf8mb4_unicode_ci NOT NULL,
  `parsed_bucket` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `parsed_object_key` varchar(512) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `parser_backend` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'opendataloader',
  `status` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'QUEUED',
  `error_message` varchar(1000) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `page_count` int DEFAULT NULL,
  `chunk_count` int NOT NULL DEFAULT '0',
  `parse_time_ms` int DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `attempt_count` int NOT NULL DEFAULT '0',
  `version` int NOT NULL DEFAULT '1',
  `available_at` datetime DEFAULT NULL,
  `lease_token` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `lease_owner` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `lease_expires_at` datetime DEFAULT NULL,
  `queued_at` datetime DEFAULT NULL,
  `processing_started_at` datetime DEFAULT NULL,
  `finished_at` datetime DEFAULT NULL,
  `error_code` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `reparse_requested` tinyint(1) NOT NULL DEFAULT '0',
  `parse_quality_status` varchar(32) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `parse_quality` json DEFAULT NULL,
  `dispatch_status` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'PENDING',
  `dispatch_attempt_count` int NOT NULL DEFAULT '0',
  `dispatch_available_at` datetime DEFAULT NULL,
  `dispatch_lease_token` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `dispatch_lease_expires_at` datetime DEFAULT NULL,
  `dispatch_error` varchar(1000) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_document_dataset_created` (`dataset_id`,`created_at`),
  KEY `idx_document_user_status` (`user_id`,`status`),
  KEY `idx_document_queue_available` (`status`,`available_at`,`id`),
  KEY `idx_document_lease_expiry` (`status`,`lease_expires_at`,`id`),
  KEY `idx_document_dispatch_available`
    (`dispatch_status`,`dispatch_available_at`,`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `document_chunk` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `chunk_id` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `document_id` bigint unsigned NOT NULL,
  `dataset_id` bigint unsigned NOT NULL,
  `user_id` bigint unsigned NOT NULL,
  `content` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `content_hash` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `chunk_type` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'text',
  `start_line` int DEFAULT NULL,
  `end_line` int DEFAULT NULL,
  `chunk_index` int NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `document_version` int NOT NULL DEFAULT '1',
  `start_page` int DEFAULT NULL,
  `end_page` int DEFAULT NULL,
  `structure_metadata` json DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_document_chunk_chunk_id` (`chunk_id`),
  KEY `idx_document_chunk_user_dataset` (`user_id`,`dataset_id`),
  KEY `idx_document_chunk_document_index` (`document_id`,`chunk_index`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
