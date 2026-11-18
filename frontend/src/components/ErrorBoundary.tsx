import { Button, Card, Result, Typography } from "antd";
import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

const { Paragraph, Text } = Typography;

type Props = { children: ReactNode };

type State = { error: Error | null };

/**
 * Stops one broken page from taking the whole application down.
 *
 * Why this exists: without it, a render-time exception in any page unmounts the
 * entire React tree and the user sees a blank white screen with no indication of
 * what failed. That is exactly what happened when the arena page mapped over an
 * object instead of an array -- the page had rendered fine, then went white a few
 * seconds later when the data arrived, and there was nothing on screen to say
 * why.
 *
 * It is deliberately a class: React has no hook equivalent for
 * ``componentDidCatch``.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Kept in the console rather than swallowed: the boundary makes the failure
    // visible instead of silent, not invisible instead of loud.
    console.error("Page render failed:", error, info.componentStack);
  }

  private reset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <Card className="panel-card" style={{ margin: 24 }}>
        <Result
          status="error"
          title="页面渲染出错"
          subTitle="该页面遇到异常，已被隔离，应用其他部分仍可正常使用。"
          extra={[
            <Button key="retry" type="primary" onClick={this.reset}>
              重试
            </Button>,
            <Button key="reload" onClick={() => window.location.reload()}>
              重新加载
            </Button>,
          ]}
        >
          {/* The message is shown, not hidden: an error you cannot read is a
              second bug on top of the first. */}
          <Paragraph>
            <Text type="danger" strong>
              {error.message}
            </Text>
          </Paragraph>
        </Result>
      </Card>
    );
  }
}
