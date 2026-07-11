import { Card, Typography } from "antd";

export function SkillPreview({ content }: { content: string }) {
  return <Card size="small" title="Markdown 预览"><Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>{content || "暂无内容"}</Typography.Paragraph></Card>;
}
