# Prototype Instructions

Run the local server yourself and open the preview in the browser available to this environment. Do not give the user server-start instructions when you can run it.

Before making substantial visual changes, use the Product Design plugin's `get-context` skill when the visual source is unclear or no longer matches the current goal. When the user gives durable prototype-specific design feedback, preferences, or decisions, record them in `AGENTS.md`.

When implementing from a selected generated mock, treat that image as the source of truth for layout, component anatomy, density, spacing, color, typography, visible content, and hierarchy.

Build app UI in `src/`. Keep `.openai/hosting.json`, `worker/index.js`, `scripts/prepare-sites-build.mjs`, and `tests/sites-worker.test.mjs` intact so the same local prototype can be handed to Sites. Before a Sites handoff, run `npm run build` and `npm run test:sites`; the build must leave `dist/client/index.html`, `dist/server/index.js`, and `dist/.openai/hosting.json`.

## Durable product and visual decisions

- The application home is conversation-first. `/` is the primary conversation screen and `/playground` is only a compatibility redirect; do not reintroduce a dashboard hero ahead of the composer.
- Follow LinkRag's single-message-column conversation anatomy: centered empty composer, bottom-docked composer after the first message, dataset/model controls inside the composer, and sources in an on-demand drawer.
- Conversation answers must render valid `[片段N]` mentions as clickable citation chips. Clicking one opens the right-side `召回片段` drawer, scrolls to and highlights the matching citation, while the message-level recall action exposes every hit and distinguishes hits that actually entered the answer context.
- Conversation composer dataset/model selectors use mutually exclusive custom popovers. Dataset selection remains multi-select with an explicit `完成` action plus outside-click/Escape dismissal; model selection is single-select and closes immediately after selection. Do not use native select menus for these controls.
- The conversation composer exposes focus only on its outer shell. The inner textarea must not render a second border, outline, or focus shadow.
- Use standard product terms: `对话`, `新建对话`, `文档`, `数据集`, `混合检索`, `召回片段`, `来源与引用`. Do not use `知识问答`, `发起问答`, or `知识文件` in the UI.
- Use `能碳会计 AI 智能体` as the public product name and `能碳会计` / `AI 智能体` as the compact sidebar lockup. Keep project-theme copy restrained and limited to identity, the conversation empty state, and directly relevant page descriptions; do not present roadmap capabilities as delivered features.
- The foundational canvas, sidebar, and workspace background are pure white `#ffffff`; use only cool light gray-blue surfaces for hierarchy. Ink remains `#243632` and the interface accent remains `#3f766c`. Primary action buttons use cool blue `#3f6fa8` with active blue `#315d91`; secondary buttons stay neutral and danger buttons keep their semantic danger color. Do not use black or red/coral as primary, active, selected, or hero colors.
- At medium widths (including 924px), hide the permanent sidebar earlier and let dataset detail use the full viewport. Dataset documents must remain a single full-width list instead of becoming multi-column cards; use compact two-line rows and stack row metadata only on narrow screens. Filenames and errors must wrap.
- A dataset document has two explicit detail entries: the filename link and a visible `详情` action. Both navigate to `/datasets/:datasetId/documents/:documentId`; never make the whole row clickable because lifecycle controls share the row.
- Document detail is a deep-linkable page, not a temporary drawer. Its primary view is the complete parsed Markdown rendered as one continuous document and parsed by one Markdown AST; insert source-chunk dividers only between safe top-level AST nodes, keep every same-line/approximate semantic boundary with continuous reader numbering, and never render independent chunk cards or concatenate retrieval chunk content. Keep derived image/table chunks and neighbor-overlap text out of the reading flow. Boundary metadata may expand on demand, while engineering storage details stay out of the client-facing page. Private parsed-image proxy URLs must be fetched with the tenant header and rendered from short-lived object URLs.
- Conversation history is server-owned through `/api/v1/agent/conversations`; never fall back to presenting browser-only messages as durable history.
- Real API mode is the default. Backend/network failures must be shown explicitly and must not silently replace business data with preview records. Preview data is allowed only when `VITE_DEMO_MODE=true` is deliberately configured.
- Document upload is asynchronous: successful uploads are actively dispatched through RabbitMQ and normally return `QUEUED`; MySQL remains the status and lease source of truth. Do not describe upload as synchronous parsing or claim that there is a separate task table.
- While any document is `QUEUED` or `PROCESSING`, refresh document state from the backend at a bounded interval; manual refresh remains available. Do not simulate queue progress in real API mode.
- Real API mode requires an authorized administrator or restricted reviewer login. Store and send the returned Bearer token, return to `/login` on 401, and never add a registration entry point or restore the trusted `X-User-Id` header. Reviewers only see chat and document review; management routes remain admin-only.
