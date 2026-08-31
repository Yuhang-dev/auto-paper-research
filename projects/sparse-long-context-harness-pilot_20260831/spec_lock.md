<!-- ppt-master-schema: spec-lock/v1 -->
# Execution Lock

## canvas
- viewBox: 0 0 1280 720
- format: PPT 16:9

## communication
- primary_language: zh-CN
- audience: 课题负责人、研究与工程团队，以及需要审阅该方案的导师或技术负责人
- objective: 以约 3:2 的篇幅先展示调研结果与非共识证据，再说明 Harness 工程实现，使受众能区分已验证发现、待补证据和未完成能力并认可下一轮计划。
- core_message: 首期价值在于建立由 Skill、确定性脚本、结构化 Wiki、独立验证和反馈共同驱动的可追溯研究循环。

## mode
- mode: pyramid

## visual_style
- visual_style: vintage-poster

## colors
- background: #F8FAFC
- secondary_bg: #EAF0F6
- primary: #123B5D
- accent: #E76F2E
- secondary_accent: #2A9D8F
- body_text: #17212B
- secondary_text: #52616F
- divider: #B9C8D4

## typography
- font_family: "Microsoft YaHei", Aptos, Arial, sans-serif
- title_family: "Microsoft YaHei", Georgia, serif
- body_family: "Microsoft YaHei", Aptos, Arial, sans-serif
- body: 24
- title: 42
- subtitle: 32
- annotation: 18
- footer: 14

## icons
- library: tabler-outline
- stroke_width: 2
- inventory: tabler-outline/search, tabler-outline/file-text, tabler-outline/database, tabler-outline/link, tabler-outline/shield-check, tabler-outline/refresh, tabler-outline/alert-triangle, tabler-outline/git-branch, tabler-outline/book, tabler-outline/cpu, tabler-outline/chart-bar, tabler-outline/code

## page_rhythm
- P01: anchor
- P02: anchor
- P03: dense
- P04: dense
- P05: breathing
- P06: anchor
- P07: anchor
- P08: dense
- P09: dense
- P10: dense
- P11: dense
- P12: dense
- P13: breathing
- P14: anchor
- P15: dense
- P16: anchor

## pptx_structure
- mode: flat

## forbidden
- `mask`, `<style>`, `class`, external CSS, `<foreignObject>`, `textPath`, `@font-face`, `<animate*>`, `<set>`, `<script>` / event attributes, `<iframe>`
- HTML named entities in text; write typography as raw Unicode and escape XML reserved characters
