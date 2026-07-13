import { Button, InputNumber, Select, Space, Switch } from "antd";
import type { Skill } from "../../types/api";

export function ModelSkillBinding({
  skills,
  value,
  onChange,
}: {
  skills: Skill[];
  value: Array<{
    skill_id: string;
    enabled: boolean;
    priority: number;
    config_json: Record<string, unknown>;
  }>;
  onChange: (
    value: Array<{
      skill_id: string;
      enabled: boolean;
      priority: number;
      config_json: Record<string, unknown>;
    }>,
  ) => void;
}) {
  const enabledSkills = skills.filter((skill) => skill.enabled);

  return (
    <Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Select
        mode="multiple"
        showSearch
        optionFilterProp="label"
        value={value.map((item) => item.skill_id)}
        options={enabledSkills.map((skill) => ({ value: skill.id, label: skill.display_name }))}
        onChange={(ids) =>
          onChange(
            ids.map(
              (skill_id, index) =>
                value.find((item) => item.skill_id === skill_id) ?? {
                  skill_id,
                  enabled: true,
                  priority: index,
                  config_json: {},
                },
            ),
          )
        }
        placeholder="选择 Skills"
        style={{ width: "100%" }}
      />
      <div className="skill-binding-list">
        {value.map((item, index) => (
          <div className="skill-binding-row" key={item.skill_id}>
            <div className="skill-binding-label">
              {skills.find((skill) => skill.id === item.skill_id)?.display_name ?? item.skill_id}
            </div>
            <Space wrap>
              <Switch
                checked={item.enabled}
                onChange={(enabled) =>
                  onChange(value.map((row) => (row.skill_id === item.skill_id ? { ...row, enabled } : row)))
                }
              />
              <InputNumber
                min={0}
                value={item.priority}
                onChange={(priority) =>
                  onChange(
                    value.map((row) =>
                      row.skill_id === item.skill_id ? { ...row, priority: priority ?? index } : row,
                    ),
                  )
                }
              />
              <Button onClick={() => onChange(value.filter((row) => row.skill_id !== item.skill_id))}>
                移除
              </Button>
            </Space>
          </div>
        ))}
      </div>
    </Space>
  );
}
